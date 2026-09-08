# -*- coding: utf-8 -*-
"""本地 HTTP 端点：给 hooks 复用一个复用 MCP 本进程模型的通道（只绑 127.0.0.1）。"""
import http.server
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from errors import InvalidInput
from config import MAX_BODY, _HTTP_PORT
from db import _input_int
from core import _handle_remember, _health, _error_msg

_DEFAULT_LOG = Path.home() / ".agent" / "hooks" / "chat_http.log"
_RETRY_SEC = 30.0      # 端口被占时后台重试间隔（秒）
_PROBE_TIMEOUT = 1.0   # /health 探测超时（秒）


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


class _Handler(http.server.BaseHTTPRequestHandler):
    """本地 HTTP 端点：给 hooks 一个复用 MCP 本进程模型的通道（入口只绑 127.0.0.1）。"""

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
        if self.path != "/remember":
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
        code, body = _handle_remember(payload)
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
