import http.client
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import title_dispatcher
import title_cache
import trace_split
import core
from tests.support import IsolatedCase
import errors
import http.server
import http_server
import mcp_tools


class TraceTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.con = sqlite3.connect(trace_split.ZCODE_DB)
        self.con.executescript("""
            CREATE TABLE message (id TEXT, session_id TEXT, sequence INTEGER, data TEXT);
            CREATE TABLE part (message_id TEXT, sequence INTEGER, data TEXT);
            CREATE TABLE session (id TEXT, title TEXT);
        """)
        self.addCleanup(self.con.close)

    def message(self, mid, seq, role, parts, **extra):
        data = {"role": role, "time": {"created": 123456}, **extra}
        self.con.execute("INSERT INTO message VALUES (?,?,?,?)", (mid, "sess_A", seq, json.dumps(data)))
        for i, part in enumerate(parts):
            self.con.execute("INSERT INTO part VALUES (?,?,?)", (mid, i, json.dumps(part)))
        self.con.commit()

    def test_split_excludes_reasoning_and_tool_results(self):
        self.message("m1", 1, "user", [{"type": "text", "text": "question"}])
        self.message("m2", 2, "assistant", [
            {"type": "reasoning", "text": "private reasoning"},
            {"type": "text", "text": "working"},
            {"type": "tool", "tool": "Read", "state": {
                "input": {"description": "read file"}, "output": "private result"}},
            {"type": "step-finish"}, {"type": "text", "text": "answer"},
        ])
        rows = trace_split.mark_final(trace_split.split_session("sess_A"), "answer")
        self.assertEqual([r["kind"] for r in rows], ["user", "mid", "tool", "final"])
        self.assertEqual([r["text"] for r in rows], ["question", "working", "Read: read file", "answer"])
        self.assertEqual(len(trace_split.split_session("sess_A", since_seq=2)), 3)

    def test_runtime_messages_are_excluded(self):
        for i, extra in enumerate(( {"metadata": {"visibility": "model-only"}},
                                   {"metadata": {"runtimeMessage": True}}, {"summary": True})):
            self.message(str(i), i, "user", [{"type": "text", "text": "injected"}], **extra)
        self.assertEqual(trace_split.split_session("sess_A"), [])

    def test_final_matching_and_fallback(self):
        rows = [{"kind": "mid", "text": "first"}, {"kind": "mid", "text": "last"}]
        self.assertEqual(trace_split.mark_final(rows, "first")[0]["kind"], "final")
        self.assertEqual(trace_split.mark_final(rows, "unknown")[-1]["kind"], "final")
        self.assertEqual(trace_split.mark_final([], "anything"), [])

    def test_title_lookup_and_cache_use_temporary_sqlite(self):
        self.con.execute("INSERT INTO session VALUES (?,?)", ("sess_A", "Title"))
        self.con.commit()
        self.assertEqual(title_cache.fetch_from_zcode("sess_A"), "Title")
        self.assertEqual(title_cache.find_ids_by_title("Title"), ["sess_A"])
        self.assertEqual(title_dispatcher.get_title("sess_A"), "Title")
        self.assertTrue((self.root / "titles.json").exists())
        self.assertEqual(title_cache.fetch_from_zcode("missing"), "")


class HttpTests(IsolatedCase):
    @classmethod
    def setUpClass(cls):
        cls.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http_server._Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        try:
            connection.request(method, path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_health_and_unknown_routes(self):
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("GET", "/healthz")[0], 200)
        self.assertEqual(self.request("GET", "/unknown")[0], 404)
        self.assertEqual(self.request("POST", "/unknown", "{}")[0], 404)

    def test_bad_json_and_missing_content(self):
        self.assertEqual(self.request("POST", "/remember", "{")[0], 400)
        self.assertEqual(self.request("POST", "/remember", "{}")[0], 400)

    def test_write_preserves_fields(self):
        payload = {"session_id": "sess_A", "session_title": "HTTP test", "content": "alpha",
                   "kind": "user", "time": "2026-09-07 10:00:00",
                   "round": 3, "step": 0}
        status, result = self.request("POST", "/remember", json.dumps(payload))
        self.assertEqual(status, 200)
        self.assertTrue(result["inserted"])
        self.assertEqual(result["round"], 3)
        self.assertEqual(result["step"], 0)
        recent = core.recent_messages(session="sess_A", kind="user")
        self.assertIn("[#3 | user | 2026-09-07 10:00:00] alpha", recent)

    def test_write_exception_is_returned_as_failure(self):
        with patch.object(core, "remember", side_effect=RuntimeError("test failure")):
            status, result = self.request("POST", "/remember", '{"text":"alpha"}')
        self.assertEqual(status, 500)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], f"失败 [E_INTERNAL]：{errors.ERROR_REASONS['E_INTERNAL']}")


class CapturedServer:
    def __init__(self, **kwargs):
        self.tools = {}

    def tool(self, *, name, **kwargs):
        def register(function):
            self.tools[name] = function
            return function
        return register


class McpWrapperTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        with patch.object(mcp_tools, "MCPServer", CapturedServer):
            self.tools = mcp_tools._build_server().tools

    def test_core_and_http_layers_do_not_import_mcp_sdk(self):
        """B1 解耦护栏：领域/数据/HTTP/归档层都不应依赖 MCP SDK（只有 mcp_tools 可以）。

        用子进程把 `mcp` 包从 import 层面屏蔽，再导入这些模块——一旦有人把 SDK 依赖放回
        领域层，这条会立刻失败。
        """
        code = (
            "import builtins\n"
            "real = builtins.__import__\n"
            "def guard(name, *a, **k):\n"
            "    if name.split('.')[0] == 'mcp': raise ImportError('mcp blocked')\n"
            "    return real(name, *a, **k)\n"
            "builtins.__import__ = guard\n"
            "import core, db, http_server, archive, title_dispatcher\n"
            "print('ok')\n"
        )
        root = Path(core.__file__).resolve().parent
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, cwd=str(root))
        self.assertIn("ok", proc.stdout, proc.stderr)

    def test_remember_context_and_parameters(self):
        with patch.object(mcp_tools, "_extract_session_id", return_value="sess_A"), \
                patch.object(core, "remember") as remember:
            result = self.tools["remember"]("alpha", kind="user", time="time", round=4, session_title="title")
        self.assertEqual(result, "成功")
        remember.assert_called_once_with("sess_A", "alpha", kind="user", time="time",
                                         session_title="title", round=4, step=None)

    def test_recent_defaults(self):
        with patch.object(core, "recent_messages", return_value="result") as recent:
            self.assertEqual(self.tools["recent"](), "result")
            self.assertIsNone(recent.call_args.args[3])  # limit=None 透传，由领域算默认
            self.tools["recent"](kind="all")
            self.assertIsNone(recent.call_args.args[3])
            self.tools["recent"](limit=2, kind="all")
            self.assertEqual(recent.call_args.args[3], 2)

    def test_recall_forwards_scope(self):
        with patch.object(mcp_tools, "_extract_session_id", return_value="sess_A"), \
                patch.object(core, "recall", return_value="result") as recall:
            self.assertEqual(self.tools["recall"]("alpha", session="current"), "result")
        self.assertEqual(recall.call_args.kwargs["current_session_id"], "sess_A")

    def test_wrapper_errors(self):
        for name, target, args in (("remember", "remember", ("alpha",)),
                                   ("recall", "recall", ("alpha",)),
                                   ("recent", "recent_messages", ()),
                                   ("list_sessions", "list_sessions", ())):
            with self.subTest(name=name), patch.object(core, target, side_effect=core.InvalidInput("bad")):
                self.assertEqual(self.tools[name](*args), f"失败 [E_INVALID]：{errors.ERROR_REASONS['E_INVALID']}")

    def test_session_admin_delete_requires_archive_first(self):
        self.remember("alpha", sid="sess_A")
        out = self.tools["session_admin"]("delete", session="sess_A")
        self.assertIn("E_NOTARCHIVED", out)
        self.assertIn("请先 action='archive'", out)
        self.assertIn("sess_A", [s["session_id"] for s in core.list_sessions()])

    def test_session_admin_delete_is_two_phase(self):
        self.remember("alpha", sid="sess_A")
        self.tools["session_admin"]("archive", session="sess_A")
        core._delete_intents.clear()
        first = self.tools["session_admin"]("delete", session="sess_A")
        self.assertIn("待确认", first)
        self.assertIn("AskUserQuestion", first)
        self.assertIn("确认删除", first)
        self.assertIn("取消", first)
        # 第一次不删：仍在归档表
        self.assertIn("sess_A", [s["session_id"] for s in core.list_sessions(source="archive")])
        second = self.tools["session_admin"]("delete", session="sess_A")
        self.assertIn("已永久删除", second)
        self.assertEqual(core.list_sessions(source="archive"), [])

    def test_session_admin_delete_gate_expires(self):
        self.remember("alpha", sid="sess_A")
        self.tools["session_admin"]("archive", session="sess_A")
        core._delete_intents.clear()
        self.assertIn("待确认", self.tools["session_admin"]("delete", session="sess_A"))
        core._delete_intents["sess_A"] = 0  # 模拟意向过期
        self.assertIn("待确认", self.tools["session_admin"]("delete", session="sess_A"))
        self.assertIn("sess_A", [s["session_id"] for s in core.list_sessions(source="archive")])

    def test_session_admin_archive_restore(self):
        self.remember("alpha", sid="sess_A")
        self.assertIn("已归档", self.tools["session_admin"]("archive", session="sess_A"))
        self.assertIn("sess_A", self.tools["list_sessions"](source="archive"))
        self.assertIn("已恢复", self.tools["session_admin"]("restore", session="sess_A"))
        self.assertNotIn("sess_A", self.tools["list_sessions"](source="archive"))

    def test_session_admin_list_moved_to_list_sessions(self):
        # 列出统一走 list_sessions；session_admin 不再接受 'list'
        self.assertIn("E_INVALID", self.tools["session_admin"]("list"))

    def test_session_admin_rejects_bad_action(self):
        out = self.tools["session_admin"]("bogus")
        self.assertIn("E_INVALID", out)
        self.assertIn("E_INVALID", self.tools["session_admin"]("archive"))  # 缺 session
