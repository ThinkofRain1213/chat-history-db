import unittest
from unittest.mock import patch

import http_server


class HttpPortTests(unittest.TestCase):
    """多实例下的 HTTP 端口归属：Windows 的 SO_REUSEADDR 会让第二个进程"绑定成功"，
    所以必须先探测再绑，并把降级写进日志（ZCode 不保留 MCP 的 stderr）。"""

    def test_defers_when_another_instance_serves_the_port(self):
        with patch.object(http_server, "_http_alive", return_value=True), \
             patch.object(http_server, "_bind") as bind, \
             patch.object(http_server, "_log") as log, \
             patch.object(http_server, "threading") as fake_threading:
            self.assertIsNone(http_server._start_http_server())
        bind.assert_not_called()
        fake_threading.Thread.assert_called_once()
        self.assertTrue(any("已有实例" in str(c) for c in log.call_args_list))

    def test_binds_and_serves_when_port_free(self):
        fake = object()
        with patch.object(http_server, "_http_alive", return_value=False), \
             patch.object(http_server, "_bind", return_value=fake), \
             patch.object(http_server, "_serve") as serve, \
             patch.object(http_server, "_log"):
            self.assertIs(http_server._start_http_server(), fake)
        serve.assert_called_once_with(fake)

    def test_retry_keeps_waiting_while_incumbent_alive_then_takes_over(self):
        state = {"n": 0}

        def alive():
            state["n"] += 1
            return state["n"] < 3  # 前两次探测到实例存活，第三次已退出

        fake = object()
        with patch.object(http_server, "_http_alive", side_effect=alive), \
             patch.object(http_server, "_bind", return_value=fake) as bind, \
             patch.object(http_server, "_serve") as serve, \
             patch.object(http_server, "_log"), \
             patch.object(http_server, "_RETRY_SEC", 0.01):
            http_server._retry_until_bound()
        self.assertEqual(bind.call_count, 1)
        serve.assert_called_once_with(fake)


if __name__ == "__main__":
    unittest.main()
