# -*- coding: utf-8 -*-
"""模型外包：会话子进程把嵌入/重排交给 17891 hub，避免每个会话各加载一份 2.2G 模型。

三层都盖到：客户端（model_hub）→ 调用方回落（_embed / score）→ hub 侧路由（/embed、/rerank）。
"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import numpy as np

import bgem3_embedding
import http_server
import model_hub
import reranker


def fake_local_vectors(self, texts):
    return [np.full(1024, 0.5, dtype=np.float32) for _ in texts]


class _RouteServer:
    """起一个真实的 _Handler（监听随机端口），用来打 /embed 与 /rerank。"""

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server._Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))


class ModelHubClientTests(unittest.TestCase):
    def test_disabled_by_env(self):
        with patch.dict("os.environ", {"CHAT_HISTORY_MODEL_HUB": "0"}):
            self.assertFalse(model_hub.enabled())
            self.assertIsNone(model_hub.post("/embed", {"texts": ["x"]}))

    def test_returns_none_when_nothing_listens(self):
        with patch.object(model_hub, "_HTTP_PORT", 1):  # 端口 1 上不会有服务
            self.assertIsNone(model_hub.post("/embed", {"texts": ["x"]}))

    def test_oversized_and_non_list_batches_are_not_outsourced(self):
        self.assertTrue(model_hub.too_many(["x"] * (model_hub._MAX_TEXTS + 1)))
        self.assertFalse(model_hub.too_many(["x"]))
        self.assertTrue(model_hub.too_many("not-a-list"))


class ModelRouteTests(unittest.TestCase):
    """hub 侧：两个新路由必须走本地实现（不能回调 model_hub，否则递归）。"""

    def test_embed_route_returns_local_vectors(self):
        with _RouteServer() as server, \
             patch.object(bgem3_embedding.BGEM3Embedding, "_embed_local", fake_local_vectors):
            status, body = server.post("/embed", {"texts": ["a", "b"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["vectors"]), 2)
        self.assertEqual(len(body["vectors"][0]), 1024)

    def test_embed_route_rejects_missing_texts(self):
        with _RouteServer() as server:
            status, body = server.post("/embed", {})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_embed_route_reports_local_failure(self):
        with _RouteServer() as server, \
             patch.object(bgem3_embedding.BGEM3Embedding, "_embed_local",
                          side_effect=RuntimeError("boom")):
            status, body = server.post("/embed", {"texts": ["a"]})
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])

    def test_rerank_route_returns_scores(self):
        seen = {}

        def fake_scores(model_dir, query, passages, max_length=512):
            seen["passages"] = passages
            return [0.5] * len(passages)

        with _RouteServer() as server, patch.object(reranker, "score_local", side_effect=fake_scores):
            status, body = server.post("/rerank", {"query": "q", "passages": ["a", "b"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["scores"], [0.5, 0.5])
        self.assertEqual(seen["passages"], ["a", "b"])

    def test_rerank_route_rejects_missing_passages(self):
        with _RouteServer() as server:
            status, body = server.post("/rerank", {"query": "q"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_unknown_path_is_404(self):
        with _RouteServer() as server:
            status, body = server.post("/nope", {})
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])


class CallerFallbackTests(unittest.TestCase):
    """hub 不可用时必须回落本地 ONNX —— 行为与改造前完全一致。"""

    def test_embed_uses_hub_when_available(self):
        hub = [[0.0] * 1024, [1.0] * 1024]
        with patch.object(model_hub, "post", return_value={"ok": True, "vectors": hub}) as post, \
             patch.object(bgem3_embedding.BGEM3Embedding, "_embed_local",
                          side_effect=AssertionError("有 hub 就不该走本地")):
            vectors = bgem3_embedding.make_embedding("unused")._embed(["a", "b"])
        post.assert_called_once()
        self.assertEqual(len(vectors), 2)
        self.assertAlmostEqual(float(vectors[1][0]), 1.0)

    def test_embed_falls_back_when_hub_missing(self):
        with patch.object(model_hub, "post", return_value=None), \
             patch.object(bgem3_embedding.BGEM3Embedding, "_embed_local", autospec=True,
                          side_effect=fake_local_vectors) as local:
            vectors = bgem3_embedding.make_embedding("unused")._embed(["a"])
        local.assert_called_once()
        self.assertEqual(len(vectors), 1)

    def test_embed_ignores_hub_reply_with_wrong_count(self):
        with patch.object(model_hub, "post", return_value={"ok": True, "vectors": [[0.0] * 1024]}), \
             patch.object(bgem3_embedding.BGEM3Embedding, "_embed_local", fake_local_vectors):
            vectors = bgem3_embedding.make_embedding("unused")._embed(["a", "b"])
        self.assertEqual(len(vectors), 2)

    def test_embed_does_not_outsource_oversized_batch(self):
        many = ["x"] * (model_hub._MAX_TEXTS + 1)
        with patch.object(model_hub, "post", side_effect=AssertionError("超量不该外包")), \
             patch.object(bgem3_embedding.BGEM3Embedding, "_embed_local", fake_local_vectors):
            vectors = bgem3_embedding.make_embedding("unused")._embed(many)
        self.assertEqual(len(vectors), len(many))

    def test_rerank_uses_hub_when_available(self):
        with patch.object(model_hub, "post", return_value={"ok": True, "scores": [0.9, 0.1]}), \
             patch.object(reranker, "score_local", side_effect=AssertionError("有 hub 就不该走本地")):
            scores = reranker.score("unused", "q", ["a", "b"])
        self.assertEqual(scores, [0.9, 0.1])

    def test_rerank_falls_back_when_hub_missing(self):
        with patch.object(model_hub, "post", return_value=None), \
             patch.object(reranker, "score_local", return_value=[1.0]) as local:
            scores = reranker.score("unused", "q", ["a"])
        local.assert_called_once()
        self.assertEqual(scores, [1.0])

    def test_rerank_falls_back_when_hub_reports_error(self):
        with patch.object(model_hub, "post", return_value=None), \
             patch.object(reranker, "score_local", return_value=[2.0]):
            self.assertEqual(reranker.score("unused", "q", ["a"]), [2.0])


if __name__ == "__main__":
    unittest.main()
