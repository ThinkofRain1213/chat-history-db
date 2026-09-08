"""Stable public errors and payload-free diagnostics."""
from contextlib import contextmanager
from functools import wraps
import sys
import traceback


class HistoryError(RuntimeError):
    code = "E_INTERNAL"


class InvalidInput(HistoryError, ValueError):
    code = "E_INVALID"


class SchemaError(HistoryError):
    code = "E_SCHEMA"


class DatabaseError(HistoryError):
    code = "E_DATABASE"


class ModelError(HistoryError):
    code = "E_MODEL"


class IndexError(HistoryError):
    code = "E_INDEX"


class NotArchived(HistoryError):
    """删除只针对已归档会话；目标仍在活跃表。"""
    code = "E_NOTARCHIVED"


# 码 → 简短、无载荷的原因（供 _error_msg 拼进 AI 可见的错误串；不含任何消息体/对话内容）。
ERROR_REASONS = {
    "E_INVALID": "输入参数非法",
    "E_SESSIONNOTFOUND": "会话不存在",
    "E_NOTARCHIVED": "会话未归档（删除只针对归档会话，请先 action='archive'）",
    "E_AMBIGUOUSTITLE": "标题重名",
    "E_SCHEMA": "表结构不兼容",
    "E_DATABASE": "数据库操作失败",
    "E_MODEL": "模型加载/推理失败",
    "E_INDEX": "文本索引缺失/创建失败",
    "E_INTERNAL": "内部异常",
}


@contextmanager
def error_boundary(error_type, operation):
    try:
        yield
    except HistoryError:
        raise
    except Exception as exc:
        raise error_type(operation) from exc


def model_operation(operation):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with error_boundary(ModelError, operation):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def log_error(error, operation, code):
    # Exception messages can contain query text; emit frame locations, not values.
    lines = [f"[chat-history] operation={operation} code={code}"]
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        lines.append(f"exception={type(current).__name__}")
        for frame in traceback.extract_tb(current.__traceback__):
            lines.append(f"  {frame.filename}:{frame.lineno} in {frame.name}")
        current = current.__cause__ or current.__context__
    sys.stderr.write("\n".join(lines) + "\n")
