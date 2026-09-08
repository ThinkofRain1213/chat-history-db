# -*- coding: utf-8 -*-
"""嵌入侧防御：模型偶发输出 NaN/Inf 时清洗成 0 并计数，避免整行被 LanceDB 拒收（消息丢失）。"""
import unittest
from unittest.mock import patch

import numpy as np

import bgem3_embedding


class _FakeSession:
    def __init__(self, out):
        self._out = out

    def run(self, *_args, **_kwargs):
        return [None, self._out]


class _FakeEncoding:
    ids = [1, 2, 3]
    attention_mask = [1, 1, 1]


class _FakeTokenizer:
    def encode_batch(self, texts):
        return [_FakeEncoding() for _ in texts]


class EmbeddingSanitizeTests(unittest.TestCase):
    def _run_embed(self, output):
        emb = bgem3_embedding.make_embedding("unused")
        with patch.object(bgem3_embedding, "_load",
                          return_value=(_FakeSession(output), _FakeTokenizer())):
            return emb._embed(["x"])

    def test_nan_and_inf_are_sanitized_and_counted(self):
        out = np.zeros((1, 1024), dtype=np.float32)
        out[0, 0] = np.nan
        out[0, 1] = np.inf
        before = bgem3_embedding.nan_vectors()
        vecs = self._run_embed(out)
        self.assertEqual(len(vecs), 1)
        self.assertTrue(np.isfinite(vecs[0]).all(), "清洗后不应残留 NaN/Inf")
        self.assertEqual(bgem3_embedding.nan_vectors(), before + 1)

    def test_clean_vector_untouched(self):
        out = np.zeros((1, 1024), dtype=np.float32)
        out[0, 5] = 3.0
        before = bgem3_embedding.nan_vectors()
        vecs = self._run_embed(out)
        self.assertAlmostEqual(float(np.linalg.norm(vecs[0])), 1.0, places=5)
        self.assertEqual(bgem3_embedding.nan_vectors(), before)


if __name__ == "__main__":
    unittest.main()
