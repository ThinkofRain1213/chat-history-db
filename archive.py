# -*- coding: utf-8 -*-
"""归档 / 恢复 / 删除会话：在 messages 与 messages_archive 之间搬行。

搬行是「读主表 → 写归档表 → 校验 → 删主表」四步、非原子，因此：
- 先写归档表再删主表：中途失败只多一份归档，主表未动，可重跑；
- 搬前先清该会话的旧归档行 → 幂等，重跑不堆重复；
- 全程持 db._WRITE_LOCK + db._session_lock(session_id)，与写入路径互斥。

行原样搬（含 vector），不需要重新嵌入；两表 schema 相同（db.Msg）。
"""
from __future__ import annotations

from lancedb.index import FTS

import db
from config import TABLE, ARCHIVE_TABLE
from errors import InvalidInput, DatabaseError, error_boundary
from title_dispatcher import SessionNotFound
from title_cache import purge as purge_title


def _session_rows(tbl, session_id: str) -> list[dict]:
    """该会话的全部行（含 vector）；无匹配返回 []。"""
    with error_boundary(DatabaseError, "read session rows"):
        q = tbl.search().where(f"session_id = {db._q(session_id)}", prefilter=True)
        rows: list[dict] = []
        for batch in q.to_batches(batch_size=10000):
            rows.extend(batch.to_pylist())
    return rows


def _ensure_archive_table(handle):
    """取归档表；不存在则按 Msg schema 创建（FTS 索引在首次搬入后重建）。"""
    tbl = db._open_table_or_none(handle, ARCHIVE_TABLE)
    if tbl is None:
        with error_boundary(DatabaseError, "create archive table"):
            tbl = handle.create_table(ARCHIVE_TABLE, schema=db.Msg)
    return tbl


def _reindex(tbl) -> None:
    """重建 FTS 索引。

    索引不随 add 自动更新，而 recall(source='archive') 依赖它；不重建则新归档的行
    在关键词分支里搜不到（向量分支仍可命中）。
    """
    if tbl is None or tbl.count_rows() <= 0:
        return
    with error_boundary(DatabaseError, "reindex archive table"):
        tbl.create_index("text", config=FTS(base_tokenizer="icu"), replace=True)

def archive_session(session_id: str, dry_run: bool = False) -> dict:
    """把该会话全部行搬到归档表。dry_run=True 只统计不写入。"""
    session_id = (session_id or "").strip()
    if not session_id:
        raise InvalidInput("session 不能为空")
    handle = db._ensure_db()
    with db._WRITE_LOCK:
        with db._session_lock(session_id):
            main = db._open_or_none(handle)
            rows = _session_rows(main, session_id) if main is not None else []
            if not rows:
                raise SessionNotFound(f"会话不存在或已归档: {session_id}")
            if dry_run:
                return {"session_id": session_id, "rows": len(rows), "dry_run": True}
            arch = _ensure_archive_table(handle)
            with error_boundary(DatabaseError, "archive rows"):
                arch.delete(f"session_id = {db._q(session_id)}")  # 幂等：先清旧归档
                arch.add(rows)
            archived = len(_session_rows(arch, session_id))
            if archived != len(rows):
                raise DatabaseError(
                    f"归档校验失败：期望 {len(rows)} 行，实际 {archived} 行（主表未删，可重跑）"
                )
            with error_boundary(DatabaseError, "delete archived rows from main"):
                main.delete(f"session_id = {db._q(session_id)}")
            _reindex(arch)
    purge_title(session_id)  # 缓存干净；标题仍可经 ZCode 重新学回
    return {"session_id": session_id, "rows": len(rows), "dry_run": False}


def restore_session(session_id: str) -> dict:
    """把归档行搬回主表。"""
    session_id = (session_id or "").strip()
    if not session_id:
        raise InvalidInput("session 不能为空")
    handle = db._ensure_db()
    with db._WRITE_LOCK:
        with db._session_lock(session_id):
            arch = db._open_table_or_none(handle, ARCHIVE_TABLE)
            rows = _session_rows(arch, session_id) if arch is not None else []
            if not rows:
                raise SessionNotFound(f"会话不在归档中: {session_id}")
            main = db._open_or_none(handle)
            if main is None:
                with error_boundary(DatabaseError, "create messages table"):
                    main = handle.create_table(TABLE, data=rows, schema=db.Msg)
                    main.create_index("text", config=FTS(base_tokenizer="icu"))
                moved = len(rows)
            else:
                with error_boundary(DatabaseError, "restore rows"):
                    main.delete(f"session_id = {db._q(session_id)}")
                    main.add(rows)
                moved = len(_session_rows(main, session_id))
                if moved != len(rows):
                    raise DatabaseError(
                        f"恢复校验失败：期望 {len(rows)} 行，实际 {moved} 行（归档未删，可重跑）"
                    )
            with error_boundary(DatabaseError, "delete restored rows from archive"):
                arch.delete(f"session_id = {db._q(session_id)}")
            _reindex(arch)
    return {"session_id": session_id, "rows": moved}


def delete_session(session_id: str, confirm: bool = False) -> dict:
    """从**归档表**永久删除该会话（不可恢复，必须显式 confirm=True）。

    只针对已归档会话：活跃会话必须先 archive_session 移入归档才能删除，
    避免一步销毁还在用的数据。
    """
    if not confirm:
        raise InvalidInput("delete_session 需要 confirm=true（删除不可恢复）")
    session_id = (session_id or "").strip()
    if not session_id:
        raise InvalidInput("session 不能为空")
    handle = db._ensure_db()
    with db._WRITE_LOCK:
        with db._session_lock(session_id):
            arch = db._open_table_or_none(handle, ARCHIVE_TABLE)
            rows = _session_rows(arch, session_id) if arch is not None else []
            if not rows:
                raise SessionNotFound(f"会话不在归档中: {session_id}")
            with error_boundary(DatabaseError, "delete archived rows"):
                arch.delete(f"session_id = {db._q(session_id)}")
    purge_title(session_id)
    return {"session_id": session_id, "rows": len(rows)}


def _count_session_rows(tbl, session_id: str) -> int:
    """过滤下推计数：只数行，不把整会话（含 1024 维向量）读进内存。

    归档/恢复/删除的搬行路径仍走 _session_rows（必须带 vector），这里只服务计数类调用
    （count_rows / session_info / 已归档提示）——它们原先也整行读取，纯浪费。
    """
    with error_boundary(DatabaseError, "count session rows"):
        return int(tbl.count_rows(filter=f"session_id = {db._q(session_id)}"))


def count_rows(session_id: str, table: str = TABLE) -> int:
    """该会话在指定表的行数（供「已归档」提示用）；表不存在返回 0。"""
    tbl = db._open_table_or_none(db._ensure_db(), table)
    if tbl is None:
        return 0
    return _count_session_rows(tbl, session_id)


def session_info(session_id: str, table: str = TABLE) -> dict:
    """该会话的 (rows, title)，供删除确认文案用；不存在时 rows=0、title=""。"""
    tbl = db._open_table_or_none(db._ensure_db(), table)
    if tbl is None:
        return {"rows": 0, "title": ""}
    rows = _count_session_rows(tbl, session_id)
    title = ""
    if rows:
        with error_boundary(DatabaseError, "read session title"):
            q = tbl.search().select(["session_title"]).where(
                f"session_id = {db._q(session_id)}", prefilter=True
            ).limit(1)
            for batch in q.to_batches(batch_size=1):
                got = batch.to_pylist()
                if got:
                    title = str(got[0].get("session_title") or "")
                    break
    return {"rows": rows, "title": title}
