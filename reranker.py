# -*- coding: utf-8 -*-
"""bge-reranker-v2-m3 cross-encoder 二阶段重排（onnxruntime-directml / 7900 XT）。

输入 (query, passage) 对，输出相关度分数（越大越相关）。
用法：score(model_dir, query, [passages]) -> list[float]

同嵌入：`score` 优先让 17891 hub 代算（复用同机唯一那份重排模型），
hub 不可用才落回本进程的 `score_local`。
"""
from pathlib import Path

import numpy as np
import model_hub
from onnx_providers import resolve_providers
from errors import model_operation

_RERANK_CACHE: dict[tuple, tuple] = {}


def _load(model_dir: str, max_length: int):
    key = (model_dir, max_length)
    if key not in _RERANK_CACHE:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        base = Path(model_dir)
        tokenizer = Tokenizer.from_file(str(base / "tokenizer.json"))
        tokenizer.enable_padding(pad_id=1)
        tokenizer.enable_truncation(max_length=max_length)
        session = ort.InferenceSession(
            str(base / "model.onnx"),
            providers=resolve_providers(),  # 跨编码器默认 CPU：DirectML 上异常慢(7.9s)且会挂起；CPU ~1.2s 更稳更快
        )
        _RERANK_CACHE[key] = (session, tokenizer)
    return _RERANK_CACHE[key]


def score(model_dir: str, query: str, passages: list[str], max_length: int = 512) -> list[float]:
    """打分入口：优先让 hub 代算，hub 不可用才本进程跑 ONNX。"""
    passages = [str(p) for p in passages]
    if not model_hub.too_many(passages):
        result = model_hub.post("/rerank", {
            "query": query,
            "passages": passages,
            "model_dir": model_dir,
            "max_length": max_length,
        })
        scores = (result or {}).get("scores")
        if isinstance(scores, list) and len(scores) == len(passages):
            return [float(s) for s in scores]
    return score_local(model_dir, query, passages, max_length)


@model_operation("reranker inference")
def score_local(model_dir: str, query: str, passages: list[str], max_length: int = 512) -> list[float]:
    """本进程 ONNX 打分（改造前的 score 原样搬来）。"""
    session, tokenizer = _load(model_dir, max_length)
    pairs = [(query, str(p)) for p in passages]
    encs = tokenizer.encode_batch(pairs)
    input_ids = np.array([e.ids for e in encs], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
    logits = session.run(
        None, {"input_ids": input_ids, "attention_mask": attention_mask}
    )[0].reshape(-1)
    return [float(x) for x in logits]


def rerank_candidates(model_dir: str, query: str, candidates: list[dict],
                      text_key: str = "text", top_n: int | None = None):
    """把 candidates（list[dict]）按交叉编码器分数降序重排。

    返回 (new_order_list, scores)：new_order 是重排后的 candidates 列表。
    """
    texts = [c[text_key] for c in candidates]
    scores = score(model_dir, query, texts)
    order = sorted(range(len(candidates)), key=lambda i: -scores[i])
    reranked = [candidates[i] for i in order]
    ordered_scores = [scores[i] for i in order]
    if top_n is not None:
        reranked = reranked[:top_n]
        ordered_scores = ordered_scores[:top_n]
    return reranked, ordered_scores
