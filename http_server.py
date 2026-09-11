# -*- coding: utf-8 -*-
"""本地 HTTP 端点：让同机的其它进程复用本进程已加载的模型（只绑 127.0.0.1）。

- `/remember` 给 hooks 代写（复用嵌入模型）；
- `/embed`、`/rerank` 给其它会话的 MCP 进程代算（复用嵌入/重排模型）——
  否则每个会话子进程都会把两份 2.2G 模型读进自己的内存。

抢到端口的进程即 hub（先探测再绑，见 `_start_http_server`）；路由都走本地实现，
绝不回头再调 model_hub（那会自己 POST 给自己）。
"""
import http.server
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import bgem3_embedding
import reranker
from errors import InvalidInput
from config import MAX_BODY, RERANK_DIR, _HTTP_PORT
from db import _input_int
from core import _handle_remember, _health, _error_msg

_DEFAULT_LOG = Path.home() / ".agent" / "hooks" / "chat_http.log"
_RETRY_SEC = 30.0      # 端口被占时后台重试间隔（秒）
_PROBE_TIMEOUT = 1.0   # /health 探测超时（秒）
_MAX_LENGTH_CAP = 8192  # 与 bge 系列模型上下文一致，防止请求把 max_length 抬到离谱的值


def _log(message: str) -> None:
    """同时写 stderr 与日志文件：ZCode 不保留 MCP 的 stderr，没有文件就查不到记录。"""
    line = f"[chat-history] http {message}"
    sys.stderr.write(line + "\n")
    try:
        path = Path(os.environ.get("CHAT_HISTORY_HTTP_LOG", "").strip() or _DEFAULT_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def _http_alive() -> bool:
    """本机端口上是否已有实例在服务（探测 /health）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_HTTP_PORT}/health",
                                    timeout=_PROBE_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - 探测失败一律视为"没有实例"
        return False


def _bind():
    """尝试绑端口；失败返回 None（原因已落日志）。"""
    try:
        return http.server.ThreadingHTTPServer(("127.0.0.1", _HTTP_PORT), _Handler)
    except Exception as e:  # noqa: BLE001
        _log(f"端口 {_HTTP_PORT} 绑定失败: {e}")
        return None


def _serve(srv) -> None:
    threading.Thread(target=srv.serve_forever, daemon=True, name="chat-history-http").start()


def _retry_until_bound() -> None:
    """端口被别的实例占着时在后台等：它退出后接管，避免本进程永久失去 HTTP 通道。"""
    while True:
        time.sleep(_RETRY_SEC)
        if _http_alive():
            continue
        srv = _bind()
        if srv is not None:
            _log(f"已接管 127.0.0.1:{_HTTP_PORT}（前一个实例已退出）")
            _serve(srv)
            return


def _max_length(payload: dict) -> int:
    """请求里的 max_length；不合法就退回 512（内部回环接口，不值得为它报错）。"""
    try:
        value = _input_int(payload.get("max_length", 512), "max_length")
    except Exception:  # noqa: BLE001
        return 512
    return max(1, min(_MAX_LENGTH_CAP, value))


def _bad_request(scope: str, message: str):
    return 400, {"ok": False, "error": _error_msg(InvalidInput(message), scope)}


def _embed_route(payload: dict):
    """代算嵌入向量：与本地路径同一份模型、同一段归一化/清洗代码，所以结果必然一致。"""
    texts = payload.get("texts")
    if not isinstance(texts, list) or not texts:
        return _bad_request("http.embed", "texts must be a non-empty list")
    try:
        embedding = bgem3_embedding.make_embedding(
            str(payload.get("model_dir") or ""), _max_length(payload))
        vectors = embedding._embed_local([str(t) for t in texts])
    except Exception as exc:  # noqa: BLE001 - 如实回报，由调用方回落本地
        return 500, {"ok": False, "error": _error_msg(exc, "http.embed")}
    return 200, {"ok": True, "vectors": [v.tolist() for v in vectors]}


def _rerank_route(payload: dict):
    """代算重排分数。model_dir 不可用时回落到本进程的 RERANK_DIR。"""
    query = payload.get("query")
    passages = payload.get("passages")
    if not isinstance(query, str) or not isinstance(passages, list) or not passages:
        return _bad_request("http.rerank", "query must be a string and passages a non-empty list")
    given = str(payload.get("model_dir") or "")
    model_dir = given if given and (Path(given) / "model.onnx").exists() else RERANK_DIR
    try:
        scores = reranker.score_local(
            model_dir, query, [str(p) for p in passages], _max_length(payload))
    except Exception as exc:  # noqa: BLE001
        return 500, {"ok": False, "error": _error_msg(exc, "http.rerank")}
    return 200, {"ok": True, "scores": [float(s) for s in scores]}


_ROUTES = {
    "/remember": _handle_remember,
    "/embed": _embed_route,
    "/rerank": _rerank_route,
}


class _Handler(http.server.BaseHTTPRequestHandler):
    """本地 HTTP 端点：给同机进程一个复用本进程模型的通道（入口只绑 127.0.0.1）。"""

    # 钉死 HTTP/1.0：每个响应后关闭连接。若将来升 1.1(keep-alive)，务必先处理好
    # 「未读尽的请求体」(do_POST 对超限 body 直接 413 而不读剩余字节) 以免帧错位。
    protocol_version = "HTTP/1.0"

    def _send(self, code: int, obj: dict, close: bool = False) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        if close:
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/healthz"):
            self._send(200, _health())
        else:
            self._send(404, {"ok": False, "error": "not found"}, close=True)

    def do_POST(self) -> None:  # noqa: N802
        handler = _ROUTES.get(self.path)
        if handler is None:
            self._send(404, {"ok": False, "error": "not found"}, close=True)
            return
        length = _input_int(self.headers.get("Content-Length", 0), "Content-Length")
        if length < 0:
            self._send(400, {"ok": False, "error": _error_msg(InvalidInput("negative Content-Length"), "http.remember")}, close=True)
            return
        if length > MAX_BODY:
            # 用头部长度判定，不真实读入超大 body，直接拒绝（413 + E_INVALID），并关闭连接防 keep-alive 帧错位
            self._send(413, {"ok": False, "error": _error_msg(InvalidInput("request body too large"), "http.remember")}, close=True)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeError) as exc:
            error = InvalidInput("invalid JSON request")
            error.__cause__ = exc
            self._send(400, {"ok": False, "error": _error_msg(error, "http.decode")})
            return
        code, body = handler(payload)
        self._send(code, body)

    def log_message(self, *args) -> None:  # 静默 http.server 默认日志
        pass


def _start_http_server():
    """在 MCP 进程内起一个后台线程的本地 HTTP 服务。

    - 绑 127.0.0.1，不对外暴露。
    - 与 stdio MCP 同进程 → 复用同一份模型缓存。
    - 随 MCP 进程（=ZCode 子进程）退出而关闭：ZCode 关闭时进程被杀，端口即释放。
    - **先探测再绑**：Windows 的 SO_REUSEADDR 会让第二个进程"绑定成功"而不报错（实测），
      两个实例同时监听行为不确定；探测到已有实例就不绑，转后台重试，等它退出再接管。
    - 模型懒加载：HTTP 端口随启动即监听（不依赖模型），模型在首次 remember/recall 时才加载。
    """
    if _http_alive():
        _log(f"127.0.0.1:{_HTTP_PORT} 已有实例在服务：本进程不绑端口（hooks 走它），"
             f"后台每 {int(_RETRY_SEC)}s 探测，它退出后接管")
        threading.Thread(target=_retry_until_bound, daemon=True,
                         name="chat-history-http-retry").start()
        return None
    srv = _bind()
    if srv is None:
        threading.Thread(target=_retry_until_bound, daemon=True,
                         name="chat-history-http-retry").start()
        return None
    _log(f"HTTP 已监听 127.0.0.1:{_HTTP_PORT}")
    _serve(srv)
    return srv
