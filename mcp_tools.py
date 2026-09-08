# -*- coding: utf-8 -*-
"""MCP 传输接线：把领域函数暴露成 MCP 工具。

从 core.py 拆出（B1）——core 只留领域逻辑，不再 import mcp SDK；本模块是全项目唯一
import `mcp.server` 的地方，于是 http_server / db 等模块不再被动依赖 SDK。

工具闭包在这里只做「取参 → 调领域函数 → 把异常转成错误串」，不含业务逻辑。
"""
from __future__ import annotations

import json

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

import archive
import core
import title_dispatcher
from config import ARCHIVE_TABLE, TABLE
from errors import InvalidInput, NotArchived


def _extract_session_id(ctx: Context | None) -> str | None:
    """从工具收到的 Context 里取 ZCode 注入的会话 id（请求 _meta 的 session_id）。

    ZCode 在每次 tools/call 的 _meta 里放 session_id；2.x 里它落在
    ctx.request_context.meta（及其 params._meta）。stdio 下 ServerRequestContext.session_id
    是 MCP 连接会话 id（为 None），不是这个，所以只能从 _meta 取。
    """
    if ctx is None:
        return None
    try:
        srctx = ctx.request_context
    except Exception:
        return None
    if srctx is None:
        return None
    candidates = [getattr(srctx, "meta", None)]
    params = getattr(srctx, "params", None)
    if params is not None:
        candidates.append(getattr(params, "_meta", None))
    for holder in candidates:
        if isinstance(holder, dict):
            sid = holder.get("session_id")
            if sid:
                return sid
            cc = holder.get("com.zcode/request-context")
            if isinstance(cc, dict) and cc.get("session_id"):
                return cc["session_id"]
        elif holder is not None and hasattr(holder, "model_dump"):
            md = holder.model_dump()
            if isinstance(md, dict):
                sid = md.get("session_id")
                if sid:
                    return sid
    return None


def _build_server():
    mcp = MCPServer(
        name="chat-history",
        instructions=(
            "结构化对话历史库 + 强召回。用 'remember' 存消息（kind 默认 final，自动算 bge-m3 向量），"
            "用 'recall' 召回（混合：BM25 精确词 + bge-m3 语义 + RRF 融合 + 跨编码器重排，"
            "可按时间/会话/角色过滤），用 'recent' 取最近消息（时间倒序），用 'list_sessions' 列出会话。"
            "recall/recent/list_sessions 的 source 参数可选 'messages'（默认，活跃表）或 'archive'（归档表）。"
            "用 'session_admin' 管理会话：action='archive' 归档（可恢复）、'restore' 恢复、"
            "'delete' 永久删除（两阶段：先调用一次拿到询问文案，用户确认后在有效期内再调一次才执行）。"
        ),
    )

    @mcp.tool(name="remember", description="存一条内容到对话历史库（kind 默认 final；会话 id、标题、时间、轮次/步骤均自动，也可显式传）。返回 '成功' 或 '失败 [错误码]'。",
              structured_output=False)
    def _remember_tool(text: str, session_id: str | None = None, kind: str = "final",
                       time: str | None = None,
                       session_title: str | None = None,
                       round: int | None = None, step: int | None = None,
                       ctx: Context | None = None) -> str:
        try:
            sid = session_id or _extract_session_id(ctx)
            core.remember(sid, text, kind=kind, time=time,
                     session_title=session_title, round=round, step=step)
            return "成功"
        except Exception as e:  # noqa: BLE001
            return core._error_msg(e, "mcp.remember")

    @mcp.tool(name="recall",
              description=("召回对话消息。"
                           "session：None/空=不限；'current'=仅当前会话；或传具体 sess_xxx。"
                           "source：'messages'（默认，活跃表）/'archive'（归档表）。"
                           "可按 kind/时间过滤，range 支持 '09:00-10:00' / '08-15' / '08-15 09:00-10:00' / '2026'。"),
              structured_output=False)
    def recall_tool(query: str, session: str | None = None,
                    kind: str | None = None, range: str | None = None,
                    limit: int = 5, top_k: int = 30,
                    source: str = "messages",
                    ctx: Context | None = None) -> str:
        try:
            return core.recall(query, session, kind, range, limit, top_k,
                          current_session_id=_extract_session_id(ctx), source=source)
        except Exception as e:  # noqa: BLE001
            return core._error_msg(e, "mcp.recall")

    @mcp.tool(name="recent",
              description=("最近消息按时间倒序（最新在前），不做语义检索。"
                           "session：None/空=不限；'current'=仅当前会话；或传具体 sess_xxx。"
                           "source：'messages'（默认，活跃表）/'archive'（归档表）。"
                           "可按 kind/时间过滤，range 支持 '09:00-10:00' / '08-15' / '08-15 09:00-10:00' / '2026'。"
                           "kind='all' 时默认返回40条，其余默认10条。"),
              structured_output=False)
    def recent_tool(limit: int | None = None, session: str | None = None,
                    kind: str | None = None, range: str | None = None,
                    source: str = "messages",
                    ctx: Context | None = None) -> str:
        try:
            # 默认（limit=None）的 all→40/否则10 由领域函数 search_recent 统一处理
            return core.recent_messages(session, kind, range, limit,
                                   current_session_id=_extract_session_id(ctx), source=source)
        except Exception as e:  # noqa: BLE001
            return core._error_msg(e, "mcp.recent")

    @mcp.tool(name="list_sessions",
              description="列出会话（session_id / 标题 / 消息条数 / 时间范围）。source：'messages'（默认，活跃表）/'archive'（归档表）。",
              structured_output=False)
    def _list_tool(source: str = "messages", ctx: Context | None = None) -> str:
        try:
            return json.dumps(core.list_sessions(source), ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            return core._error_msg(e, "mcp.list_sessions")

    @mcp.tool(name="session_admin",
              description=("会话管理（列出会话请用 list_sessions 工具）。action："
                           "'archive' 归档（可恢复，支持 dry_run=true 只统计）；'restore' 从归档恢复；"
                           "'delete' 永久删除（不可恢复）——删除是两阶段：第一次调用只登记意向并返回询问文案，"
                           "用户确认后在有效期内再次调用同一 action 才执行。"
                           "都需要 session（sess_xxx 或标题）。"),
              structured_output=False)
    def _session_admin_tool(action: str, session: str | None = None,
                            dry_run: bool = False, ctx: Context | None = None) -> str:
        try:
            act = str(action or "").strip().lower()
            if act not in ("archive", "restore", "delete"):
                raise InvalidInput("action 只支持 archive / restore / delete")
            if not session:
                raise InvalidInput(f"action={act} 需要 session（sess_xxx 或标题）")
            sid = title_dispatcher.resolve_session(session, _extract_session_id(ctx))
            if act == "archive":
                result = archive.archive_session(sid, dry_run=dry_run)
                if result.get("dry_run"):
                    return f"试运行：将归档 {result['rows']} 行（未写入）"
                return f"已归档 {result['rows']} 行（可用 action='restore' 恢复）"
            if act == "restore":
                result = archive.restore_session(sid)
                return f"已恢复 {result['rows']} 行"
            info = archive.session_info(sid, ARCHIVE_TABLE)
            if not info["rows"]:
                if archive.count_rows(sid, TABLE):
                    raise NotArchived("删除只针对已归档的会话")
                raise title_dispatcher.SessionNotFound(f"会话不存在: {sid}")
            if not core._delete_gate(sid):
                return core._delete_question(sid, info["title"] or "(无标题)", info["rows"])
            result = archive.delete_session(sid, confirm=True)
            return f"已永久删除 {result['rows']} 行（不可恢复）"
        except Exception as e:  # noqa: BLE001
            return core._error_msg(e, "mcp.session_admin")

    return mcp
