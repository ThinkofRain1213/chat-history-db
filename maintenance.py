# -*- coding: utf-8 -*-
"""启动期空间治理：按阈值回收 LanceDB 旧版本清单，并顺带更新 FTS 索引。

LanceDB 每次写入都会留下一个全量快照版本清单，单条写入让清单体积按 O(N²) 累积。
本模块在 MCP 进程内、HTTP 端口就绪之后起一个后台线程，按「垃圾体积」阈值决定是否执行
optimize(cleanup_older_than=0)：删除除最新外的全部版本、合并碎片、把新数据补进 FTS 索引。
只跑一次、不参与请求路径。

安全约束：
- delete_unverified=False：True 会删掉在途事务的文件，并发写入下实测 8/20 次失败。
- 不抢 db._WRITE_LOCK：抢锁会让一次 /remember 卡住整个 optimize 时长（实测 17s），
  而 worker 的 HTTP 超时是 30s。并发写入用 False 是安全的（实测 0/20 失败）。
- 绝不写 stdout：stdio 是 MCP 协议通道，任何字节都会破坏协议帧。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import config
import db

DEFAULT_THRESHOLD_MB = 10
_TABLES = (config.TABLE, config.ARCHIVE_TABLE)  # 维护必须覆盖主表与归档表
_STARTUP_DELAY = 2.0  # 秒：先让 stdio 握手完成，再动磁盘
_RUN_LOCK = threading.Lock()  # 非重入：同一时刻只允许一次维护
_DEFAULT_LOG = Path.home() / ".agent" / "hooks" / "chat_maintenance.log"


def _enabled() -> bool:
    return os.environ.get("CHAT_HISTORY_GC", "1").strip().lower() not in ("0", "off", "false", "no")


def _threshold_bytes() -> int:
    raw = os.environ.get("CHAT_HISTORY_GC_MB", "").strip()
    try:
        mb = float(raw) if raw else float(DEFAULT_THRESHOLD_MB)
    except ValueError:
        mb = float(DEFAULT_THRESHOLD_MB)
    return int(mb * 1024 * 1024)


def _versions_dir(table: str) -> Path:
    return Path(db._db_file()) / f"{table}.lance" / "_versions"


def _scan_bytes(root: Path) -> int:
    total = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def garbage_bytes(path: Path | None = None) -> int:
    """所有表 _versions 目录的总字节数（旧版本清单占用）；目录不存在按 0 计。

    path 给定时只统计该目录（测试用）。实测 4,769 个文件约 4ms，比用 manifest 大小
    做代理更准，且足够便宜。
    """
    if path is not None:
        return _scan_bytes(path)
    return sum(_scan_bytes(_versions_dir(name)) for name in _TABLES)


def _log_path() -> Path:
    raw = os.environ.get("CHAT_HISTORY_GC_LOG", "").strip()
    return Path(raw) if raw else _DEFAULT_LOG


def _log(message: str) -> None:
    """同时写 stderr 与日志文件：ZCode 不保留 MCP 的 stderr，没有文件就查不到记录。"""
    line = f"[chat-history] maintenance {message}"
    sys.stderr.write(line + "\n")
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def _optimize(tbl) -> None:
    """唯一的清理调用点（测试可替换）：删旧版本 + 合并碎片 + 更新索引。"""
    tbl.optimize(cleanup_older_than=timedelta(seconds=0), delete_unverified=False)


def maybe_optimize(force: bool = False) -> dict | None:
    """按阈值决定是否清理；返回统计 dict，未执行返回 None。永不向外抛异常。"""
    if not force and not _enabled():
        return None
    if not _RUN_LOCK.acquire(blocking=False):
        _log("skip: 已有维护在进行")
        return None
    try:
        return _run(force)
    except Exception as exc:  # noqa: BLE001  维护失败绝不能影响 MCP 启动
        _log(f"failed: {type(exc).__name__}")
        return None
    finally:
        _RUN_LOCK.release()


def _run(force: bool) -> dict | None:
    before_bytes = garbage_bytes()
    threshold = _threshold_bytes()
    if not force and before_bytes <= threshold:
        # 记一条 skip：否则「没清理」既可能是低于阈值、也可能是维护没跑，无法区分
        _log(f"skip: {before_bytes / 1024 / 1024:.2f} MiB <= {threshold / 1024 / 1024:.0f} MiB")
        return None

    tables = []
    rows_before = rows_after = 0
    concurrent_writes = 0
    for name in _TABLES:  # 主表与归档表都要 optimize，否则归档表会变成空间黑洞
        handle = db._ensure_db()
        tbl = db._open_table_or_none(handle, name)
        if tbl is None:
            continue
        count_before = int(tbl.count_rows())
        started = time.perf_counter()
        _optimize(tbl)
        elapsed = time.perf_counter() - started
        tbl = db._open_table_or_none(db._ensure_db(), name)
        count_after = int(tbl.count_rows()) if tbl is not None else -1
        delta = count_after - count_before
        if delta > 0:
            concurrent_writes += delta  # optimize 不持写锁，期间的正常写入要可见（而非当成异常）
        rows_before += count_before
        rows_after += count_after
        tables.append({"table": name, "rows_before": count_before, "rows_after": count_after,
                       "row_delta": delta, "seconds": round(elapsed, 1)})

    if not tables:
        return None
    after_bytes = garbage_bytes()
    report = {
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "rows_before": rows_before,
        "rows_after": rows_after,
        "concurrent_writes": concurrent_writes,
        "tables": tables,
    }
    _log(
        "cleaned "
        f"{before_bytes / 1024 / 1024:.2f} MiB -> {after_bytes / 1024 / 1024:.2f} MiB, "
        f"rows {rows_before} -> {rows_after}"
        + (f"（含 optimize 期间并发写入 {concurrent_writes} 行）" if concurrent_writes else "")
        + ", "
        + ", ".join(f"{t['table']}={t['seconds']}s" for t in tables)
    )
    if rows_after < rows_before:
        # 只有「变少」才是异常；变多说明 optimize 期间有正常写入，不算故障
        _log(f"WARN 行数减少: {rows_before} -> {rows_after}，请检查备份")
    return report


def _background() -> None:
    time.sleep(_STARTUP_DELAY)
    maybe_optimize()


def start_background_maintenance() -> threading.Thread | None:
    """在 MCP 启动流程中调用：起 daemon 线程做一次阈值清理。禁用时返回 None。"""
    if not _enabled():
        return None
    thread = threading.Thread(target=_background, daemon=True, name="chat-history-gc")
    thread.start()
    return thread
