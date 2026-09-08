# -*- coding: utf-8 -*-
"""缓存与数据源（d）：拥有标题缓存 + 查 ZCode。

- 缓存：单一映射 id -> title（title_cache.json 平铺），正反向都由它推导。
- 读源：fetch_from_zcode(id) / find_ids_by_title(title)（查 ZCode，供 c 校验用）。
- 写缓存：set_id_title / learn_title（内存改 + 原子写盘）。
- 只此一处持有缓存状态与写盘逻辑；a(编排)、c(校验) 都经它读写。
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from contextlib import closing

from errors import DatabaseError, error_boundary

_CACHE_PATH = os.environ.get("TITLE_CACHE_PATH", str(Path(__file__).parent / "title_cache.json"))
_ZCODE_DB = os.environ.get("ZCODE_DB_PATH", str(Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"))


def cache_path() -> str:
    return _CACHE_PATH


# ----------------------------------------------------------------------------
# 读源（ZCode）
# ----------------------------------------------------------------------------

def fetch_from_zcode(session_id: str) -> str:
    with error_boundary(DatabaseError, "read ZCode session title"):
        with closing(sqlite3.connect(f"file:{_ZCODE_DB}?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT title FROM session WHERE id = ?", (session_id,)).fetchone()
            return str(row["title"]) if row and row["title"] else ""


def find_ids_by_title(title: str) -> list[str]:
    with error_boundary(DatabaseError, "resolve ZCode session title"):
        with closing(sqlite3.connect(f"file:{_ZCODE_DB}?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT id FROM session WHERE title = ?", (title,)).fetchall()
            return [str(r["id"]) for r in rows]


# ----------------------------------------------------------------------------
# 缓存状态与原子写
# ----------------------------------------------------------------------------

# 单一映射 id -> title（正反向均由它推导；无单独反向表）。
_FORWARD: dict[str, str] | None = None
_CACHE_LOCK = threading.RLock()


def _load() -> dict[str, str]:
    global _FORWARD
    if _FORWARD is None:
        with _CACHE_LOCK:
            if _FORWARD is None:  # 双重检查，防首次并发竞态
                try:
                    with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        _FORWARD = raw["forward"] if isinstance(raw.get("forward"), dict) else raw
                    else:
                        _FORWARD = {}
                except Exception as e:  # noqa: BLE001  缓存非关键路径；失败仅留一行无内容日志
                    sys.stderr.write(f"[chat-history] title cache load failed: {type(e).__name__}\n")
                    _FORWARD = {}
    return _FORWARD


def _save(cache: dict) -> None:
    """原子写缓存：先写同目录临时文件，再 os.replace 原子替换，避免中断留下半截文件。"""
    try:
        d = os.path.dirname(_CACHE_PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".title_cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
            os.replace(tmp, _CACHE_PATH)  # 同目录下原子替换
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[chat-history] title cache save failed: {type(e).__name__}\n")


def _persist(mutator) -> None:
    """锁内执行 mutator，若返回 True 则原子写盘一次。"""
    with _CACHE_LOCK:
        fwd = _load()
        if mutator(fwd):
            _save(fwd)


# ----------------------------------------------------------------------------
# 对外读写（a/c 用）
# ----------------------------------------------------------------------------

def get(session_id: str) -> str | None:
    """读 b：id → title 值；未命中返回 None。"""
    return _load().get(session_id)


def ids_for_title(title: str) -> list[str] | None:
    """反查（b 推导）：返回 title 对应的 id 列表；该 title 不在缓存则返回 None（未命中）。

    先持锁把映射快照成 list 再遍历：_persist 的 mutator 在锁内原地增删同一个 dict，
    无锁迭代会在 HTTP 线程与 MCP 线程并发时抛 RuntimeError: dictionary changed size。
    """
    with _CACHE_LOCK:
        snapshot = list(_load().items())
    ids = [sid for sid, t in snapshot if t == title]
    return ids if ids else None


def set_id_title(session_id: str, title: str) -> None:
    """写 b：id → title（空标题则移除该键，避免垃圾条目）。"""
    def mutator(fwd: dict[str, str]) -> bool:
        if title == "":
            if session_id in fwd:
                del fwd[session_id]
                return True
            return False
        if fwd.get(session_id) != title:
            fwd[session_id] = title
            return True
        return False
    _persist(mutator)


def learn_title(ids: list[str], value: str) -> None:
    """写 b：把 (id -> title) 学进缓存（resolve 反向学到），供反查推导。"""
    def mutator(fwd: dict[str, str]) -> bool:
        changed = False
        for sid in ids:
            if fwd.get(sid) != value:
                fwd[sid] = value
                changed = True
        return changed
    _persist(mutator)


def purge(session_id: str) -> None:
    """移除单个会话的缓存键（归档/删除会话后调用）。"""
    def mutator(fwd: dict[str, str]) -> bool:
        if session_id in fwd:
            del fwd[session_id]
            return True
        return False
    _persist(mutator)


def drop_empty() -> int:
    """清掉空标题的垃圾键，返回删除条数。"""
    removed = [0]

    def mutator(fwd: dict[str, str]) -> bool:
        empties = [sid for sid, title in fwd.items() if not str(title).strip()]
        for sid in empties:
            del fwd[sid]
        removed[0] = len(empties)
        return bool(empties)

    _persist(mutator)
    return removed[0]
