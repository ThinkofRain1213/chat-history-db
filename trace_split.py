# -*- coding: utf-8 -*-
"""从 ZCode 会话库（message + part 表）拆分出每一"步"，供 Stop hook 写入 chat-history。

- 不依赖 hook 输入（那里只有最终回复）；直接读 ZCode 持久化的细粒度消息。
- 每步一条，kind 标记：user / mid(中间输出) / tool(工具,只存名+描述) / final(最后文本)。
- reasoning(思考) 与 step-start/finish 标记跳过（剔除思考标记）。
- tool 只存名字与入参描述，不存返回数据。
"""
import json
import os
import sqlite3
from pathlib import Path


def zcode_db_path() -> str:
    """ZCode 会话库路径：优先 ZCODE_DB_PATH 环境变量，否则 user 目录默认值。

    与 title_cache._ZCODE_DB 使用同一环境变量、同一兜底，避免各处硬编码机器路径。
    """
    return os.environ.get(
        "ZCODE_DB_PATH", str(Path.home() / ".zcode" / "cli" / "db" / "db.sqlite")
    )


ZCODE_DB = zcode_db_path()


def split_session(session_id: str, since_seq: int | None = None) -> list[dict]:
    """按 sequence 读该会话的 message+part，产出有序步骤列表。

    返回 [{'kind','role','time','text'}, ...]（reasoning / step标记 已剔除；
    tool 的 text = '工具名: 描述'，不含返回内容）。
    调用方可随后把"最后一条 assistant 文本"标记为 final。
    """
    con = sqlite3.connect(f"file:{zcode_db_path()}?mode=ro", uri=True)
    msgs = con.execute(
        "SELECT id, sequence, data FROM message WHERE session_id=? ORDER BY sequence",
        (session_id,),
    ).fetchall()
    steps: list[dict] = []
    for mid, seq, mdata in msgs:
        if since_seq is not None and seq < since_seq:
            continue
        m = json.loads(mdata)
        role = m.get("role")
        # 跳过系统注入的 user 消息（todo 提醒 / runtime 上下文 / compact 摘要），不是真人输入
        meta = m.get("metadata") or {}
        if role == "user" and (meta.get("visibility") == "model-only"
                               or meta.get("runtimeMessage") or m.get("summary")):
            continue
        created = m.get("time", {}).get("created") if isinstance(m.get("time"), dict) else None
        parts = con.execute(
            "SELECT data FROM part WHERE message_id=? ORDER BY sequence", (mid,)
        ).fetchall()
        textbuf: list[str] = []
        for (pdata,) in parts:
            try:
                d = json.loads(pdata)
            except Exception:
                continue
            ty = d.get("type")
            if ty == "text":
                textbuf.append(d.get("text", ""))
            elif ty == "reasoning":
                continue  # 剔除思考
            elif ty == "tool":
                tool = d.get("tool")
                inp = d.get("state", {}).get("input", {}) if isinstance(d.get("state"), dict) else {}
                desc = inp.get("description") or inp.get("command") or str(inp)
                if textbuf:
                    steps.append({"kind": "mid", "role": role, "time": created,
                                  "text": "".join(textbuf)})
                    textbuf = []
                steps.append({"kind": "tool", "role": role, "time": created,
                              "text": f"{tool}: {desc}"})
            # step-start / step-finish 等类型跳过
        if textbuf:
            txt = "".join(textbuf)
            kind = "user" if role == "user" else "mid"
            steps.append({"kind": kind, "role": role, "time": created, "text": txt})
    con.close()
    return steps


def mark_final(steps: list[dict], final_text: str) -> list[dict]:
    """把与 final_text 匹配的（或最后一条 assistant 文本）标记为 final。"""
    if not steps:
        return steps
    # 优先把内容与 final_text 相同的 mid 改为 final；否则最后一条 assistant mid 改 final
    for s in reversed(steps):
        if s["kind"] == "mid" and (not final_text or s["text"] == final_text):
            s["kind"] = "final"
            return steps
    for s in reversed(steps):
        if s["kind"] == "mid":
            s["kind"] = "final"
            return steps
    return steps
