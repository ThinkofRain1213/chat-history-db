# -*- coding: utf-8 -*-
"""ONNX Runtime provider 选择的单一来源。

嵌入（bgem3_embedding）与重排（reranker）共用，避免各自硬编码 providers 导致不一致。
"""

import os


def resolve_providers() -> list[str]:
    """返回 ONNX Runtime 的 provider 顺序列表（先出现的优先）。

    默认 CPU——这两类模型在 7900XT 的 DirectML 上实测比 CPU 慢，CPU 更稳更快。
    可用环境变量 CHAT_HISTORY_PROVIDERS 覆盖，逗号分隔，如
    'DmlExecutionProvider,CPUExecutionProvider'（优先 DML，退 CPU）。
    """
    raw = os.environ.get("CHAT_HISTORY_PROVIDERS", "").strip()
    if raw:
        providers = [p.strip() for p in raw.split(",") if p.strip()]
        if providers:
            if "CPUExecutionProvider" not in providers:
                # 兜底：保证列表里至少有一个可用 provider（DML 缺失时落 CPU，避免 E_MODEL）
                providers.append("CPUExecutionProvider")
            return providers
    return ["CPUExecutionProvider"]
