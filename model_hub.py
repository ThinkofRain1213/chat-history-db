# -*- coding: utf-8 -*-
"""模型外包客户端：把「算向量 / 算重排分数」交给 17891 hub，复用它已加载的模型。

背景：ZCode 给每个会话起一个独立 MCP 子进程，各自跑嵌入与重排就意味着各自把
bge-m3（2.2G）+ bge-reranker（2.2G）读进自己的内存——几条会话就几份常驻。
而 17891 上的 hub（抢到端口的那个进程）本来就在替 hooks 代写，模型也已经加载，
所以把它的模型能力一并开放（`/embed`、`/rerank` 两个路由），会话进程只发 HTTP。

本模块是**客户端**侧：
- `post()` 成功返回解析后的 dict；hub 不在（没选上端口 / 已退出 / 正在换人）、
  或返回非 200 / 非 `{"ok": true}`，一律返回 None。
- 调用方拿到 None 就回落本地 ONNX，行为与改造前完全一致（只是那时必然本地）。
- 设 `CHAT_HISTORY_MODEL_HUB=0` 可整体关闭（测试默认关闭，避免连上生产 hub）。

注意：hub 侧的路由必须调本地实现（`_embed_local` / `score_local`），
否则会自己 POST 给自己形成递归。
"""
import json
import os
import urllib.request

from config import _HTTP_PORT, MAX_BODY

# 首次调用可能触发 hub 侧加载模型（2.2G 级，数秒）；写路径还握着会话锁，
# 所以不能无限等：超时即回落本地，锁的等待上界另有 db._LOCK_TIMEOUT_SEC 兜底。
_TIMEOUT = 30.0

# 嵌入/重排请求体远小于此（单条文本 + 一个 1024 维向量约 20KB），
# 真正要防的是有人用超长文本把 hub 撑爆。
_MAX_TEXTS = 512


def enabled() -> bool:
    """是否允许走 hub（每次调用现读环境变量，测试可逐用例开关）。"""
    return os.environ.get("CHAT_HISTORY_MODEL_HUB", "1").strip() != "0"


def post(path: str, payload: dict):
    """POST 到 hub 的模型路由；任何失败返回 None（由调用方回落本地）。"""
    if not enabled():
        return None
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if len(data) > MAX_BODY:
        return None
    request = urllib.request.Request(
        f"http://127.0.0.1:{_HTTP_PORT}{path}",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - hub 不可用是正常情形，不当作错误上报
        return None
    return body if isinstance(body, dict) and body.get("ok") else None


def too_many(texts) -> bool:
    """超出单次请求文本条数上限就不外包（避免一次请求体过大）。"""
    return not isinstance(texts, list) or len(texts) > _MAX_TEXTS
