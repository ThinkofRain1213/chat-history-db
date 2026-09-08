# -*- coding: utf-8 -*-
"""chat-history-db 备份 / 恢复 CLI。

用法：
    python tools/backup.py backup              # 备份 chat.db/ + title_cache.json 到 ~/.agent/backups/
    python tools/backup.py list                # 列出已有备份（时间倒序）
    python tools/backup.py restore <备份目录>    # 用指定备份覆盖回项目（先自动备份当前）
    python tools/backup.py verify              # 用 _validate_messages_schema 校验当前库 schema
    python tools/backup.py vacuum [--yes]      # 回收旧版本清单+合并碎片+更新索引（不可回滚，先自动备份）
    python tools/backup.py reindex             # 重建 text FTS 索引（vacuum 后仍有未索引行时用）

备份遵循 ~/.agent/backups 约定，命名 chat-history-db-<时间戳>。
数据涉及：chat.db/（LanceDB 目录，含 messages.lance）、title_cache.json。
error_codes.json 是代码库文档，随代码走，不单独备份。
注意：MCP 进程持有数据库，备份/恢复应在服务不处于写入态时执行。
"""
import argparse
import json
import os
import shutil
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
# 以脚本方式运行时 sys.path[0] 是 tools/，补上项目根才能 import 同目录的 db / config
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))
_BACKUP_ROOT = Path.home() / ".agent" / "backups"
_PREFIX = "chat-history-db-"
_MCP_PORT = int(os.environ.get("CHAT_HISTORY_PORT", "17891"))
_PENDING_DB = Path(
    os.environ.get("CHAT_PENDING_DB") or (Path.home() / ".agent" / "hooks" / "chat_pending.db")
)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _data_sources() -> tuple[Path, Path]:
    """按当前配置解析实际数据路径(库目录, 标题缓存)——与服务端一致，尊重环境变量覆盖。"""
    db = Path(os.environ.get("CHAT_HISTORY_DB") or (_PROJECT / "chat.db"))
    cache = Path(os.environ.get("TITLE_CACHE_PATH") or (_PROJECT / "title_cache.json"))
    return db, cache


def backup(dest_root: Path | None = None) -> Path:
    """把当前数据拷到带时间戳的备份目录，返回该目录。"""
    root = dest_root or _BACKUP_ROOT
    root.mkdir(parents=True, exist_ok=True)
    base = f"{_PREFIX}{_timestamp()}"
    dest = root / base
    n = 1
    while dest.exists():  # 同秒内多次备份：追加序号避免撞目录名
        dest = root / f"{base}-{n}"
        n += 1
    dest.mkdir(parents=True, exist_ok=False)
    db, cache = _data_sources()
    for src, label in ((db, "chat.db"), (cache, "title_cache.json")):
        if not src.exists():
            continue
        target = dest / label
        if src.is_dir():
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)
    return dest


def list_backups(root: Path | None = None) -> list[Path]:
    """列出备份目录（时间倒序）。"""
    root = root or _BACKUP_ROOT
    if not root.exists():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith(_PREFIX)),
        key=lambda p: p.name,
        reverse=True,
    )


def restore(archive: Path, dest_root: Path | None = None, force: bool = False) -> None:
    """用备份覆盖回当前数据路径。恢复前先自动备份当前，保证可回滚。

    与 vacuum 同样先过 _guard：MCP 在跑或队列有积压时拒绝——否则会在写入过程中
    rmtree 掉正在被写的库目录。
    """
    archive = Path(archive)
    if not archive.is_dir():
        raise SystemExit(f"备份不存在或不是目录: {archive}")
    reasons = _guard(force)
    if reasons:
        raise SystemExit(
            "拒绝执行 restore：\n  - " + "\n  - ".join(reasons)
            + "\n请先停止 MCP、清空队列，或确认风险后加 --force。"
        )
    try:
        backup(dest_root=dest_root)
    except Exception as e:  # noqa: BLE001  恢复动作不该被「备份当前失败」挡住
        print(f"[warn] 备份当前状态失败: {e}", file=sys.stderr)
    db, cache = _data_sources()
    for src, dst in ((archive / "chat.db", db), (archive / "title_cache.json", cache)):
        if not src.exists():
            continue
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def verify() -> None:
    """用启动时同样的 schema 校验检查当前库（不加载真实模型），并覆盖归档表。"""
    import config
    import db as dbmod
    try:
        ok = dbmod._validate_messages_schema()
        archived = dbmod._migrate_messages_schema(table=config.ARCHIVE_TABLE)
    except Exception as e:  # noqa: BLE001
        print(f"schema 校验失败: {type(e).__name__}", file=sys.stderr)
        raise SystemExit(1)
    if ok:
        print("schema 健康：当前 messages 表结构符合预期")
    else:
        print("schema：messages 表缺失或未初始化（首次 remember() 会自动创建）")
    print("归档表：" + ("已存在且结构符合预期" if archived else "不存在（尚未归档过任何会话）"))


def _tree_stats(path: Path) -> tuple[int, int]:
    """(文件数, 总字节数)；路径不存在返回 (0, 0)。"""
    if not path.exists():
        return 0, 0
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def _index_stats(tbl) -> list[dict]:
    """索引概览：名称 / 类型 / 列 / 已索引行 / 未索引行。"""
    out = []
    for i in tbl.list_indices():
        out.append({
            "name": i.name,
            "type": i.index_type,
            "columns": list(i.columns),
            "indexed_rows": int(getattr(i, "num_indexed_rows", 0) or 0),
            "unindexed_rows": int(getattr(i, "num_unindexed_rows", 0) or 0),
        })
    return out


def _mcp_running(port: int = _MCP_PORT) -> bool:
    """本地 MCP 端口是否在监听——在跑就可能有并发写入。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _pending_backlog() -> int:
    """待入库队列中 pending/processing 条数；队列不存在或不可读返回 0。"""
    if not _PENDING_DB.exists():
        return 0
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{_PENDING_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM chat_pending WHERE status IN ('pending','processing')"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except Exception:  # noqa: BLE001  队列不可读不阻断，端口检查兜底
        return 0


def _guard(force: bool) -> list[str]:
    """vacuum 前置安全检查，返回阻止原因（空列表 = 可执行）。"""
    if force:
        return []
    reasons = []
    if _mcp_running():
        reasons.append(f"MCP 正在监听 127.0.0.1:{_MCP_PORT}（可能正在写入）")
    backlog = _pending_backlog()
    if backlog:
        reasons.append(f"待入库队列仍有 {backlog} 条 pending/processing")
    return reasons


def _table_names() -> tuple[str, ...]:
    """需要维护的表：主表 + 归档表（归档表不存在时自动跳过）。"""
    import config
    return (config.TABLE, config.ARCHIVE_TABLE)


def _open_table(name: str):
    """按当前配置打开指定表，返回 (db, tbl)；表不存在时 tbl 为 None。"""
    import db as dbmod
    handle = dbmod._ensure_db()
    return handle, dbmod._open_table_or_none(handle, name)


def _open_messages():
    import config
    return _open_table(config.TABLE)


def reindex() -> list[dict]:
    """重建各表（messages / messages_archive）的 text FTS 索引，返回索引概览。"""
    from lancedb.index import FTS
    out: list[dict] = []
    found = False
    for name in _table_names():
        _, tbl = _open_table(name)
        if tbl is None:
            continue
        found = True
        tbl.create_index("text", config=FTS(base_tokenizer="icu"), replace=True)
        out.extend({"table": name, **item} for item in _index_stats(tbl))
    if not found:
        raise SystemExit("messages 表不存在，无需重建索引")
    return out


def _print_report(r: dict) -> None:
    b, a = r["before"], r["after"]
    print(f"数据库: {r['db_path']}")
    print(f"  文件数: {b['files']:,} -> {a['files']:,}")
    print(f"  体积  : {b['bytes'] / 1024 / 1024:.2f} MiB -> {a['bytes'] / 1024 / 1024:.2f} MiB")
    print(f"  行数  : {b['rows']:,} -> {a['rows']:,}")
    print(f"  释放  : {r['freed_bytes'] / 1024 / 1024:.2f} MiB，减少 {r['removed_files']:,} 个文件")
    for i in a["indices"]:
        print(f"  索引 {i.get('table', '')}.{i['name']}({i['type']}) "
              f"已索引={i['indexed_rows']:,} 未索引={i['unindexed_rows']:,}")
    if r["reindexed"]:
        print("  已自动重建 FTS 索引")
    print("  行数校验:", "通过" if r["rows_preserved"] else "失败")


def vacuum(assume_yes: bool = False, force: bool = False,
           do_backup: bool = True, as_json: bool = False) -> dict:
    """回收旧版本清单 + 合并碎片 + 更新索引（等价 PostgreSQL VACUUM），不改任何行数据。

    LanceDB 每次写入都生成一个「全量快照」版本清单，单条写入会让清单体积二次方累积。
    本操作删除除最新外的全部版本、把小文件合并成大文件、并把新数据补进 FTS 索引。
    清理后旧版本不可回滚，因此执行前必须先有备份；服务在跑时拒绝执行。
    """
    db_path, _ = _data_sources()
    if not db_path.exists():
        raise SystemExit(f"数据库目录不存在: {db_path}")

    reasons = _guard(force)
    if reasons:
        raise SystemExit(
            "拒绝执行 vacuum：\n  - " + "\n  - ".join(reasons)
            + "\n请先停止 MCP、清空队列，或确认风险后加 --force。"
        )

    if do_backup:
        dest = backup()
        if not as_json:
            print(f"已先备份当前状态: {dest}")
    elif not list_backups():
        raise SystemExit("没有可用备份，拒绝执行 vacuum（先跑 backup，或加 --force）")

    if not assume_yes:
        if not sys.stdin.isatty():
            raise SystemExit("非交互环境请显式加 --yes 确认执行 vacuum")
        if input("vacuum 会删除旧版本（不可回滚），确认执行？[y/N] ").strip().lower() not in ("y", "yes"):
            raise SystemExit("已取消")

    files0, bytes0 = _tree_stats(db_path)
    before_rows, before_indices = 0, []
    optimized: list[str] = []
    for name in _table_names():  # 主表与归档表都要 optimize，否则归档表会变成空间黑洞
        _, tbl = _open_table(name)
        if tbl is None:
            continue
        before_rows += int(tbl.count_rows())
        before_indices.extend({"table": name, **item} for item in _index_stats(tbl))
        tbl.optimize(cleanup_older_than=timedelta(seconds=0), delete_unverified=True)
        optimized.append(name)
    if not optimized:
        raise SystemExit("messages 表不存在，无需 vacuum")

    files1, bytes1 = _tree_stats(db_path)
    after_rows, after_indices = 0, []
    for name in optimized:
        _, tbl = _open_table(name)  # 重新打开以读取 optimize 后的最新状态
        if tbl is None:
            continue
        after_rows += int(tbl.count_rows())
        after_indices.extend({"table": name, **item} for item in _index_stats(tbl))

    before = {"files": files0, "bytes": bytes0, "rows": before_rows, "indices": before_indices}
    after = {"files": files1, "bytes": bytes1, "rows": after_rows, "indices": after_indices}

    reindexed = False
    if any(i["unindexed_rows"] for i in after_indices):
        reindex()
        after_rows, after_indices = 0, []
        for name in optimized:
            _, tbl = _open_table(name)
            if tbl is None:
                continue
            after_rows += int(tbl.count_rows())
            after_indices.extend({"table": name, **item} for item in _index_stats(tbl))
        after["rows"], after["indices"] = after_rows, after_indices
        reindexed = True

    report = {
        "db_path": str(db_path),
        "before": before,
        "after": after,
        "freed_bytes": bytes0 - bytes1,
        "removed_files": files0 - files1,
        "rows_preserved": before["rows"] == after["rows"],
        "reindexed": reindexed,
    }

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)

    if not report["rows_preserved"]:
        raise SystemExit(f"行数不一致（{before['rows']} -> {after['rows']}），请立即用备份恢复")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("backup", help="备份数据")
    sub.add_parser("list", help="列出备份")
    p_restore = sub.add_parser("restore", help="恢复备份")
    p_restore.add_argument("archive", help="备份目录（chat-history-db-<时间戳> 或绝对路径）")
    p_restore.add_argument("--force", action="store_true", help="跳过 MCP/队列安全检查")
    sub.add_parser("verify", help="校验当前库 schema")
    p_vacuum = sub.add_parser("vacuum", help="回收旧版本/合并碎片/更新索引（不可回滚，先备份）")
    p_vacuum.add_argument("--yes", action="store_true", help="跳过交互确认")
    p_vacuum.add_argument("--force", action="store_true", help="跳过 MCP/队列安全检查")
    p_vacuum.add_argument("--no-backup", action="store_true", help="不先自动备份（需已有备份）")
    p_vacuum.add_argument("--json", action="store_true", help="输出 JSON 统计")
    sub.add_parser("reindex", help="重建 text FTS 索引")
    args = ap.parse_args()
    if args.cmd == "backup":
        print(f"已备份到: {backup()}")
    elif args.cmd == "list":
        rows = list_backups()
        for p in rows:
            print(p)
        if not rows:
            print("（无备份）")
    elif args.cmd == "restore":
        restore(Path(args.archive).expanduser(), force=args.force)
        print("恢复完成——请重启服务并对 /health 或本工具 verify 校验")
    elif args.cmd == "verify":
        verify()
    elif args.cmd == "vacuum":
        vacuum(assume_yes=args.yes, force=args.force,
               do_backup=not args.no_backup, as_json=args.json)
    elif args.cmd == "reindex":
        for i in reindex():
            print(f"索引 {i['name']}({i['type']}) 已索引={i['indexed_rows']:,} 未索引={i['unindexed_rows']:,}")
        print("索引重建完成")
    else:
        ap.print_help()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
