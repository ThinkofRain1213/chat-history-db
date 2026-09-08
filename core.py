# -*- coding: utf-8 -*-
"""核心领域：写入/检索/列出会话，以及错误助手、健康检查、MCP 工具接线。"""
import json
import os
import threading
import time
from contextlib import closing
from pathlib import Path

from lancedb.index import FTS
from lancedb.rerankers import RRFReranker

import bgem3_embedding
import reranker
import title_dispatcher
from errors import (HistoryError, InvalidInput, DatabaseError, ModelError,
                    IndexError as HistoryIndexError, error_boundary, log_error, ERROR_REASONS)

from config import MODEL_DIR, RERANK_DIR, TABLE, ARCHIVE_TABLE, MAX_LIMIT, MAX_TOP_K, _HTTP_PORT
from timeutil import _parse_time_range, _cur_time_str
from db import (Msg, _ensure_db, _open_or_none, _open_table_or_none, _validate_messages_schema,
                _session_tail, _session_lock, _recent_rows, _summary_rows,
                _build_filter, _kind_set, _input_int, _clamp, _WRITE_LOCK)
import archive


def _error_code(e: Exception) -> str:
    """Only explicitly classified errors are part of the public contract."""
    if isinstance(e, title_dispatcher.SessionNotFound):
        return "E_SESSIONNOTFOUND"
    if isinstance(e, title_dispatcher.AmbiguousTitle):
        return "E_AMBIGUOUSTITLE"
    if isinstance(e, HistoryError):
        return e.code
    return "E_INTERNAL"


def _error_msg(e: Exception, operation: str = "tool") -> str:
    code = _error_code(e)
    log_error(e, operation, code)
    return f"失败 [{code}]：{ERROR_REASONS.get(code, code)}"


_SOURCES = {"messages": TABLE, "archive": ARCHIVE_TABLE}


def _source_table(db_handle, source: str):
    """按 source 取表（messages=活跃表 / archive=归档表）；非法值报 E_INVALID，表不存在返回 None。"""
    key = str(source or "messages").strip().lower()
    if key not in _SOURCES:
        raise InvalidInput("source 只支持 'messages'（默认）或 'archive'")
    return _open_table_or_none(db_handle, _SOURCES[key])


def _other_table_hint(session: str | None, current_session_id: str | None, source: str) -> str:
    """目标表无结果时，若另一张表有该会话，返回可操作提示；否则空串。"""
    if not session:
        return ""
    try:
        session_id = title_dispatcher.resolve_session(session, current_session_id)
        key = str(source or "messages").strip().lower()
        other = "messages" if key == "archive" else "archive"
        count = archive.count_rows(session_id, _SOURCES[other])
    except Exception:  # noqa: BLE001  提示是增强信息，失败就当没有
        return ""
    if not count:
        return ""
    if other == "archive":
        return (f"该会话已归档（{count} 行），当前查的是 messages 表。"
                f"请用 source='archive' 检索，或 restore_session 恢复。")
    return (f"该会话在活跃表（{count} 行），当前查的是 archive 表。"
            f"请用 source='messages' 检索。")


_DELETE_TTL = int(os.environ.get("CHAT_HISTORY_DELETE_TTL", "60") or "60")
_delete_intents: dict[str, float] = {}  # session_id -> 意向到期时间（进程内存，重启即清空）
_delete_lock = threading.Lock()
_INTENT_MAX = 64  # 意向表软上限：超过就顺手清掉已过期的，避免被放弃的意向永久驻留

def _ttl_display() -> str:
    if _DELETE_TTL >= 60 and _DELETE_TTL % 60 == 0:
        return f"{_DELETE_TTL // 60} 分钟"
    return f"{_DELETE_TTL} 秒"


def _delete_question(session_id: str, title: str, rows: int) -> str:
    """第一次删除调用返回的固定文案：含给询问工具的完整结构化参数。"""
    ask = {
        "questions": [{
            "question": f"是否确认永久删除会话「{title}」？删除后无法恢复。",
            "header": "删除确认",
            "multiSelect": False,
            "options": [
                {"label": "确认删除", "description": f"{session_id} · {rows} 行 · 删除后不可恢复"},
                {"label": "取消", "description": "保留该会话，不做任何改动"},
            ],
        }]
    }
    return (
        "【待确认 · 永久删除已归档会话】\n"
        f"会话：{title}\n"
        f"ID：{session_id}\n"
        f"消息：{rows} 行\n"
        "影响：删除后不可恢复（该会话当前在归档表中）\n"
        "\n"
        "请调用询问工具（ZCode 下为 AskUserQuestion），参数如下——不要把这段 JSON 直接发给用户：\n"
        f"{json.dumps(ask, ensure_ascii=False, indent=2)}\n"
        "\n"
        f"- 用户选择「确认删除」后，请再次调用 session_admin(action=delete, session=...)，本次意向 {_ttl_display()}后失效；\n"
        "- 用户选择「取消」或未确认时，请不要再次调用。"
    )


def _delete_gate(session_id: str) -> bool:
    """两阶段删除闸门：第一次调用登记意向并返回 False；有效期内再次调用放行并立即关闭。

    状态只在进程内存、重启即清空（宁可要求重新确认）。只覆盖删除——归档/恢复可逆，不走这道关。
    """
    now = time.time()
    with _delete_lock:
        if len(_delete_intents) > _INTENT_MAX:  # 用户放弃确认的意向不会自己消失，超上限时回收
            for sid, deadline in list(_delete_intents.items()):
                if now >= deadline:
                    del _delete_intents[sid]
        deadline = _delete_intents.get(session_id)
        if deadline is None or now >= deadline:
            _delete_intents[session_id] = now + _DELETE_TTL
            return False
        del _delete_intents[session_id]
        return True


def _derive_round_step(tail: dict | None, kind: str) -> tuple[int, int]:
    """按「该会话最后一条」推导 (round, step)。

    - 用户消息：开新轮（round + 1），无步骤（step = 0）。
    - agent 消息（mid/tool/final）：归属最后一个用户轮次，轮内步骤 + 1。
    只看本会话最后一条，与全表写入顺序、time 无关。会话开头若先有 agent 消息（尚无用户轮次），
    这些行落在 round=0（展示为 `#0.step`），直到第一条用户消息开出 round=1。
    """
    base_round = int(tail["round"]) if tail else 0
    if kind == "user":
        return base_round + 1, 0
    return base_round, (int(tail["step"]) + 1 if tail else 1)


def _ensure_messages_table(db_handle):
    """确保 messages 表存在且带 FTS 索引；并发首建时容忍别人抢先建好。

    两个进程同时首建时，后到者的 create_table 会抛"表已存在"。这时改用别人建好的表，
    而不是让整次写入失败（此前靠队列重试自愈，但首建窗口内会白失败一次）。
    """
    tbl = _open_or_none(db_handle)
    if tbl is not None:
        return tbl
    try:
        with error_boundary(DatabaseError, "create messages table"):
            tbl = db_handle.create_table(TABLE, schema=Msg)
    except Exception:  # noqa: BLE001 - 可能是并发首建；下面确认表是否已存在
        tbl = _open_or_none(db_handle)
        if tbl is None:
            raise
    with error_boundary(HistoryIndexError, "create text FTS index"):
        if not any(i.index_type == "FTS" and "text" in i.columns for i in tbl.list_indices()):
            tbl.create_index("text", config=FTS(base_tokenizer="icu"))
    return tbl


def remember(session_id: str, text: str, kind: str = "final",
             time: str | None = None,
             session_title: str | None = None,
             round: int | None = None, step: int | None = None) -> dict:
    """存一条消息。写入会发生一次同步的 bge-m3 向量推理（单条约 1~2s，CPU）。

    权衡：向量推理发生在写锁内（问题 3），故并发 remember 会在推理上串行排队；
    这是为了让同一会话 round/step 不撞号、避免并发写冲突的取舍。若需高并发写吞吐，
    可改为锁外预计算向量（显式 _emb._embed）再带 vector 写入。
    """
    session_id = session_id or "default"  # wrapper 已解析当前会话；兜底 default
    text = str(text)
    kind = kind or "final"
    if round is not None:
        round = _input_int(round, "round")
    if step is not None:
        step = _input_int(step, "step")
    if not session_title:
        try:
            session_title = title_dispatcher.get_title(session_id)  # 分流：直接读缓存→未命中查ZCode写入缓存
        except Exception as e:  # 标题是增强字段：查不到则降级为空标题，不阻断写入
            log_error(e, "title.besteffort", _error_code(e))
            session_title = ""
    time = str(time) if time else _cur_time_str()  # 可读时间 "YYYY-MM-DD HH:MM:SS"（北京时间）
    db = _ensure_db()
    # MCP(stdio) 与 HTTP(hook) 双通道可能并发写：进程级写锁串行化「建表 + 写表」；
    # 跨进程的同会话竞态由 _session_lock 兜住。
    with _WRITE_LOCK:
        tbl = _ensure_messages_table(db)
        # 同一会话的「读最后一行 → 推导 → 写入」必须原子，否则两条并发写会算出相同的 round/step。
        # 表对象必须在锁内重新打开：LanceDB 表对象绑定的是打开那一刻的快照，锁外打开的那份
        # 会停在别人提交之前的版本（实测同一对象读到旧行、重新 open_table 才看到新行），
        # 于是"读了旧尾行"照样撞号——文件锁只保证互斥，保证不了读到最新版本。
        with _session_lock(session_id):
            tbl = _open_or_none(db) or tbl
            tail = _session_tail(tbl, session_id)
            if round is None or step is None:
                d_round, d_step = _derive_round_step(tail, kind)
                round = d_round if round is None else round
                step = d_step if step is None else step
            row = dict(session_id=session_id, session_title=session_title or "",
                       kind=str(kind), time=time,
                       round=int(round), step=int(step), text=text)
            with error_boundary(DatabaseError, "append message"):
                tbl.add([row])
    return {"session_id": session_id, "kind": str(kind),
            "time": time, "round": int(round), "step": int(step),
            "inserted": True}


def search_recall(query: str, session: str | None = None, kind: str | None = None,
                  range: str | None = None,
                  limit: int = 5, top_k: int = 30,
                  current_session_id: str | None = None,
                  source: str = "messages") -> tuple[list[dict], bool]:
    """召回并重排，返回 (结构化结果列表, scoped)。

    source: 'messages'（默认，活跃表）/ 'archive'（归档表）——一次只查一张表。
    结果每条含 session_id/session_title/kind/time/round/step/text/score；不含任何展示用字符串拼装。
    scoped = 是否按单会话限定（决定展示层要不要带标题/会话 id）。
    limit<=0 → 返回空（与 search_recent 同一语义：要 0 条就是 0 条，不钳到 1，也不读表）。
    """
    query = str(query)
    limit = _clamp(_input_int(limit, "limit"), 0, MAX_LIMIT)
    top_k = _clamp(_input_int(top_k, "top_k"), 1, MAX_TOP_K)
    top_k = max(top_k, limit)  # 候选池必须 >= 最终条数，否则 recall 给不出 limit 条
    # session：None/空=不限；'current'=当前会话；标准 sess_ id；非标准=按标题匹配（可能抛 SessionNotFound/AmbiguousTitle）
    session_id = title_dispatcher.resolve_session(session, current_session_id)
    scoped = session_id is not None  # 传了 session → 只留轮次；不限 → 标题+id
    if limit <= 0:
        return [], scoped
    db = _ensure_db()
    tbl = _source_table(db, source)
    if tbl is None:
        return [], scoped
    with error_boundary(DatabaseError, "inspect search indexes"):
        indexes = tbl.list_indices()
    if not any(i.index_type == "FTS" and "text" in i.columns for i in indexes):
        raise HistoryIndexError("missing text FTS index")
    rt = _parse_time_range(range) if range else None
    if range and rt is None:
        raise InvalidInput(
            "range 格式错误，支持：'09:00-10:00' / '09:00' / '08-15' / "
            "'08-15 09:00-10:00'（月-日前缀可选，纯时间默认今天）"
        )
    where = _build_filter(session_id, _kind_set(kind), rt)
    with error_boundary(DatabaseError, "hybrid search"):
        q = tbl.search(query, query_type="hybrid", fts_columns=["text"]).rerank(RRFReranker())
        if where:
            q = q.where(where, prefilter=True)
        cands = q.limit(top_k).to_arrow().to_pylist()
    if not cands:
        return [], scoped
    with error_boundary(ModelError, "rerank candidates"):
        reranked, scores = reranker.rerank_candidates(RERANK_DIR, query, cands, top_n=limit)
    rows = []
    for c, s in zip(reranked, scores):
        rows.append({
            "session_id": c["session_id"], "session_title": c["session_title"] or "",
            "kind": c["kind"], "time": c["time"],
            "round": c.get("round", 0), "step": c.get("step", 0),
            "text": str(c["text"]), "score": round(float(s), 3),
        })
    return rows, scoped


def _ref(c: dict) -> str:
    """消息定位串：用户消息 `#round`，agent 消息 `#round.step`。

    会话开头尚无用户轮次时 round=0，显示为 `#0.step`（如 `#0.3`）——去 turn 之前这里回退 `#turn`。
    """
    rnd = int(c.get("round") or 0)
    step = int(c.get("step") or 0)
    if rnd <= 0:
        return f"#0.{step}" if step > 0 else "#0"
    return f"#{rnd}" if step <= 0 else f"#{rnd}.{step}"


def _format_recall(rows: list[dict], scoped: bool, source: str = "messages") -> str:
    mark = " | archive" if str(source or "").strip().lower() == "archive" else ""
    lines = []
    for c in rows:
        text = c["text"]
        score = c["score"]
        if scoped:
            # 传了 session → 只留轮次（标题/id 都冗余）
            lines.append(f"[{_ref(c)} | {c['kind']} | {c['time']} | score={score}{mark}] {text}")
        else:
            title = c["session_title"] or ""
            if title:
                # 标题打头，id 在其后；标题为空则退化为用 id
                lines.append(f"[{title} {_ref(c)} | {c['session_id']} | {c['kind']} | {c['time']} | score={score}{mark}] {text}")
            else:
                lines.append(f"[{c['session_id']} {_ref(c)} | {c['kind']} | {c['time']} | score={score}{mark}] {text}")
    return "\n".join(lines)


def recall(query: str, session: str | None = None, kind: str | None = None,
           range: str | None = None,
           limit: int = 5, top_k: int = 30,
           current_session_id: str | None = None, source: str = "messages") -> str:
    rows, scoped = search_recall(query, session, kind, range, limit, top_k,
                                 current_session_id, source)
    if not rows:
        hint = _other_table_hint(session, current_session_id, source)
        if hint:
            return hint
    return _format_recall(rows, scoped, source)


def list_sessions(source: str = "messages") -> list[dict]:
    db = _ensure_db()
    tbl = _source_table(db, source)
    if tbl is None:
        return []
    rows = _summary_rows(tbl)
    sess: dict[str, dict] = {}
    for r in rows:
        sid = r["session_id"]
        s = sess.setdefault(sid, {
            "session_id": sid, "session_title": r["session_title"] or "",
            "count": 0, "first_time": r["time"], "last_time": r["time"],
        })
        s["count"] += 1
        s["last_time"] = max(s["last_time"], r["time"])  # 可读时间串可直接比较
        s["first_time"] = min(s["first_time"], r["time"])  # 修复乱序导入时 first_time 不准
        if not s["session_title"] and r["session_title"]:
            s["session_title"] = r["session_title"]
    return sorted(sess.values(), key=lambda s: s["last_time"], reverse=True)


def search_recent(session: str | None = None, kind: str | None = None,
                  range: str | None = None, limit: int | None = None,
                  current_session_id: str | None = None,
                  source: str = "messages") -> tuple[list[dict], bool]:
    """取最近的会话消息（按 time/round/step 倒序），返回 (结构化结果列表, scoped)。

    source: 'messages'（默认，活跃表）/ 'archive'（归档表）——一次只查一张表。
    不做语义检索，纯按 time/round/step 排序。字段：session_id/session_title/kind/time/round/step/text。
    limit=None 时按 kind 取默认：kind='all' → 40，否则 10。规则单一来源在此。
    limit<=0 → 返回空（与 search_recall 同一语义：要 0 条就是 0 条）。
    scoped = 是否按单会话限定（决定展示层要不要带标题/会话 id）。
    """
    # session：None/空=不限；'current'=当前会话；标准 sess_ id；非标准=按标题匹配（可能抛 SessionNotFound/AmbiguousTitle）
    session_id = title_dispatcher.resolve_session(session, current_session_id)
    scoped = session_id is not None  # 传了 session → 只留轮次；不限 → 标题+id
    db = _ensure_db()
    tbl = _source_table(db, source)
    if tbl is None:
        return [], scoped
    rt = _parse_time_range(range) if range else None
    if range and rt is None:
        raise InvalidInput(
            "range 格式错误，支持：'09:00-10:00' / '09:00' / '08-15' / "
            "'08-15 09:00-10:00'（月-日前缀可选，纯时间默认今天）"
        )
    if limit is None:
        limit = 40 if str(kind or "").strip().lower() == "all" else 10
    limit = _clamp(_input_int(limit, "limit"), 0, MAX_LIMIT)
    if limit <= 0:
        return [], scoped
    where = _build_filter(session_id, _kind_set(kind), rt)
    out = _recent_rows(tbl, where, limit)
    rows = [{
        "session_id": c["session_id"], "session_title": c["session_title"] or "",
        "kind": c["kind"], "time": c["time"],
        "round": c.get("round", 0), "step": c.get("step", 0), "text": str(c["text"]),
    } for c in out]
    return rows, scoped


def _format_recent(rows: list[dict], scoped: bool, source: str = "messages") -> str:
    mark = " | archive" if str(source or "").strip().lower() == "archive" else ""
    lines = []
    for c in rows:
        if scoped:
            lines.append(f"[{_ref(c)} | {c['kind']} | {c['time']}{mark}] {c['text']}")
        else:
            title = c["session_title"] or ""
            if title:
                lines.append(f"[{title} {_ref(c)} | {c['session_id']} | {c['kind']} | {c['time']}{mark}] {c['text']}")
            else:
                lines.append(f"[{c['session_id']} {_ref(c)} | {c['kind']} | {c['time']}{mark}] {c['text']}")
    return "\n".join(lines)


def recent_messages(session: str | None = None, kind: str | None = None,
                    range: str | None = None, limit: int | None = None,
                    current_session_id: str | None = None,
                    source: str = "messages") -> str:
    """最近的消息，按时间倒序（最新在前）。不做语义检索，纯按 time/round/step 排序。

    session: None/空=不限；'current'=当前会话(wrapper 传 current_session_id)；其他=按标题/会话解析。
    source: 'messages'（默认，活跃表）/ 'archive'（归档表）。
    可按 kind / range（同 recall 的格式，kind 为多值/all、缺省只看 user/final）过滤。返回紧凑单行，正文全文不截断。
    limit=None 时按 kind 取默认（all→40，否则 10），规则见 search_recent。
    """
    rows, scoped = search_recent(session, kind, range, limit, current_session_id, source)
    if not rows:
        hint = _other_table_hint(session, current_session_id, source)
        if hint:
            return hint
    return _format_recent(rows, scoped, source)


def _handle_remember(payload: dict) -> tuple[int, dict]:
    """处理 hook 的 /remember 请求：原样入库（不剥离），复用本进程模型。"""
    try:
        if not isinstance(payload, dict):
            raise InvalidInput("request body must be an object")
        content = payload.get("content") or payload.get("text")
        if not content:
            raise InvalidInput("missing content")
        res = remember(
            payload.get("session_id"),
            content,
            kind=payload.get("kind") or "final",
            time=payload.get("time"),
            session_title=payload.get("session_title"),
            round=payload.get("round"),
            step=payload.get("step"),
        )
        return 200, {"ok": True, "inserted": bool(res.get("inserted")),
                     "round": res.get("round"), "step": res.get("step")}
    except Exception as e:  # noqa: BLE001
        status = 400 if isinstance(e, InvalidInput) else 500
        return status, {"ok": False, "error": _error_msg(e, "http.remember")}


def _queue_status() -> dict:
    """hooks 写入队列的状态（供 /health 按需查看）：ERROR 条数 + pending/processing 积压。

    只读打开 `~/.agent/hooks/chat_pending.db`（`CHAT_PENDING_DB` 可覆盖，与 tools/backup.py 同约定）。
    文件不存在、表不存在或读失败一律返回 None，绝不抛异常——health 不能因为队列不可读而失败；
    None 表示"读不到"，0 表示"读到了且为空"。
    """
    path = os.environ.get("CHAT_PENDING_DB") or str(
        Path.home() / ".agent" / "hooks" / "chat_pending.db")
    if not os.path.exists(path):
        return {"error": None, "pending": None}
    try:
        import sqlite3
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)) as con:
            err = int(con.execute(
                "SELECT COUNT(*) FROM chat_pending WHERE status='ERROR'").fetchone()[0])
            pend = int(con.execute(
                "SELECT COUNT(*) FROM chat_pending WHERE status IN ('pending','processing')"
            ).fetchone()[0])
        return {"error": err, "pending": pend}
    except Exception:  # noqa: BLE001  队列不可读不影响 health 其余部分
        return {"error": None, "pending": None}


def _health() -> dict:
    """健康检查：读取各依赖真实状态，自检不抛异常。

    ok 只反映硬依赖（库能连 + 表 schema 合法）；FTS/模型目录/写入队列是软信号，只影响
    recall 或写入可观测性，如实上报但不拉低 ok——否则会在仅退化 recall、其余功能正常的
    机器上误报宕机。
    """
    try:
        db = _ensure_db()
        db_status = "ok"
    except Exception:  # noqa: BLE001  health 必须不抛
        db, db_status = None, "err"
    try:
        schema = _validate_messages_schema(db) if db is not None else False
    except Exception:  # noqa: BLE001
        schema = False
    fts = False
    if db is not None:
        try:
            tbl = _open_or_none(db)
            if tbl is not None:
                fts = any(i.index_type == "FTS" and "text" in i.columns for i in tbl.list_indices())
        except Exception:  # noqa: BLE001
            fts = False
    queue = _queue_status()  # 一次只读查询，两个字段共用
    return {
        "ok": (db_status == "ok") and schema,
        "port": _HTTP_PORT,
        "db": db_status,
        "schema": schema,
        "fts": fts,
        # 累计被清洗过 NaN/Inf 的向量条数；>0 说明嵌入出现过异常输入（详见 bgem3_embedding._embed）
        "nan_vectors": bgem3_embedding.nan_vectors(),
        "embeddings": (Path(MODEL_DIR) / "onnx" / "model.onnx").exists(),  # 嵌入要 MODEL_DIR/onnx/model.onnx
        "reranker": (Path(RERANK_DIR) / "model.onnx").exists(),  # 重排要 RERANK_DIR/model.onnx
        # hooks 写入队列（软信号，按需查）：失败条数 / 待入库积压；None = 队列文件读不到
        "queue_error": queue["error"],
        "queue_pending": queue["pending"],
    }
