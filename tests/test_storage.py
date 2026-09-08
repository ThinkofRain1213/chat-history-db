import os
import json
import socket
import sqlite3
import threading
import unittest
import http.client
import http.server
from pathlib import Path
from unittest.mock import Mock, patch
import pyarrow as pa
import title_dispatcher
import title_cache
import trace_split
import onnx_providers
import core
import db
from tests.support import IsolatedCase
from lancedb.index import FTS
import config
import db as dbmod
import errors
import http_server
import mcp_server
import mcp_tools
import reranker


class SchemaTests(IsolatedCase):
    def test_missing_table_is_allowed(self):
        self.assertFalse(dbmod._validate_messages_schema())

    def test_current_schema_is_allowed(self):
        dbmod._ensure_db().create_table(config.TABLE, schema=dbmod.Msg)
        self.assertTrue(dbmod._validate_messages_schema())

    def test_legacy_role_is_rejected(self):
        fields = [pa.field("role", f.type) if f.name == "kind" else f
                  for f in dbmod.Msg.to_arrow_schema()]
        dbmod._ensure_db().create_table(config.TABLE, schema=pa.schema(fields))
        with self.assertRaisesRegex(RuntimeError, "缺列 kind"):
            dbmod._validate_messages_schema()

    def test_wrong_types_and_dimensions_are_rejected(self):
        for name, wrong_type in (("round", pa.string()), ("vector", pa.list_(pa.float32(), 8))):
            fields = [pa.field(f.name, wrong_type) if f.name == name else f
                      for f in dbmod.Msg.to_arrow_schema()]
            db = Mock()
            db.open_table.return_value.schema = pa.schema(fields)
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                dbmod._validate_messages_schema(db)

    def test_unreadable_existing_table_is_not_missing(self):
        db = Mock()
        db.open_table.side_effect = OSError("broken table")
        db.list_tables.return_value.tables = [config.TABLE]
        with self.assertRaises(errors.DatabaseError) as caught:
            dbmod._validate_messages_schema(db)
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_schema_failure_prevents_startup(self):
        with patch.object(dbmod, "_validate_messages_schema", side_effect=RuntimeError("schema")), \
                patch.object(http_server, "_start_http_server") as http, \
                patch.object(mcp_tools, "_build_server") as mcp:
            with self.assertRaises(RuntimeError):
                mcp_server.main()
            http.assert_not_called()
            mcp.assert_not_called()

    def test_startup_order(self):
        calls = Mock()
        with patch.object(dbmod, "_validate_messages_schema", calls.validate), \
                patch.object(http_server, "_start_http_server", calls.http), \
                patch.object(mcp_tools, "_build_server", calls.build):
            mcp_server.main()
        self.assertEqual([c[0] for c in calls.mock_calls],
                         ["validate", "http", "build", "build().run"])
        calls.build.return_value.run.assert_called_once_with(transport="stdio")

    def test_facade_has_no_reexports(self):
        """B2 护栏：门面只保留 main()，符号一律从归属模块 import（不再 re-export）。

        历史上这里导出过 40+ 个名字，导致「patch 门面不生效」「对外符号面由测试决定」。
        """
        self.assertTrue(hasattr(mcp_server, "main"))
        for name in ("remember", "recall", "_validate_messages_schema", "Msg", "TABLE",
                     "lancedb", "FTS", "_start_http_server", "_build_server", "_extract_session_id"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(mcp_server, name), f"mcp_server 不应再 re-export {name}")


class StorageTests(IsolatedCase):
    def test_first_write_and_append(self):
        self.assertEqual(self.remember(kind="user")["round"], 1)
        self.assertEqual(self.remember()["step"], 1)
        table = dbmod._ensure_db().open_table(config.TABLE)
        self.assertEqual(table.count_rows(), 2)
        self.assertTrue(dbmod._validate_messages_schema())
        self.assertTrue(any(i.index_type == "FTS" and i.columns == ["text"] for i in table.list_indices()))

    def test_append_to_precreated_empty_table(self):
        table = dbmod._ensure_db().create_table(config.TABLE, schema=dbmod.Msg)
        table.create_index("text", config=FTS(base_tokenizer="icu"))
        self.assertTrue(self.remember()["inserted"])
        self.assertIn("alpha message", core.recent_messages())

    def test_explicit_round_step_are_preserved_and_scoped(self):
        first = self.remember(kind="user", round=20, step=0)
        self.assertEqual((first["round"], first["step"]), (20, 0))
        self.assertEqual(self.remember()["step"], 1)  # 自动：尾行 (20,0) → (20,1)
        self.assertEqual(self.remember(sid="sess_B", kind="user")["round"], 1)  # 另一会话独立

    def test_session_tail_reads_only_three_columns(self):
        self.remember(kind="user", round=2, step=0)
        tbl = dbmod._ensure_db().open_table(config.TABLE)
        tail = dbmod._session_tail(tbl, "sess_A")
        self.assertEqual((tail["round"], tail["step"], tail["kind"]), (2, 0, "user"))
        self.assertNotIn("vector", tail)
        self.assertIsNone(dbmod._session_tail(tbl, "sess_missing"))

    def test_empty_database(self):
        self.assertEqual(core.recent_messages(), "")
        self.assertEqual(core.list_sessions(), [])
        self.assertEqual(core.recall("alpha"), "")

    def test_remember_without_title_survives_missing_zcode_db(self):
        # 隔离环境的 ZCODE_DB_PATH 指向不存在的 sqlite；不传 session_title 时应降级为空标题而非写失败
        res = core.remember("sess_X", "hello without title")
        self.assertEqual(res["inserted"], True)
        rows = core.list_sessions()
        self.assertEqual(rows[0]["session_id"], "sess_X")
        self.assertEqual(rows[0]["session_title"], "")

    def test_concurrent_remember_round_step_are_unique(self):
        import concurrent.futures
        n = 8

        def put(i):
            return core.remember("sess_conc", f"msg {i}", session_title="Conc")

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(put, range(n)))
        rows = core.search_recent(session="sess_conc", limit=100)[0]
        keys = [(r["round"], r["step"]) for r in rows]
        self.assertEqual(len(set(keys)), n)  # 无重复 (round, step)
        self.assertEqual(sorted(r["step"] for r in rows), list(range(1, n + 1)))  # 无用户轮 → round=0，step 1..n

    def test_recent_kind_filters_and_tie_order(self):
        for kind in ("user", "mid", "tool", "final"):
            self.remember(text=kind + " body", kind=kind)
        result = core.recent_messages(session="sess_A")
        self.assertIn("user body", result)
        self.assertIn("final body", result)
        self.assertNotIn("tool body", result)
        self.assertNotIn("mid body", result)
        all_rows = core.recent_messages(session="sess_A", kind="all")
        self.assertEqual(len(all_rows.splitlines()), 4)
        self.assertTrue(all_rows.startswith("[#1.3 | final"))
        self.assertEqual(len(core.recent_messages(kind="tool,mid").splitlines()), 2)

    def test_recent_time_boundaries_and_session(self):
        self.remember("before", time="2026-09-07 08:59:59")
        self.remember("at_start", time="2026-09-07 09:00:00")
        self.remember("at_end", time="2026-09-07 10:00:00")
        self.remember("other", sid="sess_B", time="2026-09-07 09:30:00")
        result = core.recent_messages(session="sess_A", range="2026-09-07 09:00-10:00")
        self.assertIn("at_start", result)
        for excluded in ("before", "at_end", "other"):
            self.assertNotIn(excluded, result)

    def test_recent_limit_and_global_labels(self):
        self.remember("first")
        self.remember("second")
        result = core.recent_messages(limit=1)
        self.assertEqual(len(result.splitlines()), 1)
        self.assertIn("sess_A", result)
        self.assertIn("Test session", result)
        self.assertIn("second", result)

    def test_session_counts_and_last_time(self):
        self.remember(time="2026-09-07 09:00:00")
        self.remember(time="2026-09-07 10:00:00")
        self.remember(sid="sess_B", time="2026-09-07 11:00:00")
        rows = core.list_sessions()
        self.assertEqual([r["session_id"] for r in rows], ["sess_B", "sess_A"])
        self.assertEqual(rows[1]["count"], 2)
        self.assertEqual(rows[1]["first_time"], "2026-09-07 09:00:00")
        self.assertEqual(rows[1]["last_time"], "2026-09-07 10:00:00")

    def test_first_time_handles_out_of_order_import(self):
        self.remember(time="2026-09-07 10:00:00")
        self.remember(time="2026-09-07 09:00:00")
        self.assertEqual(core.list_sessions()[0]["first_time"], "2026-09-07 09:00:00")

    def test_recall_real_hybrid_filters(self):
        self.remember("alpha final", kind="final")
        self.remember("alpha user", kind="user")
        self.remember("alpha other", sid="sess_B")
        result = core.recall("alpha", session="sess_A", kind="final", top_k=5, limit=1)
        self.assertIn("alpha final", result)
        self.assertIn("score=", result)
        self.assertNotIn("alpha user", result)
        self.assertNotIn("alpha other", result)

    def test_recall_time_filter_and_empty_candidates(self):
        self.remember("alpha today")
        self.remember("alpha yesterday", time="2026-09-06 10:00:00")
        result = core.recall("alpha", range="2026-09-07", top_k=5)
        self.assertIn("alpha today", result)
        self.assertNotIn("alpha yesterday", result)
        with patch.object(reranker, "rerank_candidates") as rank:
            self.assertEqual(core.recall("alpha", session="sess_missing"), "")
            rank.assert_not_called()

    def test_invalid_range(self):
        self.remember()
        with self.assertRaises(ValueError):
            core.recent_messages(range="invalid")
        with self.assertRaises(ValueError):
            core.recall("alpha", range="invalid")

    def test_recent_query_pushes_filters_and_orders_globally(self):
        for sid, time, rnd, step, kind in (
                ("sess_B", "2026-09-07 11:00", 1, 0, "final"),
                ("sess_A", "2026-09-07 10:00", 1, 0, "final"),
                ("sess_A", "2026-09-07 12:00", 2, 0, "user"),
                ("sess_A", "2026-09-07 12:00", 1, 0, "user")):
            self.remember(f"m{sid}{time}{rnd}{step}", sid=sid, time=time,
                          round=rnd, step=step, kind=kind)
        query = dbmod._ensure_db().open_table(config.TABLE)
        where = dbmod._build_filter("sess_A", {"user", "final"}, None)
        rows = dbmod._recent_rows(query, where, 2)
        self.assertEqual([(r["round"], r["step"]) for r in rows], [(2, 0), (1, 0)])
        self.assertTrue(all(r["session_id"] == "sess_A" for r in rows))
        for r in rows:
            self.assertNotIn("vector", r)

    def test_recent_query_obey_time_boundaries_and_empty_session(self):
        self.remember("at_start", time="2026-09-07 09:00:00")
        self.remember("at_end", time="2026-09-07 10:00:00")
        tbl = dbmod._ensure_db().open_table(config.TABLE)
        rows = dbmod._recent_rows(tbl, dbmod._build_filter(None, {"final"}, ("2026-09-07 09:00:00", "2026-09-07 10:00:00")), 5)
        self.assertEqual([r["text"] for r in rows], ["at_start"])
        self.assertEqual(dbmod._recent_rows(tbl, dbmod._build_filter("sess_missing", None, None), 5), [])

class BoundTests(IsolatedCase):
    """item 09：limit/top_k 边界钳位，防止 top_k<=0 触发 LanceDB 退化全表读取。"""

    def test_clamp_helper(self):
        self.assertEqual(dbmod._clamp(100000, 1, 200), 200)
        self.assertEqual(dbmod._clamp(-5, 1, 200), 1)
        self.assertEqual(dbmod._clamp(7, 1, 200), 7)

    def test_recall_top_k_zero_does_not_read_full_table(self):
        # 若 top_k=0 未被钳到 1，q.limit(0) 会退化为全表读取（3 条）；钳位后只取 1 条候选。
        for i in range(3):
            self.remember("alpha msg %d" % i)
        with patch.object(reranker, "rerank_candidates", return_value=([], [])) as rank:
            core.recall("alpha", top_k=0, limit=1)
            cands = rank.call_args[0][2]
            top_n = rank.call_args.kwargs["top_n"]
        self.assertEqual(len(cands), 1)  # 候选被钳到 1，未读全表
        self.assertEqual(top_n, 1)

    def test_recall_clamps_huge_values(self):
        for i in range(5):
            self.remember("alpha msg %d" % i)
        with patch.object(reranker, "rerank_candidates", return_value=([], [])) as rank:
            core.recall("alpha", top_k=10 ** 9, limit=10 ** 9)
            cands = rank.call_args[0][2]
            top_n = rank.call_args.kwargs["top_n"]
        self.assertLessEqual(len(cands), config.MAX_TOP_K)
        self.assertEqual(top_n, config.MAX_LIMIT)

    def test_recall_top_k_raised_to_limit(self):
        for i in range(5):
            self.remember("alpha msg %d" % i)
        with patch.object(reranker, "rerank_candidates", return_value=([], [])) as rank:
            core.recall("alpha", top_k=1, limit=3)
            cands = rank.call_args[0][2]
            top_n = rank.call_args.kwargs["top_n"]
        self.assertEqual(top_n, 3)
        self.assertEqual(len(cands), 3)  # top_k 被抬到 limit=3

    def test_recent_limit_clamped(self):
        self.remember("one")
        self.assertEqual(len(core.recent_messages(limit=10 ** 9).splitlines()), 1)

    def test_zero_or_negative_limit_returns_empty_for_both(self):
        """C3：limit<=0 在 recall 与 recent 上语义一致——要 0 条就是 0 条（不钳到 1、不读表）。"""
        for i in range(3):
            self.remember("alpha msg %d" % i)
        for value in (0, -5):
            with self.subTest(value=value):
                self.assertEqual(core.search_recall("alpha", limit=value)[0], [])
                self.assertEqual(core.search_recent(limit=value)[0], [])
                self.assertEqual(core.recall("alpha", limit=value), "")
                self.assertEqual(core.recent_messages(limit=value), "")


class FilterSafetyTests(IsolatedCase):
    """item 10：过滤值转义，防止单引号破坏语法/注入扩域。"""

    def test_q_escapes_single_quote(self):
        self.assertEqual(dbmod._q("a'b"), "'a''b'")
        self.assertEqual(dbmod._q("sess_x"), "'sess_x'")

    def test_build_filter_escapes_all_values(self):
        f = dbmod._build_filter("a'b", {"user", "x'y"}, ("09:00:00", "10:00:00"))
        self.assertIn("session_id = 'a''b'", f)
        self.assertIn("kind = 'x''y'", f)
        self.assertIn("time >= '09:00:00'", f)
        self.assertIn("time < '10:00:00'", f)
        self.assertIsNone(dbmod._build_filter(None, None, None))

    def test_recent_scopes_quote_session_id(self):
        self.remember("quote sid row", sid="sess_a'b", time="2026-09-07 10:00:00")
        self.remember("normal row", sid="sess_normal", time="2026-09-07 11:00:00")
        out = core.recent_messages(session="sess_a'b")
        self.assertIn("quote sid row", out)
        self.assertNotIn("normal row", out)

    def test_recall_scopes_quote_session_id(self):
        self.remember("quote sid rec", sid="sess_a'b", kind="final")
        self.remember("normal rec", sid="sess_normal", kind="final")
        out = core.recall("quote", session="sess_a'b", kind="final")
        self.assertIn("quote sid rec", out)
        self.assertNotIn("normal rec", out)

    def test_session_tail_scopes_quote_session_id(self):
        self.remember(sid="sess_a'b", round=3, step=0, kind="user")
        self.remember(sid="sess_normal", round=9, step=0, kind="user")
        tbl = dbmod._ensure_db().open_table(config.TABLE)
        self.assertEqual(dbmod._session_tail(tbl, "sess_a'b")["round"], 3)


class CacheWriteTests(IsolatedCase):
    """item 11：标题缓存原子写入。"""

    def test_save_writes_readable_cache_and_cleans_tmp(self):
        target = Path(title_cache.cache_path())
        title_cache._save({"sess_a": "标题", "sess_b": "x"})
        self.assertTrue(target.exists())
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["sess_a"], "标题")
        self.assertEqual(data["sess_b"], "x")
        leftovers = [p for p in target.parent.iterdir() if p.name.startswith(".title_cache-")]
        self.assertEqual(leftovers, [])

    def test_get_title_validates_each_call_and_updates_on_change(self):
        # 每次都校验：命中但 ZCode 结果变化 → 更新并返回新值；对齐 → 直接返回
        with patch.object(title_cache, "fetch_from_zcode", side_effect=["A", "B"]) as fetch:
            self.assertEqual(title_dispatcher.get_title("sess_1"), "A")
            self.assertEqual(title_dispatcher.get_title("sess_1"), "B")  # 变化 → 更新
        self.assertEqual(fetch.call_count, 2)  # 每次调用都校验（低成本）

    def test_get_title_falls_back_to_cache_when_zcode_down(self):
        with patch.object(title_cache, "fetch_from_zcode", side_effect=["cached", RuntimeError("zcode down")]):
            self.assertEqual(title_dispatcher.get_title("sess_1"), "cached")
            self.assertEqual(title_dispatcher.get_title("sess_1"), "cached")  # 校验失败 → 回落缓存

    def test_get_title_miss_then_zcode_down_returns_empty(self):
        # 未命中且 ZCode 不可用 → 柔化为空标题
        with patch.object(title_cache, "fetch_from_zcode", side_effect=RuntimeError("zcode down")):
            self.assertEqual(title_dispatcher.get_title("sess_new"), "")

    def test_cache_file_persists_single_map(self):
        with patch.object(title_cache, "fetch_from_zcode", return_value="标题A"), \
             patch.object(title_cache, "find_ids_by_title", return_value=["sess_A"]):
            title_dispatcher.get_title("sess_A")
            title_dispatcher.resolve_session("标题A")
        data = json.loads(Path(title_cache.cache_path()).read_text(encoding="utf-8"))
        # 只存一组 id->title；反向(title→[ids])由它推导，不再单独存
        self.assertEqual(data, {"sess_A": "标题A"})

    def test_legacy_flat_cache_migrates_on_load(self):
        Path(title_cache.cache_path()).write_text('{"sess_old": "旧标题"}', encoding="utf-8")
        fwd = title_cache._load()
        self.assertEqual(fwd, {"sess_old": "旧标题"})

    def test_title_miss_returns_empty_and_title_notfound_raises(self):
        with patch.object(title_cache, "fetch_from_zcode", return_value=""), \
             patch.object(title_cache, "find_ids_by_title", return_value=[]):
            self.assertEqual(title_dispatcher.get_title("sess_none"), "")  # id 查不到 → 空标题
            with self.assertRaises(title_dispatcher.SessionNotFound):
                title_dispatcher.resolve_session("不存在的标题")  # title 查不到 → SessionNotFound


class ZcodeDbPathTests(IsolatedCase):
    """item 12：ZCode 会话库路径统一走 ZCODE_DB_PATH，不再硬编码机器路径。"""

    def test_zcode_db_path_reads_env(self):
        with patch.dict(os.environ, {"ZCODE_DB_PATH": "C:/custom/z.sqlite"}):
            self.assertEqual(trace_split.zcode_db_path(), "C:/custom/z.sqlite")

    def test_zcode_db_path_default_fallback(self):
        saved = os.environ.pop("ZCODE_DB_PATH", None)
        try:
            expected = str(Path.home() / ".zcode" / "cli" / "db" / "db.sqlite")
            self.assertEqual(trace_split.zcode_db_path(), expected)
        finally:
            if saved is not None:
                os.environ["ZCODE_DB_PATH"] = saved


class HttpBodyLimitTests(IsolatedCase):
    """item 13：HTTP 请求体大小上限。用临时随机端口起真实 _Handler，避开 17891。"""

    @classmethod
    def setUpClass(cls):
        cls._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http_server._Handler)  # port 0 → 随机
        cls._thread = threading.Thread(target=cls._srv.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls._srv.shutdown()  # 先停 serve_forever，再关 socket，避免在关闭的 socket 上 select
        cls._srv.server_close()
        cls._thread.join(timeout=5)

    def test_oversized_body_rejected_413_without_reading(self):
        s = socket.create_connection(("127.0.0.1", self._srv.server_address[1]), timeout=5)
        try:
            # 只声明超大 Content-Length，不真实发送 body——服务端应仅凭头部就拒绝而不读入
            req = ("POST /remember HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   "Content-Type: application/json\r\n"
                   "Content-Length: %d\r\nConnection: close\r\n\r\n" % (config.MAX_BODY + 1))
            s.sendall(req.encode("latin-1"))
            chunks = []
            while True:
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
            resp = b"".join(chunks).decode("latin-1")
        finally:
            s.close()
        self.assertIn(" 413 ", resp)
        self.assertIn("E_INVALID", resp)
        self.assertIn("Connection: close", resp)

    def test_normal_body_not_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self._srv.server_address[1], timeout=5)
        try:
            conn.request("POST", "/remember",
                         body=json.dumps({"text": "hi", "session_id": "sess_httptest",
                                          "session_title": "Test"}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertNotEqual(resp.status, 413)
            self.assertEqual(resp.status, 200)
        finally:
            conn.close()


class HealthTests(IsolatedCase):
    """item 14：/health 反映真实依赖状态，不抛异常。"""

    @classmethod
    def setUpClass(cls):
        cls._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http_server._Handler)
        cls._thread = threading.Thread(target=cls._srv.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls._srv.shutdown()
        cls._srv.server_close()
        cls._thread.join(timeout=5)

    def _get_health(self):
        conn = http.client.HTTPConnection("127.0.0.1", self._srv.server_address[1], timeout=5)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    def test_health_ok_when_db_and_schema_valid(self):
        tbl = dbmod._ensure_db().create_table(config.TABLE, schema=dbmod.Msg)
        tbl.create_index("text", config=FTS(base_tokenizer="icu"))
        status, body = self._get_health()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["db"], "ok")
        self.assertTrue(body["schema"])
        self.assertTrue(body["fts"])

    def test_health_not_ok_when_schema_invalid(self):
        dbmod._ensure_db().create_table(config.TABLE, schema=dbmod.Msg)
        with patch.object(core, "_validate_messages_schema", return_value=False):
            status, body = self._get_health()
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertFalse(body["schema"])

    def test_soft_deps_do_not_flip_ok(self):
        tbl = dbmod._ensure_db().create_table(config.TABLE, schema=dbmod.Msg)
        tbl.create_index("text", config=FTS(base_tokenizer="icu"))
        with patch.object(core, "MODEL_DIR", str(self.root / "no-model")), \
             patch.object(core, "RERANK_DIR", str(self.root / "no-rerank")):
            status, body = self._get_health()
        self.assertTrue(body["ok"])
        self.assertFalse(body["embeddings"])
        self.assertFalse(body["reranker"])

    def test_queue_status_reports_error_and_backlog(self):
        """写入队列状态按需可查：ERROR 条数 + pending/processing 积压（替代已删除的 queue_alert）。"""
        tbl = dbmod._ensure_db().create_table(config.TABLE, schema=dbmod.Msg)
        tbl.create_index("text", config=FTS(base_tokenizer="icu"))
        con = sqlite3.connect(os.environ["CHAT_PENDING_DB"])
        con.execute("CREATE TABLE chat_pending (id INTEGER PRIMARY KEY, status TEXT)")
        con.executemany("INSERT INTO chat_pending (status) VALUES (?)",
                        [("ERROR",), ("pending",), ("processing",), ("done",)])
        con.commit()
        con.close()
        status, body = self._get_health()
        self.assertEqual(status, 200)
        self.assertEqual(body["queue_error"], 1)
        self.assertEqual(body["queue_pending"], 2)

    def test_queue_status_is_none_when_queue_missing(self):
        """队列文件读不到 → None（不是 0），且不影响 ok。"""
        tbl = dbmod._ensure_db().create_table(config.TABLE, schema=dbmod.Msg)
        tbl.create_index("text", config=FTS(base_tokenizer="icu"))
        status, body = self._get_health()
        self.assertIsNone(body["queue_error"])
        self.assertIsNone(body["queue_pending"])
        self.assertTrue(body["ok"])


class StoreTests(IsolatedCase):
    """item 18：数据库状态收拢到单个 Store 对象，便于一次性隔离/重置。"""

    def test_store_reset_clears_state(self):
        st = dbmod.Store()
        st.db = object()
        st.db_path = "x"
        st.db_key = "k"
        st.reset()
        self.assertIsNone(st.db)
        self.assertIsNone(st.db_path)
        self.assertIsNone(st.db_key)

    def test_ensure_db_populates_store(self):
        self.assertIsNone(db._store.db)  # IsolatedCase 每用例已换新 Store
        dbmod._ensure_db()
        self.assertIsNotNone(db._store.db)
        self.assertEqual(db._store.db_path, dbmod._db_file())


class StructuredSearchTests(IsolatedCase):
    """item 19：核心检索返回结构化数据，展示层由 _format_* 负责。"""

    def test_search_recall_returns_structured(self):
        self.remember("alpha final", kind="final")
        self.remember("alpha user", kind="user")
        self.remember("alpha other", sid="sess_B", kind="final")
        rows, scoped = core.search_recall("alpha", session="sess_A", kind="final", top_k=5, limit=1)
        self.assertTrue(scoped)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        for key in ("session_id", "session_title", "kind", "time", "round", "step", "text", "score"):
            self.assertIn(key, r)
        self.assertEqual(r["session_id"], "sess_A")
        self.assertEqual(r["kind"], "final")
        self.assertIn("alpha final", r["text"])
        self.assertGreaterEqual(r["score"], 0)

    def test_search_recall_unscoped(self):
        self.remember("alpha final", kind="final")
        rows, scoped = core.search_recall("alpha")
        self.assertFalse(scoped)

    def test_search_recent_returns_structured(self):
        self.remember("first", time="2026-09-07 09:00:00")
        self.remember("second", time="2026-09-07 10:00:00")
        rows, scoped = core.search_recent(session="sess_A", limit=2)
        self.assertTrue(scoped)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], "second")
        self.assertEqual(rows[1]["text"], "first")
        for key in ("session_id", "session_title", "kind", "time", "round", "step", "text"):
            self.assertIn(key, rows[0])

    def test_format_recall_equals_public_recall_output(self):
        self.remember("gamma body", kind="final")
        rows, scoped = core.search_recall("gamma", session="sess_A", top_k=3, limit=1)
        self.assertEqual(core._format_recall(rows, scoped),
                         core.recall("gamma", session="sess_A", top_k=3, limit=1))

    def test_format_recent_equals_public_recent_output(self):
        self.remember("delta", kind="final")
        rows, scoped = core.search_recent(session="sess_A", limit=3)
        self.assertEqual(core._format_recent(rows, scoped),
                         core.recent_messages(session="sess_A", limit=3))


class ProviderTests(unittest.TestCase):
    """item 20：onnx providers 单一来源。"""

    def test_default_is_cpu(self):
        with patch.dict(os.environ, {}, clear=False):
            saved = os.environ.pop("CHAT_HISTORY_PROVIDERS", None)
            try:
                self.assertEqual(onnx_providers.resolve_providers(), ["CPUExecutionProvider"])
            finally:
                if saved is not None:
                    os.environ["CHAT_HISTORY_PROVIDERS"] = saved

    def test_env_overrides_ordered(self):
        with patch.dict(os.environ, {"CHAT_HISTORY_PROVIDERS": "DmlExecutionProvider,CPUExecutionProvider"}):
            self.assertEqual(onnx_providers.resolve_providers(),
                             ["DmlExecutionProvider", "CPUExecutionProvider"])

    def test_blank_env_falls_back(self):
        with patch.dict(os.environ, {"CHAT_HISTORY_PROVIDERS": "  ,  "}):
            self.assertEqual(onnx_providers.resolve_providers(), ["CPUExecutionProvider"])

    def test_env_without_cpu_gets_cpu_appended(self):
        with patch.dict(os.environ, {"CHAT_HISTORY_PROVIDERS": "DmlExecutionProvider"}):
            self.assertEqual(onnx_providers.resolve_providers(),
                             ["DmlExecutionProvider", "CPUExecutionProvider"])


@unittest.skipUnless(os.environ.get("CHAT_HISTORY_REAL_MODELS") == "1", "Opt-in real ONNX models")
class RealModelTests(IsolatedCase):
    real_models = True

    def test_real_embedding_hybrid_and_reranking(self):
        self.remember("数据库结构已经修复", kind="final")
        self.remember("用户询问数据库结构", kind="user")
        result = core.recall("数据库结构修复", kind="final", top_k=2, limit=1)
        self.assertIn("数据库结构已经修复", result)
        self.assertIn("score=", result)
        table = dbmod._ensure_db().open_table(config.TABLE)
        self.assertEqual(len(table.to_arrow().to_pylist()[0]["vector"]), 1024)
