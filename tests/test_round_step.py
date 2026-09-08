import os
import unittest
from pathlib import Path
from unittest.mock import patch

from lancedb.pydantic import LanceModel, Vector

import db
from tests.support import IsolatedCase
import config
import core


class _OldMsg(LanceModel):
    """迁移前的老 schema（无 round/step），用于验证启动补列。"""

    session_id: str
    session_title: str
    kind: str
    time: str
    turn: int
    text: str
    vector: Vector(1024)


class RoundStepTests(IsolatedCase):
    """round/step 语义：用户开轮且无 step，agent 消息归属最后一个用户轮次。"""

    def test_user_opens_round_without_step(self):
        first = self.remember("q1", kind="user")
        second = self.remember("q2", kind="user")
        self.assertEqual((first["round"], first["step"]), (1, 0))
        self.assertEqual((second["round"], second["step"]), (2, 0))

    def test_agent_messages_follow_last_user_round(self):
        self.remember("q", kind="user")
        mid = self.remember("m", kind="mid")
        tool = self.remember("t", kind="tool")
        final = self.remember("f", kind="final")
        self.assertEqual([(x["round"], x["step"]) for x in (mid, tool, final)],
                         [(1, 1), (1, 2), (1, 3)])

    def test_consecutive_user_messages_each_open_new_round(self):
        rounds = [self.remember(f"q{i}", kind="user")["round"] for i in range(3)]
        self.assertEqual(rounds, [1, 2, 3])
        final = self.remember("f", kind="final")
        self.assertEqual((final["round"], final["step"]), (3, 1))

    def test_agent_output_after_final_stays_in_last_user_round(self):
        self.remember("q", kind="user")
        self.remember("f", kind="final")
        mid = self.remember("m", kind="mid")  # 斜杠命令轮：没有 user 行
        self.assertEqual((mid["round"], mid["step"]), (1, 2))

    def test_sessions_are_isolated(self):
        a1 = self.remember("a", kind="user", sid="sess_A")
        b1 = self.remember("b", kind="user", sid="sess_B")
        a2 = self.remember("a2", kind="mid", sid="sess_A")
        self.assertEqual((a1["round"], a1["step"]), (1, 0))
        self.assertEqual((b1["round"], b1["step"]), (1, 0))
        self.assertEqual((a2["round"], a2["step"]), (1, 1))

    def test_round_step_unique_per_session(self):
        seen = set()
        for kind in ("user", "mid", "tool", "final", "user", "mid"):
            result = self.remember(kind, kind=kind)
            key = (result["round"], result["step"])
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_explicit_round_step_is_preserved(self):
        result = self.remember("x", kind="mid", round=9, step=9)
        self.assertEqual((result["round"], result["step"]), (9, 9))

    def test_round_zero_rows_display_as_zero_round(self):
        """会话开头尚无用户轮次的行（round=0）显示为 #0[.step]；去 turn 之前这里回退 #turn。"""
        self.remember("legacy", kind="mid", round=0, step=3)
        self.assertIn("[#0.3 | mid |", core.recent_messages(session="sess_A", kind="all"))
        following = self.remember("new", kind="user")
        self.assertEqual((following["round"], following["step"]), (1, 0))

    def test_recent_shows_round_and_step(self):
        self.remember("q", kind="user")
        self.remember("m", kind="mid")
        out = core.recent_messages(session="sess_A", kind="all")
        self.assertIn("#1 | user", out)
        self.assertIn("#1.1 | mid", out)

    def test_migration_adds_missing_columns_idempotently(self):
        conn = db._ensure_db()
        conn.create_table(config.TABLE, data=[
            dict(session_id="sess_A", session_title="t", kind="user",
                 time="2026-09-07 10:00:00", turn=1, text="old", vector=[0.0] * 1024)
        ], schema=_OldMsg)
        self.assertTrue(db._migrate_messages_schema())
        tbl = db._open_or_none(conn)
        self.assertTrue({"round", "step"} <= {f.name for f in tbl.schema})
        self.assertTrue(db._migrate_messages_schema())  # 第二次为 no-op，不抛
        row = tbl.search().select(["round", "step"]).limit(1).to_list()[0]
        self.assertEqual((row["round"], row["step"]), (0, 0))
        self.assertTrue(db._validate_messages_schema())  # 补列后校验必须通过

    def test_migration_without_table_returns_false(self):
        self.assertFalse(db._migrate_messages_schema())

    def test_session_lock_is_mutually_exclusive(self):
        """同一会话的锁必须互斥：进入临界区的线程不能重叠。"""
        import threading
        import time

        inside, overlaps = [], []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            for _ in range(3):
                with db._session_lock("sess_lock_probe"):
                    if inside:
                        overlaps.append(1)
                    inside.append(1)
                    time.sleep(0.01)
                    inside.pop()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(overlaps, [])

    def test_lock_path_is_normalized(self):
        """同一个库的不同拼写必须映射到同一把锁，否则跨进程互斥失效（会算出相同 round/step）。"""
        raw = db._db_file()
        base = db._lock_path("sess_X")
        with patch.dict(os.environ, {"CHAT_HISTORY_DB": raw + os.sep}):
            self.assertEqual(db._lock_path("sess_X"), base)
        if db.msvcrt is not None:  # Windows 路径大小写不敏感、斜杠等价
            for variant in (raw.upper(), raw.replace("\\", "/")):
                with patch.dict(os.environ, {"CHAT_HISTORY_DB": variant}):
                    self.assertEqual(db._lock_path("sess_X"), base, variant)

    def test_lock_dir_sits_next_to_db_not_tempdir(self):
        """锁目录必须由库路径决定：放在 tempdir 会随 TMPDIR/TEMP/TMP 变化，互斥静默失效。"""
        import tempfile

        expected = Path(os.path.realpath(db._db_file()) + ".locks")
        self.assertEqual(db._lock_path("sess_X").parent, expected)
        self.assertNotIn(tempfile.gettempdir().lower(),
                         str(db._lock_path("sess_X")).lower())

    def test_ensure_db_ignores_path_spelling_change(self):
        """同一进程内库路径换拼写不得重连（realpath/normcase 归一后是同一个库）。"""
        first = db._ensure_db()
        with patch.dict(os.environ, {"CHAT_HISTORY_DB": db._db_file().replace("\\", "/")}):
            second = db._ensure_db()
        self.assertIs(first, second)

    def test_lock_prune_removes_only_old_files_above_cap(self):
        """锁文件只在超过上限时清理，且只删 7 天前的（仍被持有的删不掉，不破坏互斥）。"""
        import time as _time

        lock_dir = Path(os.path.realpath(db._db_file()) + ".locks")
        lock_dir.mkdir(parents=True, exist_ok=True)
        old, fresh = lock_dir / "old.lock", lock_dir / "fresh.lock"
        old.write_bytes(b"")
        fresh.write_bytes(b"")
        past = _time.time() - 8 * 24 * 3600
        os.utime(old, (past, past))
        with patch.object(db, "_LOCK_MAX_FILES", 1):
            db._prune_locks(lock_dir)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_remember_refreshes_table_inside_lock(self):
        """锁外打开的表对象停在旧快照：写入必须锁内重开，否则读到旧尾行照样算出相同 round/step。

        复现真实交错：A 在锁外开表（快照停在此刻）→ 另一个写入进程提交一行 → A 才拿到锁。
        修复前 A 用旧快照推导，得到与别人相同的值；修复后锁内重开，看到最新行。
        """
        import lancedb

        import core

        self.remember("first", kind="user")  # (round, step) = (1, 0)
        handle = db._ensure_db()
        stale = core._open_or_none(handle)  # 模拟"锁外打开、随后变旧"的表对象
        other = lancedb.connect(db._db_file())  # 模拟另一个写入进程
        other.open_table(db.TABLE).add([{
            "session_id": "sess_A", "session_title": "Test session", "kind": "final",
            "time": "2026-09-07 10:00:01", "round": 1, "step": 1, "text": "other",
        }])

        real_open = core._open_or_none
        calls = []

        def first_call_is_stale(handle_):
            calls.append(1)
            return stale if len(calls) == 1 else real_open(handle_)

        with patch.object(core, "_open_or_none", side_effect=first_call_is_stale):
            got = core.remember("sess_A", "third", kind="final")
        self.assertEqual((got["round"], got["step"]), (1, 2))

        # 反证：始终用锁外那份旧快照（= 修复前的顺序）就会算出别人已经占用的 (1, 1)
        with patch.object(core, "_open_or_none", return_value=stale):
            dup = core.remember("sess_A", "fourth", kind="final")
        self.assertEqual((dup["round"], dup["step"]), (1, 1))  # 旧快照停在 (1,0)，算出 (1,1)，撞号

    def test_ensure_messages_table_tolerates_concurrent_create(self):
        """并发首建：create_table 抛"已存在"时应改用别人建好的表，而不是让这次写入失败。"""
        import core

        handle = db._ensure_db()
        real_create = handle.create_table

        def create_then_raise(*args, **kwargs):
            real_create(*args, **kwargs)  # 模拟另一个进程抢先建好
            raise RuntimeError("table already exists")

        with patch.object(handle, "create_table", side_effect=create_then_raise):
            tbl = core._ensure_messages_table(handle)
        self.assertEqual(tbl.count_rows(), 0)
        self.assertTrue(any(i.index_type == "FTS" and "text" in i.columns
                            for i in tbl.list_indices()))


if __name__ == "__main__":
    unittest.main()
