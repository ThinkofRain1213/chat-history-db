import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bgem3_embedding
import reranker
import title_cache
import core
from errors import (HistoryError, InvalidInput, DatabaseError, SchemaError,
                    ModelError, IndexError as HistoryIndexError, error_boundary)
from tests.support import IsolatedCase
import config
import db as dbmod
import errors
import lancedb


class ErrorTests(IsolatedCase):
    def test_stable_categories_and_unknown_valueerror(self):
        for error, code in ((InvalidInput(), "E_INVALID"), (DatabaseError(), "E_DATABASE"),
                            (SchemaError(), "E_SCHEMA"), (ModelError(), "E_MODEL"),
                            (HistoryIndexError(), "E_INDEX"), (ValueError(), "E_INTERNAL"),
                            (KeyError(), "E_INTERNAL"), (RuntimeError(), "E_INTERNAL")):
            with self.subTest(code=code):
                self.assertEqual(core._error_code(error), code)

    def test_every_code_has_reason_and_msg_includes_it(self):
        import title_dispatcher
        cases = {
            "E_INVALID": InvalidInput(),
            "E_DATABASE": DatabaseError(),
            "E_SCHEMA": SchemaError(),
            "E_MODEL": ModelError(),
            "E_INDEX": HistoryIndexError(),
            "E_SESSIONNOTFOUND": title_dispatcher.SessionNotFound(),
            "E_AMBIGUOUSTITLE": title_dispatcher.AmbiguousTitle(),
            "E_INTERNAL": ValueError(),
        }
        for code, error in cases.items():
            msg = core._error_msg(error, "test")
            self.assertIn(code, msg)
            self.assertIn(errors.ERROR_REASONS[code], msg)

    def test_error_codes_json_matches_runtime_contract(self):
        """error_codes.json 是代码库文档副本：键集合必须与 ERROR_REASONS 一致，且每条有 reason/fix。

        两者是双源常量（文档 vs 代码），改一处忘另一处会静默漂移，所以在这里钉死。
        """
        path = Path(__file__).resolve().parents[1] / "error_codes.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(doc), set(errors.ERROR_REASONS))
        for code, item in doc.items():
            with self.subTest(code=code):
                self.assertTrue(item.get("reason"))
                self.assertTrue(item.get("fix"))

    def test_boundary_preserves_nested_model_failure(self):
        with self.assertRaises(ModelError):
            with error_boundary(DatabaseError, "write"):
                raise ModelError("embedding")

    def test_missing_table_and_paginated_existing_table(self):
        db = Mock()
        db.open_table.side_effect = ValueError("missing")
        db.list_tables.side_effect = [SimpleNamespace(tables=["a"], page_token="next"),
                                     SimpleNamespace(tables=[], page_token=None)]
        self.assertIsNone(dbmod._open_or_none(db))
        db.list_tables.side_effect = [SimpleNamespace(tables=["a"], page_token="next"),
                                     SimpleNamespace(tables=[config.TABLE], page_token=None)]
        with self.assertRaises(DatabaseError):
            dbmod._open_or_none(db)

    def test_database_failures_do_not_become_empty_results(self):
        db = Mock()
        db.open_table.side_effect = OSError("unavailable")
        db.list_tables.return_value = SimpleNamespace(tables=[], page_token=None)
        with self.assertRaises(DatabaseError):
            dbmod._open_or_none(db)
        db.list_tables.side_effect = PermissionError("denied")
        with self.assertRaises(DatabaseError):
            dbmod._open_or_none(db)
        with patch.object(lancedb, "connect", side_effect=ValueError("library failure")):
            with self.assertRaises(DatabaseError):
                dbmod._ensure_db()

    def test_title_database_failure_is_not_no_match(self):
        with self.assertRaises(DatabaseError):
            title_cache.find_ids_by_title("title")
        with self.assertRaises(DatabaseError):
            title_cache.fetch_from_zcode("sess_A")

    def test_read_and_write_failures(self):
        self.remember()
        qb = Mock()
        qb.to_batches.side_effect = ValueError("backend read")
        query = Mock()
        query.select.return_value = qb
        tbl = Mock()
        tbl.search.return_value = query
        with self.assertRaises(DatabaseError):
            dbmod._summary_rows(tbl)  # 生产在用读路径，读失败应包成 DatabaseError
        tbl.add.side_effect = OSError("write")
        with patch.object(core, "_open_or_none", return_value=tbl):
            with self.assertRaises(DatabaseError):
                self.remember(round=8)

    def test_missing_index_is_explicit(self):
        dbmod._ensure_db().create_table(config.TABLE, schema=dbmod.Msg)
        with self.assertRaises(HistoryIndexError):
            core.recall("query")

    def test_index_creation_failure_keeps_error_category(self):
        db = Mock()
        db.create_table.return_value.create_index.side_effect = RuntimeError("index failed")
        with patch.object(core, "_ensure_db", return_value=db), \
                patch.object(core, "_open_or_none", return_value=None):
            with self.assertRaises(HistoryIndexError):
                self.remember()

    def test_invalid_numbers_are_input_errors(self):
        with self.assertRaises(InvalidInput):
            self.remember(round="bad")
        with self.assertRaises(InvalidInput):
            core.recall("alpha", top_k="bad")
        self.remember()
        with self.assertRaises(InvalidInput):
            core.recent_messages(limit="bad")

    def test_http_nonobject_payload_and_invalid_round(self):
        for payload in ([], None, {}, {"text": "alpha", "round": "bad"}):
            with self.subTest(payload=payload):
                code, result = core._handle_remember(payload)
                self.assertEqual(code, 400)
                self.assertEqual(result["error"], f"失败 [E_INVALID]：{errors.ERROR_REASONS['E_INVALID']}")

    def test_logs_exclude_payload_and_stdout(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        try:
            raise ValueError("SECRET_PAYLOAD")
        except ValueError as error:
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                result = core._error_msg(error, "mcp.recall")
        self.assertEqual(result, f"失败 [E_INTERNAL]：{errors.ERROR_REASONS['E_INTERNAL']}")
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("SECRET_PAYLOAD", stderr.getvalue())
        self.assertIn("mcp.recall", stderr.getvalue())
        self.assertIn("ValueError", stderr.getvalue())
        self.assertIn("test_logs_exclude_payload", stderr.getvalue())


class ModelBoundaryTests(IsolatedCase):
    real_models = True

    def test_embedding_load_failure(self):
        with patch.object(bgem3_embedding, "_load", side_effect=ValueError("bad model")):
            with self.assertRaises(ModelError):
                dbmod._emb.compute_source_embeddings(["alpha"])

    def test_reranker_load_failure(self):
        with patch.object(reranker, "_load", side_effect=ValueError("bad model")):
            with self.assertRaises(ModelError):
                reranker.score("missing", "alpha", ["alpha"])
