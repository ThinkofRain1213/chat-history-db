# -*- coding: utf-8 -*-
"""bge-m3 本地嵌入：官方 ONNX 导出 + onnxruntime（CPU 执行）。

- 模型：BAAI/bge-m3 (XLM-RoBERTa, 1024 维, 8192 上下文)
- 实测 DirectML(7900XT) 在这类模型上反而比 CPU 慢（bge-m3 批量 6.35s vs CPU 1.43s），故用 CPU。
- ONNX 输出 sentence_embedding（已 CLS pooling 且 L2 归一化）
- 无需 query 指令前缀（官方声明）
"""
from pathlib import Path

import numpy as np
from onnx_providers import resolve_providers
from errors import model_operation

from lancedb.embeddings import EmbeddingFunction, register

_MODEL_CACHE: dict[str, tuple] = {}

# 被清洗过 NaN/Inf 的向量条数（累计）。由 /health 的 nan_vectors 暴露——MCP 的 stderr 不落盘，
# 只写 stderr 等于没有告警，所以用一个可查询的计数代替。
_NAN_VECTORS = 0


def nan_vectors() -> int:
    """累计清洗掉的 NaN/Inf 向量条数。"""
    return _NAN_VECTORS


def _load(model_dir: str, max_length: int):
    """懒加载并缓存 (onnx session, tokenizer)。避免每次构造都重载 2.3G 模型。"""
    key = (model_dir, max_length)
    if key not in _MODEL_CACHE:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        base = Path(model_dir) / "onnx"
        tokenizer = Tokenizer.from_file(str(base / "tokenizer.json"))
        tokenizer.enable_padding(pad_id=1)
        tokenizer.enable_truncation(max_length=max_length)
        session = ort.InferenceSession(
            str(base / "model.onnx"),
            providers=resolve_providers(),
        )
        _MODEL_CACHE[key] = (session, tokenizer)
    return _MODEL_CACHE[key]


@register("bgem3")
class BGEM3Embedding(EmbeddingFunction):
    """LanceDB 嵌入函数：用 onnxruntime 本地跑官方 bge-m3 ONNX（默认 CPU；可选 DirectML）。"""

    model_dir: str = ""  # 指向含 onnx/xxx 的目录（绝对路径）
    max_length: int = 512  # DML 在 7900XT 上撑不住 8192 的 attention（OOM），对话消息 512 足够

    def ndims(self) -> int:
        return 1024

    def name(self) -> str:
        return "bgem3"

    def lang(self) -> str:
        return "zh,en"

    @model_operation("embedding inference")
    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        session, tokenizer = _load(self.model_dir, self.max_length)
        encs = tokenizer.encode_batch(list(texts))
        input_ids = np.array([e.ids for e in encs], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        outs = session.run(
            None, {"input_ids": input_ids, "attention_mask": attention_mask}
        )
        se = np.asarray(outs[1], dtype=np.float32)  # [batch, 1024]
        n = np.linalg.norm(se, axis=-1, keepdims=True)
        se = se / np.where(n > 0, n, 1.0)  # 防御性归一化（模型本身已归一无妨）
        if not np.isfinite(se).all():
            # 模型偶发输出 NaN/Inf 时 LanceDB 会拒收整行、消息重试后可能被丢弃。
            # 这里清洗成 0 并计数：宁可这一条语义检索退化，也不丢消息。
            global _NAN_VECTORS
            _NAN_VECTORS += int((~np.isfinite(se)).any(axis=-1).sum())
            se = np.nan_to_num(se, nan=0.0, posinf=0.0, neginf=0.0)
        return list(se)

    def compute_query_embeddings(self, q, *a, **k):
        return self._embed(self.sanitize_input(q))

    def compute_source_embeddings(self, d, *a, **k):
        return self._embed(self.sanitize_input(d))


def make_embedding(model_dir: str, max_length: int = 512) -> BGEM3Embedding:
    return BGEM3Embedding.create(model_dir=model_dir, max_length=max_length)
