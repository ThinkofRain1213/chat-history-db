import contextlib
import io
import os
import threading
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import maintenance
from tests.support import IsolatedCase


class FakeTable:
    """记录 optimize 入参的替身，用来断言清理参数。"""

    def __init__(self):
        self.kwargs = None

    def optimize(self, **kwargs):
        self.kwargs = kwargs


class MaintenanceTests(IsolatedCase):
    """启动期空间治理：按阈值决定是否回收旧版本清单。

    全部在隔离临时库上执行；每个用例换一把新的重入锁，避免跨用例残留。
    """

    def setUp(self):
        super().setUp()
        self.patch(patch.object(maintenance, "_RUN_LOCK", threading.Lock()))
        # 日志默认写真实 ~/.agent/hooks/chat_maintenance.log，测试必须隔离，否则污染生产日志
        self.patch(patch.object(maintenance, "_DEFAULT_LOG", self.root / "gc.log"))

    def _seed(self, n=5):
        for i in range(n):
            self.remember(f"alpha message {i}", sid="sess_gc")

    def _threshold(self, mb):
        self.patch(patch.dict(os.environ, {"CHAT_HISTORY_GC_MB": str(mb)}, clear=False))

    def test_skips_when_below_threshold(self):
        self._seed()
        self._threshold(1000)
        optimize = self.patch(patch.object(maintenance, "_optimize", Mock()))
        self.assertIsNone(maintenance.maybe_optimize())
        optimize.assert_not_called()

    def test_runs_when_above_threshold(self):
        self._seed(20)
        self._threshold(0)
        report = maintenance.maybe_optimize()
        self.assertIsNotNone(report)
        self.assertGreater(report["before_bytes"], 0)
        self.assertLess(report["after_bytes"], report["before_bytes"])
        self.assertEqual(report["rows_before"], report["rows_after"])

    def test_failure_is_swallowed(self):
        self._seed(1)
        self._threshold(0)
        self.patch(patch.object(maintenance, "_optimize", Mock(side_effect=RuntimeError("boom"))))
        self.assertIsNone(maintenance.maybe_optimize())

    def test_not_reentrant(self):
        self._seed(1)
        self._threshold(0)
        optimize = self.patch(patch.object(maintenance, "_optimize", Mock()))
        maintenance._RUN_LOCK.acquire()
        try:
            self.assertIsNone(maintenance.maybe_optimize())
        finally:
            maintenance._RUN_LOCK.release()
        optimize.assert_not_called()

    def test_optimize_uses_safe_kwargs(self):
        fake = FakeTable()
        maintenance._optimize(fake)
        self.assertIs(fake.kwargs["delete_unverified"], False)
        self.assertEqual(fake.kwargs["cleanup_older_than"], timedelta(seconds=0))

    def test_disabled_by_env(self):
        self._seed(1)
        self._threshold(0)
        self.patch(patch.dict(os.environ, {"CHAT_HISTORY_GC": "0"}, clear=False))
        optimize = self.patch(patch.object(maintenance, "_optimize", Mock()))
        self.assertIsNone(maintenance.maybe_optimize())
        optimize.assert_not_called()

    def test_missing_table_is_silent(self):
        self._threshold(-1)  # 阈值置负，强制越过阈值判断，走到「表不存在」分支
        self.assertIsNone(maintenance.maybe_optimize())

    def test_stdout_stays_empty(self):
        self._seed(5)
        self._threshold(0)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            maintenance.maybe_optimize()
        self.assertEqual(buffer.getvalue(), "")  # stdio 是 MCP 协议通道，绝不能写

    def test_background_thread_runs_and_is_daemon(self):
        self._seed(1)
        self._threshold(0)
        self.patch(patch.object(maintenance, "_STARTUP_DELAY", 0))
        called = threading.Event()
        self.patch(patch.object(maintenance, "maybe_optimize", lambda *a, **k: called.set()))
        thread = maintenance.start_background_maintenance()
        self.assertIsNotNone(thread)
        self.assertTrue(thread.daemon)
        self.assertTrue(called.wait(5))

    def test_concurrent_writes_during_optimize_are_reported(self):
        """optimize 不持写锁，期间写入是正常的：要计入 concurrent_writes，而不是记成行数异常。"""
        self._seed(5)
        self._threshold(0)
        real_optimize = maintenance._optimize

        def optimize_with_write(tbl):
            self.remember("concurrent row", sid="sess_gc")  # 模拟另一进程在 optimize 期间写入
            real_optimize(tbl)

        self.patch(patch.object(maintenance, "_optimize", optimize_with_write))
        report = maintenance.maybe_optimize()
        self.assertIsNotNone(report)
        self.assertEqual(report["concurrent_writes"], 1)
        self.assertEqual(report["rows_after"], report["rows_before"] + 1)
        self.assertIn("并发写入 1 行", (self.root / "gc.log").read_text(encoding="utf-8"))

    def test_logs_to_file(self):
        maintenance._log("cleaned 1 MiB -> 0 MiB")
        self.assertIn("cleaned 1 MiB -> 0 MiB", (self.root / "gc.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
