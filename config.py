# -*- coding: utf-8 -*-
"""集中配置：路径与环境变量常量（无状态，只读）。"""
import datetime as _dt
import os
from pathlib import Path

_BASE = Path(__file__).resolve().parent  # 项目根（config.py 即位于项目根）
MODEL_DIR = os.environ.get("CHAT_HISTORY_MODEL_DIR") or str(_BASE / "models" / "bge-m3-onnx")
RERANK_DIR = os.environ.get("CHAT_HISTORY_RERANK_DIR") or str(_BASE / "models" / "bge-reranker-v2-m3-onnx")
TABLE = "messages"
ARCHIVE_TABLE = "messages_archive"  # 归档表：与 TABLE 同 schema，搬行归档用
_HTTP_PORT = int(os.environ.get("CHAT_HISTORY_PORT", "17891"))  # 供 hooks 复用本进程模型的端口（随 MCP 进程退出而关闭）
_TZ8 = _dt.timezone(_dt.timedelta(hours=8))
_TIME_FMT = "%Y-%m-%d %H:%M:%S"
MAX_LIMIT = 200   # 最终返回条数硬上限
MAX_TOP_K = 500   # 候选池硬上限（>= MAX_LIMIT，保证 top_k>=limit 时不会越界）
MAX_BODY = 1_000_000  # HTTP 请求体大小上限（字节）；仅防护 HTTP/本地端点防内存耗尽，MCP 主通道(stdio)不限体量
