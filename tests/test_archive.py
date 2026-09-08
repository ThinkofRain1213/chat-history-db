import unittest
from unittest.mock import patch

import archive
import maintenance
import title_cache
from config import ARCHIVE_TABLE, TABLE
from tests.support import IsolatedCase
import core
import db


class ArchiveTests(IsolatedCase):
    """归档 / 恢复 / 删除会话：搬行原子性、幂等、source 参数与已归档提示。"""

    def _seed(self, sid="sess_A", n=3):
        for i in range(n):
            self.remember(f"{sid} msg {i}", sid=sid, kind="user" if i == 0 else "mid")
        return n

    def test_archive_moves_rows_and_preserves_vectors(self):
        self._seed()
        before = core.recent_messages(session="sess_A", kind="all")
        vectors = (db._ensure_db().open_table(TABLE)
                   .search().where("session_id = 'sess_A'", prefilter=True)
                   .select(["vector"]).limit(10).to_list())
        result = archive.archive_session("sess_A")
        self.assertEqual(result["rows"], 3)

        main = db._ensure_db().open_table(TABLE)
        self.assertEqual(len(archive._session_rows(main, "sess_A")), 0)
        arch = db._ensure_db().open_table(ARCHIVE_TABLE)
        archived = archive._session_rows(arch, "sess_A")
        self.assertEqual(len(archived), 3)
        self.assertEqual([r["vector"] for r in archived], [v["vector"] for v in vectors])
        self.assertIn("sess_A msg 0", before)

    def test_archive_clears_stale_archive_rows(self):
        """模拟「上次搬到一半」的残留：重跑后归档表里该会话只有一份。"""
        self._seed(n=2)
        main = db._ensure_db().open_table(TABLE)
        arch = archive._ensure_archive_table(db._ensure_db())
        arch.add(archive._session_rows(main, "sess_A"))  # 人为留下重复归档
        archive.archive_session("sess_A")
        self.assertEqual(len(archive._session_rows(arch, "sess_A")), 2)

    def test_archive_unknown_session_raises(self):
        from title_dispatcher import SessionNotFound
        with self.assertRaises(SessionNotFound):
            archive.archive_session("sess_nope")

    def test_dry_run_does_not_write(self):
        self._seed()
        result = archive.archive_session("sess_A", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["rows"], 3)
        main = db._ensure_db().open_table(TABLE)
        self.assertEqual(len(archive._session_rows(main, "sess_A")), 3)
        self.assertEqual(archive.count_rows("sess_A", ARCHIVE_TABLE), 0)

    def test_restore_moves_rows_back(self):
        self._seed()
        archive.archive_session("sess_A")
        result = archive.restore_session("sess_A")
        self.assertEqual(result["rows"], 3)
        main = db._ensure_db().open_table(TABLE)
        self.assertEqual(len(archive._session_rows(main, "sess_A")), 3)
        self.assertEqual(archive.count_rows("sess_A", ARCHIVE_TABLE), 0)

    def test_restore_missing_raises(self):
        from title_dispatcher import SessionNotFound
        self._seed()
        with self.assertRaises(SessionNotFound):
            archive.restore_session("sess_A")

    def test_delete_requires_confirm(self):
        self._seed()
        archive.archive_session("sess_A")
        with self.assertRaises(Exception) as cm:
            archive.delete_session("sess_A")
        self.assertIn("confirm", str(cm.exception))
        self.assertEqual(archive.count_rows("sess_A", ARCHIVE_TABLE), 3)

    def test_delete_active_session_is_rejected(self):
        """活跃会话不在归档表 → 删除必须失败，数据不动。"""
        from title_dispatcher import SessionNotFound
        self._seed()
        with self.assertRaises(SessionNotFound):
            archive.delete_session("sess_A", confirm=True)
        self.assertEqual(archive.count_rows("sess_A", TABLE), 3)

    def test_delete_removes_rows_permanently(self):
        self._seed()
        archive.archive_session("sess_A")
        result = archive.delete_session("sess_A", confirm=True)
        self.assertEqual(result["rows"], 3)
        self.assertEqual(archive.count_rows("sess_A", ARCHIVE_TABLE), 0)

    def test_archived_session_hidden_and_hint_returned(self):
        self._seed()
        archive.archive_session("sess_A")
        # 主表查不到 → 返回可操作提示（任何 kind 组合都提示）
        for kwargs in ({}, {"kind": "all"}):
            hint = core.recent_messages(session="sess_A", **kwargs)
            self.assertIn("已归档", hint)
            self.assertIn("source='archive'", hint)
        listed = [s["session_id"] for s in core.list_sessions()]
        self.assertNotIn("sess_A", listed)
        self.assertIn("sess_A", [s["session_id"] for s in core.list_sessions(source="archive")])

    def test_recent_and_recall_with_source_archive(self):
        self._seed()
        archive.archive_session("sess_A")
        out = core.recent_messages(session="sess_A", kind="all", source="archive")
        self.assertIn("sess_A msg 0", out)
        self.assertIn("archive", out)  # 来源标记
        hit = core.recall("sess_A msg", session="sess_A", source="archive")
        self.assertIn("sess_A msg", hit)

    def test_invalid_source_raises(self):
        self._seed()
        with self.assertRaises(Exception) as cm:
            core.list_sessions(source="bogus")
        self.assertIn("source", str(cm.exception))

    def test_title_cache_key_purged_on_archive(self):
        self._seed()
        title_cache.set_id_title("sess_A", "A 标题")
        self.assertEqual(title_cache.get("sess_A"), "A 标题")
        archive.archive_session("sess_A")
        self.assertIsNone(title_cache.get("sess_A"))

    def test_drop_empty_removes_blank_titles(self):
        title_cache.set_id_title("sess_keep", "有标题")
        title_cache._save({"sess_keep": "有标题", "sess_x": "", "sess_y": "  "})
        title_cache._FORWARD = None  # 重新加载
        self.assertEqual(title_cache.drop_empty(), 2)
        self.assertEqual(title_cache.get("sess_keep"), "有标题")
        self.assertIsNone(title_cache.get("sess_x"))

    def test_maintenance_covers_archive_table(self):
        self._seed()
        archive.archive_session("sess_A")
        arch_dir = maintenance._versions_dir(ARCHIVE_TABLE)
        self.assertTrue(arch_dir.exists())
        self.assertGreaterEqual(maintenance.garbage_bytes(), maintenance._scan_bytes(arch_dir))


    def test_delete_intents_are_pruned_when_over_cap(self):
        """被放弃的删除意向不会自己消失：超过上限时回收已过期项，避免无界增长。"""
        import core
        core._delete_intents.clear()
        self.addCleanup(core._delete_intents.clear)
        self.patch(patch.object(core, "_DELETE_TTL", -1))  # 意向立即过期
        for i in range(core._INTENT_MAX + 5):
            core._delete_gate(f"sess_{i}")  # 只登记意向，不执行删除
        self.assertLessEqual(len(core._delete_intents), core._INTENT_MAX + 1)


    def test_count_and_info_do_not_read_full_rows(self):
        """B3：计数/取标题走过滤下推，不再把整会话（含 1024 维向量）读进内存。"""
        self._seed(n=3)
        with patch.object(archive, "_session_rows", side_effect=AssertionError("不应整行读取")):
            self.assertEqual(archive.count_rows("sess_A"), 3)
            self.assertEqual(archive.count_rows("sess_nope"), 0)
            self.assertEqual(archive.session_info("sess_A"), {"rows": 3, "title": "Test session"})
            self.assertEqual(archive.session_info("sess_nope"), {"rows": 0, "title": ""})


if __name__ == "__main__":
    unittest.main()
