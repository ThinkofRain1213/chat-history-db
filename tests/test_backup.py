import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import title_cache
from tools import backup as bt
from tests.support import IsolatedCase
import config
import db


class BackupTests(IsolatedCase):
    """item 22：备份/恢复工具。用临时目录隔离，绝不写真实 ~/.agent/backups。"""

    def test_backup_copies_configured_data_into_timestamped_dir(self):
        self.remember("hello", sid="sess_a")
        title_cache._save({"sess_a": "标题"})  # 确保缓存文件存在（remember 带 title 时不写缓存）
        root = Path(tempfile.mkdtemp(prefix="bkptest-"))
        dest = bt.backup(dest_root=root)
        self.assertTrue(dest.is_dir())
        self.assertTrue((dest / "chat.db").is_dir())
        self.assertTrue((dest / "chat.db" / "messages.lance").exists() or (dest / "chat.db").is_dir())
        self.assertTrue((dest / "title_cache.json").exists())

    def test_list_is_descending(self):
        root = Path(tempfile.mkdtemp(prefix="bkptest-"))
        bt.backup(dest_root=root)
        bt.backup(dest_root=root)
        rows = bt.list_backups(root=root)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].name > rows[1].name)  # 时间倒序

    def test_restore_copies_archive_into_configured_data_path(self):
        root = Path(tempfile.mkdtemp(prefix="bkptest-"))
        archive = root / "chat-history-db-20260101-000000"
        archive.mkdir(parents=True)
        (archive / "chat.db").mkdir()
        (archive / "chat.db" / "messages.lance").write_text("fake", encoding="utf-8")
        (archive / "title_cache.json").write_text(json.dumps({"a": "b"}), encoding="utf-8")
        self._quiet()
        bt.restore(archive, dest_root=root)
        db_path = Path(db._db_file())
        self.assertTrue((db_path / "messages.lance").exists())
        self.assertEqual(json.loads(Path(title_cache.cache_path()).read_text(encoding="utf-8")), {"a": "b"})

    def _quiet(self, running=False, backlog=0):
        """把 restore 的前置检查替换为固定值，避免测试依赖真实 MCP 端口/队列。"""
        self.patch(patch.object(bt, "_mcp_running", lambda *a, **k: running))
        self.patch(patch.object(bt, "_pending_backlog", lambda *a, **k: backlog))

    def test_restore_refuses_when_mcp_running(self):
        root = Path(tempfile.mkdtemp(prefix="bkptest-"))
        archive = root / "chat-history-db-20260101-000000"
        archive.mkdir(parents=True)
        self._quiet(running=True)
        with self.assertRaises(SystemExit) as cm:
            bt.restore(archive, dest_root=root)
        self.assertIn("拒绝执行", str(cm.exception))

    def test_restore_refuses_when_backlog_pending(self):
        root = Path(tempfile.mkdtemp(prefix="bkptest-"))
        archive = root / "chat-history-db-20260101-000000"
        archive.mkdir(parents=True)
        self._quiet(backlog=4)
        with self.assertRaises(SystemExit) as cm:
            bt.restore(archive, dest_root=root)
        self.assertIn("4 条", str(cm.exception))

    def test_restore_force_skips_guards(self):
        root = Path(tempfile.mkdtemp(prefix="bkptest-"))
        archive = root / "chat-history-db-20260101-000000"
        archive.mkdir(parents=True)
        (archive / "chat.db").mkdir()
        (archive / "chat.db" / "messages.lance").write_text("fake", encoding="utf-8")
        self._quiet(running=True, backlog=3)
        bt.restore(archive, dest_root=root, force=True)
        self.assertTrue((Path(db._db_file()) / "messages.lance").exists())

    def test_verify_healthy(self):
        # 建表后 verify 应正常返回（不退出1）
        db._ensure_db().create_table(config.TABLE, schema=db.Msg)
        bt.verify()

    def test_verify_missing_table_is_not_fatal(self):
        # 空库（无表）→ _validate_messages_schema 返回 False → verify 提示缺失但不退出1
        bt.verify()


class VacuumTests(IsolatedCase):
    """数据库治理：vacuum（回收旧版本/合并碎片/更新索引）与 reindex。

    全部在隔离临时库上执行；MCP 端口与待入库队列用 patch 模拟，绝不碰真实环境。
    """

    def _seed(self, n=5):
        for i in range(n):
            self.remember(f"alpha message {i}", sid="sess_v")
        self.remember("beta message", sid="sess_w")

    def _quiet(self, running=False, backlog=0, backup_root=None):
        self.patch(patch.object(bt, "_mcp_running", lambda *a, **k: running))
        self.patch(patch.object(bt, "_pending_backlog", lambda *a, **k: backlog))
        if backup_root is not None:
            self.patch(patch.object(bt, "_BACKUP_ROOT", backup_root))

    def test_vacuum_reduces_files_and_preserves_rows(self):
        self._seed(20)  # 数据量足够，合并后文件数必然下降
        bt_root = self.root / "backups"
        self._quiet(backup_root=bt_root)
        report = bt.vacuum(assume_yes=True)
        self.assertTrue(report["rows_preserved"])
        self.assertGreater(report["before"]["rows"], 0)
        self.assertLess(report["after"]["files"], report["before"]["files"])
        for i in report["after"]["indices"]:
            self.assertEqual(i["unindexed_rows"], 0)
        self.assertTrue(list(bt_root.iterdir()))  # 已自动生成备份

    def test_vacuum_refuses_when_mcp_running(self):
        self._seed(1)
        self._quiet(running=True)
        with self.assertRaises(SystemExit) as cm:
            bt.vacuum(assume_yes=True)
        self.assertIn("拒绝执行", str(cm.exception))

    def test_vacuum_refuses_when_backlog_pending(self):
        self._seed(1)
        self._quiet(backlog=7)
        with self.assertRaises(SystemExit) as cm:
            bt.vacuum(assume_yes=True)
        self.assertIn("7 条", str(cm.exception))

    def test_vacuum_force_skips_guards(self):
        self._seed(1)
        self._quiet(running=True, backlog=3, backup_root=self.root / "backups")
        report = bt.vacuum(assume_yes=True, force=True)
        self.assertTrue(report["rows_preserved"])

    def test_vacuum_requires_existing_backup_when_backup_disabled(self):
        self._seed(1)
        self._quiet(backup_root=self.root / "empty-backups")
        with self.assertRaises(SystemExit) as cm:
            bt.vacuum(assume_yes=True, do_backup=False)
        self.assertIn("没有可用备份", str(cm.exception))

    def test_vacuum_requires_yes_when_not_tty(self):
        self._seed(1)
        self._quiet(backup_root=self.root / "backups")
        self.patch(patch.object(sys, "stdin", io.StringIO("")))  # isatty() -> False
        with self.assertRaises(SystemExit) as cm:
            bt.vacuum(assume_yes=False)
        self.assertIn("--yes", str(cm.exception))

    def test_reindex_clears_unindexed_rows(self):
        self._seed()
        for i in bt.reindex():
            self.assertEqual(i["unindexed_rows"], 0)

    def test_cli_script_mode_reaches_project_modules(self):
        """以脚本方式运行 tools/backup.py 时必须能 import 项目根下的模块。

        直接运行脚本时 sys.path[0] 是 tools/ 而非项目根，曾因此让 verify/vacuum/reindex
        全部 ModuleNotFoundError（backup/list/restore 不 import 项目模块，掩盖了这个问题）。
        """
        root = Path(bt.__file__).resolve().parent.parent
        env = {**os.environ, "CHAT_HISTORY_DB": str(self.root / "chat.db")}
        proc = subprocess.run(
            [sys.executable, str(root / "tools" / "backup.py"), "verify"],
            capture_output=True, text=True, cwd=str(root), env=env,
        )
        self.assertNotIn("ModuleNotFoundError", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
