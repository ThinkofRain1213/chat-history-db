# -*- coding: utf-8 -*-
"""必须落盘的日志通道：ZCode 不保留 MCP 的 stderr，只写 stderr 等于没有告警。

两个来源共用这一条通道：

- **第三方库的重试告警**。LanceDB 的嵌入失败只走 `logging.warning`（root logger → stderr），
  生产上完全不可见。2026-09-10 的事故（表元数据里冻结的模型路径失效 → 每次嵌入失败 →
  指数退避 7 次约 17 分钟）就是因此烧了半小时才被发现的。`setup()` 给 root logger 挂一个
  文件 handler，把 WARNING 及以上落盘。
- **本项目的 `errors.log_error`**：它本来只写 stderr（帧位置，不含消息体），现在同时追加到同一文件。

路径约定与 maintenance / http_server 一致：取 `CHAT_HISTORY_ERROR_LOG`，默认
`~/.agent/hooks/chat_errors.log`。只追加、绝不写 stdout（stdio 是 MCP 协议通道）。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

_DEFAULT_LOG = Path.home() / ".agent" / "hooks" / "chat_errors.log"

# 本模块挂上去的 handler（供 close() 精确摘除；不要改成扫描 root.handlers ——
# 那会误摘别人挂的 handler）。
_INSTALLED: list[logging.Handler] = []


def log_path() -> Path:
    raw = os.environ.get("CHAT_HISTORY_ERROR_LOG", "").strip()
    return Path(raw) if raw else _DEFAULT_LOG


def append(message: str) -> None:
    """追加一行带时间戳的记录。永不抛异常：日志通道不能变成新的故障源。"""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def setup() -> None:
    """给 root logger 挂文件 handler（幂等），让第三方库的 WARNING 落盘。

    幂等靠比对现有 handler 的 baseFilename 与目标路径，而不是模块级标志位——
    测试会在不同临时目录之间切换路径，标志位会让后一次挂载静默失效。
    """
    try:
        target = str(log_path().absolute())
    except OSError:
        return
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler) and str(
                Path(getattr(handler, "baseFilename", "")).absolute()) == target:
            return
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, encoding="utf-8")
    except OSError:
        return
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    _INSTALLED.append(handler)


def close() -> None:
    """摘掉并关闭本模块挂的文件 handler（对称于 setup，可重复调用）。

    生产不需要（进程退出即释放），但**测试必须调用**：Windows 下被 handler 打开的日志文件
    会让用例临时目录清理失败（WinError 32）。`tests/support.py` 已把它挂进每个用例的清理。
    """
    root = logging.getLogger()
    for handler in list(_INSTALLED):
        try:
            root.removeHandler(handler)
        except ValueError:  # 已经不在 root 上
            pass
        handler.close()
    _INSTALLED.clear()
