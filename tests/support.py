import gc
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import bgem3_embedding
import core
import db
import lancedb
import reranker
import title_dispatcher
import title_cache
import trace_split


def fake_embed(self, texts):
    vectors = []
    for text in texts:
        vector = np.zeros(1024, dtype=np.float32)
        for word in str(text).lower().split():
            index = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4]) % 1024
            vector[index] += 1
        norm = np.linalg.norm(vector)
        if norm:
            vector /= norm
        vectors.append(vector)
    return vectors


def fake_scores(model_dir, query, passages, max_length=512):
    words = set(query.lower().split())
    return [float(len(words & set(text.lower().split()))) for text in passages]


class IsolatedCase(unittest.TestCase):
    real_models = False

    def setUp(self):
        root = Path.home() / ".agent" / "temp"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="chat-history-tests-", dir=root)
        self.root = Path(temporary.name)
        self.addCleanup(temporary.cleanup)
        self.addCleanup(gc.collect)
        self.patch(patch.dict(os.environ, {
            "CHAT_HISTORY_DB": str(self.root / "chat.db"),
            "TITLE_CACHE_PATH": str(self.root / "titles.json"),
            "ZCODE_DB_PATH": str(self.root / "zcode.sqlite"),
            "CHAT_PENDING_DB": str(self.root / "chat_pending.db"),  # /health 的队列探测也不许读生产队列
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        }))
        self.patch(patch.object(db, "_store", db.Store()))  # 每个用例换一个全新 Store，隔离 DB 状态
        self.patch(patch.object(title_cache, "_CACHE_PATH", str(self.root / "titles.json")))
        self.patch(patch.object(title_cache, "_ZCODE_DB", str(self.root / "zcode.sqlite")))
        # 重置标题缓存的单一映射，避免跨用例残留
        self.patch(patch.object(title_cache, "_FORWARD", None))
        self.patch(patch.object(trace_split, "ZCODE_DB", str(self.root / "zcode.sqlite")))
        connect = lancedb.connect

        def isolated_connect(uri, *args, **kwargs):
            if not Path(uri).resolve().is_relative_to(self.root.resolve()):
                raise AssertionError("Test attempted to access a non-test LanceDB")
            return connect(uri, *args, **kwargs)

        self.patch(patch.object(lancedb, "connect", side_effect=isolated_connect))
        if not self.real_models:
            self.patch(patch.object(bgem3_embedding.BGEM3Embedding, "_embed", fake_embed))
            self.patch(patch.object(reranker, "score", side_effect=fake_scores))
            self.patch(patch.object(bgem3_embedding, "_load", side_effect=AssertionError("Real embedding disabled")))
            self.patch(patch.object(reranker, "_load", side_effect=AssertionError("Real reranker disabled")))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def remember(self, text="alpha message", kind="final", sid="sess_A", **kwargs):
        kwargs.setdefault("session_title", "Test session")
        kwargs.setdefault("time", "2026-09-07 10:00:00")
        return core.remember(sid, text, kind=kind, **kwargs)
