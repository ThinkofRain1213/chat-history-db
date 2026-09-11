# -*- coding: utf-8 -*-
"""bge-m3 本地嵌入：官方 ONNX 导出 + onnxruntime（CPU 执行）。

- 模型：BAAI/bge-m3 (XLM-RoBERTa, 1024 维, 8192 上下文)
- 实测 DirectML(7900XT) 在这类模型上反而比 CPU 慢（bge-m3 批量 6.35s vs CPU 1.43s），故用 CPU。
- ONNX 输出 sentence_embedding（已 CLS pooling 且 L2 归一化）
- 无需 query 指令前缀（官方声明）

多个会话各跑一份 2.2G 模型没有意义，所以 `_embed` 优先把活外包给 17891 hub
（见 model_hub），只有 hub 不可用时才落到本进程的 `_embed_local`。
"""
from pathlib import Path

import numpy as np
import model_hub
from pydantic import field_validator
from onnx_providers import resolve_providers
from errors import model_operation

from lancedb.embeddings import EmbeddingFunction, register

_MODEL_CACHE: dict[str, tuple] = {}

# 被清洗过 NaN/Inf 的向量条数（累计）。由 /health 的 nan_vectors 暴露——MCP 的 stderr 不落盘，
# 只写 stderr 等于没有告警，所以用一个可查询的计数代替。
# 改走 hub 后嵌入大多在 hub 进程里发生，而这个计数只在「本地算过」的进程里增长；
# /health 恰好由 hub 进程回答，所以它读到的正是实际发生嵌入的那个进程的计数。
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

    @field_validator("model_dir", mode="before")
    @classmethod
    def _resolve_model_dir(cls, value) -> str:
        """把不可用的 model_dir 回落到运行时配置（config.MODEL_DIR）。

        LanceDB 会把嵌入函数的参数**冻结进表 schema 元数据**，打开表时用
        `create(**obj["model"])` 重建实例——于是「建表那一刻」的绝对路径跟着库走。
        但 models/ 是部署期资产（不入库、被 .gitignore 忽略，clone 后自行下载），
        副本被移动/重装/换机后这个冻结路径就失效：每次嵌入都在加载模型时抛错，
        LanceDB 还会带着会话锁重试 7 次（指数退避，累计十几分钟），期间所有写入冻结。
        所以凡是路径不可用的（含空值）一律回落到 config.MODEL_DIR；
        传入的路径确实可用时保持原样（测试与显式指定仍生效）。
        """
        from config import MODEL_DIR

        path = str(value or "")
        if path and (Path(path) / "onnx" / "tokenizer.json").exists():
            return path
        return MODEL_DIR

    def ndims(self) -> int:
        return 1024

    def name(self) -> str:
        return "bgem3"

    def lang(self) -> str:
        return "zh,en"

    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        """LanceDB 调用的入口：优先让 hub 代算，hub 不可用才本进程跑 ONNX。

        hub 侧的 /embed 路由调的就是下面的 `_embed_local`，所以两条路径的向量必然一致
        （同一份模型、同一段归一化与 NaN 清洗）。
        """
        texts = list(texts)
        if not model_hub.too_many(texts):
            result = model_hub.post("/embed", {
                "texts": texts,
                "model_dir": self.model_dir,
                "max_length": self.max_length,
            })
            vectors = (result or {}).get("vectors")
            if isinstance(vectors, list) and len(vectors) == len(texts):
                return [np.asarray(v, dtype=np.float32) for v in vectors]
        return self._embed_local(texts)

    @model_operation("embedding inference")
    def _embed_local(self, texts: list[str]) -> list[np.ndarray]:
        """本进程 ONNX 推理（改造前的 _embed 原样搬来）。"""
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
