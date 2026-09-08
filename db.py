# -*- coding: utf-8 -*-
"""数据层：LanceDB 表模型、连接/校验、查询与过滤构造。"""
import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import msvcrt  # Windows：跨进程文件锁
except ImportError:  # pragma: no cover - 本项目的部署目标只有 Windows
    msvcrt = None

import lancedb
from lancedb.pydantic import LanceModel, Vector
from lancedb.query import ColumnOrdering

import bgem3_embedding  # 注册 bgem3 嵌入函数（打开表时 LanceDB 靠它重建嵌入）
from bgem3_embedding import make_embedding
from errors import (HistoryError, InvalidInput, DatabaseError, SchemaError, error_boundary)
from config import MODEL_DIR, TABLE, _BASE

_emb = make_embedding(MODEL_DIR)  # 轻量：只是配置；ONNX 会话懒加载


class Msg(LanceModel):
    session_id: str
    session_title: str
    kind: str
    time: str
    round: int = 0
    step: int = 0
    text: str = _emb.SourceField()
    vector: Vector(1024) = _emb.VectorField()


class Store:
    """封装 LanceDB 连接与路径的模块级唯一状态。

    把 _db/_db_path 两个裸全局收拢到一个对象：单点持有、可 reset。
    测试只需整体替换 _store 即可完全隔离，不再 patch 两个分散的全局。
    """
    def __init__(self):
        self.db = None
        self.db_path = None
        self.db_key = None

    def reset(self):
        self.db = None
        self.db_path = None
        self.db_key = None


_store = Store()

# 进程级写锁：串行化「首个连接 + 写表」，避免 MCP/HTTP 双通道并发写导致 round/step 撞号或重复建表。
# 用 RLock 以允许同一线程在 remember 内再进入 _ensure_db 的连接锁。
_WRITE_LOCK = threading.RLock()


def _db_file() -> str:
    return os.environ.get("CHAT_HISTORY_DB", str(_BASE / "chat.db"))


def _db_canonical(path: str | None = None) -> str:
    """库文件的规范路径：realpath 解析 junction / 符号链接 / 8.3 短名，abspath 展开相对路径与 `..`。

    大小写与斜杠方向保持原样，只用于定位（锁目录名）；做比较键请用 _db_key()。
    """
    return os.path.realpath(os.path.abspath(path or _db_file()))


def _db_key(path: str | None = None) -> str:
    """跨进程/跨连接比较用的库路径键：规范路径再 normcase，抹平大小写与正反斜杠。

    同一个库的任何拼写都必须归一到同一个键：否则跨进程互斥静默失效（实测两个进程分别用
    `C:\\...` 与 `C:/...` 写同一会话时，60 次持锁区间出现 93 对重叠），同一进程也会对同一个库
    重复建连接。
    """
    return os.path.normcase(_db_canonical(path))


def _ensure_db():
    p = _db_file()
    key = _db_key(p)
    if _store.db is None or _store.db_key != key:
        with _WRITE_LOCK:  # 防首个并发连接竞态（双重检查）
            if _store.db is None or _store.db_key != key:
                with error_boundary(DatabaseError, "connect history database"):
                    _store.db = lancedb.connect(p)
                _store.db_path = p
                _store.db_key = key
    return _store.db


def _lock_dir() -> Path:
    """锁目录紧挨库文件：能写这个库的进程，必然能写到同一个锁目录。

    不能用 tempfile.gettempdir()——它按 TMPDIR→TEMP→TMP 取第一个可用值，两个写入进程只要
    这些环境变量不同就会落到不同锁目录，跨进程互斥同样静默失效（与路径拼写不同同一类问题：
    拿进程环境里的字符串当跨进程键，而不是拿目标文件本身当键）。
    """
    return Path(_db_canonical() + ".locks")


def _lock_path(session_id: str) -> Path:
    """按会话分锁文件；目录取自库的规范路径，不同库互不干扰。"""
    digest = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:32]
    return _lock_dir() / f"{digest}.lock"


# 锁文件每个会话一个、零字节；超过上限才清理，只删 7 天没动过的。
# 仍被持有的锁文件删不掉（Windows 拒绝删除已打开的文件），所以清理不会破坏互斥。
_LOCK_KEEP_SEC = 7 * 24 * 3600
_LOCK_MAX_FILES = 500


def _prune_locks(lock_dir: Path) -> None:
    try:
        files = list(lock_dir.glob("*.lock"))
        if len(files) <= _LOCK_MAX_FILES:
            return
        cutoff = time.time() - _LOCK_KEEP_SEC
        for f in files:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass  # 正被别的进程持有 / 权限不足：跳过
    except OSError:
        pass


@contextmanager
def _session_lock(session_id: str):
    """跨进程互斥：同一会话的「读最后一行 → 推导 → 写入」不可重叠。

    作用域是单个 session_id——不同会话互不阻塞，也不规定写入先后顺序；
    只保证两条并发写入不会读到同一条最后一行（否则 round/step 会算出相同值）。
    Windows 用 msvcrt 字节锁，进程退出由系统释放，不会留下死锁文件。
    """
    if msvcrt is None:  # 非 Windows：进程内 _WRITE_LOCK 已足够
        yield
        return
    path = _lock_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _prune_locks(path.parent)
    handle = open(path, "a+b")
    try:
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.02)
        yield
    finally:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        handle.close()


# 新增列的迁移默认值（旧行回填）。cast 到 bigint 以匹配 Msg 的 int 列类型。
_MIGRATION_DEFAULTS = {"round": "cast(0 as bigint)", "step": "cast(0 as bigint)"}


def _migrate_messages_schema(db=None, table: str = TABLE) -> bool:
    """给指定表补上缺失的新列（只做加法、幂等）；表不存在返回 False。

    add_columns 对同一列重复调用会抛 ValueError，所以先比对 schema 再补。
    启动时对 messages 与 messages_archive 各跑一次。
    """
    db = db if db is not None else _ensure_db()
    tbl = _open_table_or_none(db, table)
    if tbl is None:
        return False
    with error_boundary(DatabaseError, "read table schema"):
        existing = {field.name for field in tbl.schema}
    missing = {name: expr for name, expr in _MIGRATION_DEFAULTS.items() if name not in existing}
    if missing:
        with error_boundary(DatabaseError, "migrate messages schema"):
            tbl.add_columns(missing)
    return True


def _validate_messages_schema(db=None) -> bool:
    """启动时校验 messages 表；缺表时允许首次 remember() 创建。"""
    db = db if db is not None else _ensure_db()
    tbl = _open_or_none(db)
    if tbl is None:
        return False

    expected = Msg.to_arrow_schema()
    with error_boundary(DatabaseError, "read table schema"):
        actual = tbl.schema
    expected_fields = {field.name: str(field.type) for field in expected}
    actual_fields = {field.name: str(field.type) for field in actual}
    # 前向兼容：允许表里有附加列（多列），但必备列必须存在且类型一致。
    for name, typ in expected_fields.items():
        if name not in actual_fields:
            raise SchemaError(f"{TABLE} 表缺列 {name}；expected={expected_fields}")
        if actual_fields[name] != typ:
            raise SchemaError(f"{TABLE} 表列 {name} 类型不兼容；expected={typ}, actual={actual_fields[name]}")
    return True


def _open_table_or_none(db, name: str):
    """打开指定表；表不存在返回 None（其它异常按 DatabaseError 抛出）。"""
    try:
        return db.open_table(name)
    except HistoryError:
        raise
    except Exception as exc:
        with error_boundary(DatabaseError, "list history tables"):
            page_token = None
            while True:
                page = db.list_tables(page_token=page_token)
                if name in page.tables:
                    raise DatabaseError(f"open {name} table") from exc
                page_token = page.page_token
                if not page_token:
                    break
        if isinstance(exc, ValueError):
            return None
        raise DatabaseError(f"open {name} table") from exc


def _open_or_none(db):
    return _open_table_or_none(db, TABLE)


def _session_tail(tbl, session_id: str) -> dict | None:
    """返回该会话最后一条的 (kind, round, step)；无记录返回 None。

    round/step 的推导完全基于本会话的最后一条——与全表写入顺序、time 字段无关。
    排序键就是 round/step 本身（2026-09-08 去掉 turn 后）：round 单调递增，同一 round 内
    step 递增，所以 (round, step) 最大者即最后一条。只读三列，过滤/排序/截断下推。
    """
    order = [ColumnOrdering(column_name="round", ascending=False),
             ColumnOrdering(column_name="step", ascending=False)]
    with error_boundary(DatabaseError, "session tail query"):
        q = tbl.search().select(["kind", "round", "step"]).where(
            f"session_id = {_q(session_id)}", prefilter=True
        ).order_by(order).limit(1)
        rows = []
        for batch in q.to_batches(batch_size=1):
            rows.extend(batch.to_pylist())
            if rows:
                break
    return rows[0] if rows else None


def _input_int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidInput(f"{name} must be an integer") from exc


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _recent_rows(tbl, where: str | None, limit: int) -> list[dict]:
    """只读必要列、把过滤下推、排序+截断交给底层，返回最新 limit 条。

    不读取 vector 列（1024 维，recent 用不到）。排序按 (time desc, round desc, step desc)。
    limit 必须 >0：LanceDB 的 limit(<=0) 会退化为返回全表。
    """
    order = [ColumnOrdering(column_name="time", ascending=False),
             ColumnOrdering(column_name="round", ascending=False),
             ColumnOrdering(column_name="step", ascending=False)]
    with error_boundary(DatabaseError, "recent query"):
        q = tbl.search().select(
            ["session_id", "session_title", "kind", "time", "round", "step", "text"]
        ).order_by(order)
        if where:
            q = q.where(where, prefilter=True)
        q = q.limit(limit)
        rows = []
        for batch in q.to_batches(batch_size=limit):
            rows.extend(batch.to_pylist())
            if len(rows) >= limit:
                break
    return rows[:limit]


def _summary_rows(tbl) -> list[dict]:
    """只读会话汇总所需的三列（不含 1024 维 vector），用于 list_sessions 聚合。

    会话的 count/first_time/last_time 必须遍历所有会话才能统计，无法像 recent 那样用 limit
    截断；但只拉 (session_id, session_title, time) 三列，避免把向量列一起读进内存。
    """
    with error_boundary(DatabaseError, "session summary query"):
        q = tbl.search().select(["session_id", "session_title", "time"])
        rows = []
        for batch in q.to_batches(batch_size=10000):
            rows.extend(batch.to_pylist())
    return rows


def _q(value) -> str:
    """把值安全地放进 LanceDB 过滤字符串：单引号转义成两个单引号。

    LanceDB 的 WHERE 是类 SQL 语法，未转义的单引号会让解析抛 ValueError（实测确认），
    并可能造成注入式扩域。session_id/kind/time 都来自调用方，必须转义。
    """
    return "'" + str(value).replace("'", "''") + "'"


def _build_filter(session_id: str | None, kset: set[str] | None,
                  rt: tuple[str, str] | None) -> str | None:
    """统一构造 recall / recent 的 WHERE 片段；所有值经 _q 转义。"""
    conds = []
    if session_id:
        conds.append(f"session_id = {_q(session_id)}")
    if kset:
        conds.append("(" + " OR ".join(f"kind = {_q(k)}" for k in kset) + ")")
    if rt:
        conds.append(f"time >= {_q(rt[0])} AND time < {_q(rt[1])}")
    return " AND ".join(conds) if conds else None


def _kind_set(kind: str | None) -> set[str] | None:
    """kind → set。None/空=默认只看 user/final；'all'=全部；否则按逗号拆多值。"""
    if not kind:
        return {"user", "final"}
    if str(kind).strip().lower() == "all":
        return {"user", "tool", "mid", "final"}
    ks = {k.strip() for k in str(kind).split(",") if k.strip()}
    return ks or None
