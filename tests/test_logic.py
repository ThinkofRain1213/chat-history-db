import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import title_dispatcher
import title_validate
import title_cache
from tests.support import IsolatedCase
import core
import db
import errors
import mcp_tools
import timeutil


class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 7, 16, 0, 0)
        return value if tz is None else value.replace(tzinfo=tz)


class TimeTests(unittest.TestCase):
    def test_calendar_ranges(self):
        cases = {
            "2026": ("2026-01-01 00:00:00", "2027-01-01 00:00:00"),
            "2024-02-29": ("2024-02-29 00:00:00", "2024-03-01 00:00:00"),
            "09-07": ("2026-09-07 00:00:00", "2026-09-08 00:00:00"),
            "09:00": ("2026-09-07 09:00:00", "2026-09-07 16:00:00"),
            "23:00-01:00": ("2026-09-07 23:00:00", "2026-09-08 01:00:00"),
            "2026-09-07 09:00-10:00": ("2026-09-07 09:00:00", "2026-09-07 10:00:00"),
            "17:00": ("2026-09-07 17:00:00", "2026-09-07 17:00:00"),
        }
        with patch.object(datetime, "datetime", FixedDateTime):
            for text, expected in cases.items():
                with self.subTest(text=text):
                    self.assertEqual(timeutil._parse_time_range(text), expected)

    def test_invalid_ranges(self):
        for text in ("", "bad", "2025-02-29", "13-01", "24:00", "09:60", "09:00-25:00"):
            with self.subTest(text=text):
                self.assertIsNone(timeutil._parse_time_range(text))

    def test_epoch_is_beijing_time(self):
        self.assertEqual(timeutil._epoch_to_time_str(0), "1970-01-01 08:00:00")

    def test_now_naive_is_naive(self):
        self.assertIsNone(timeutil._now_naive().tzinfo)

    def test_now_naive_matches_beijing_cur_time(self):
        # _cur_time_str() 为北京 epoch 字符串；_now_naive 格式化后应与它同一分钟（北京时间语义一致）
        self.assertEqual(timeutil._dt_to_str(timeutil._now_naive())[:16],
                         timeutil._cur_time_str()[:16])


class TitleValidateTests(unittest.TestCase):
    """c（title_validate.check）单测：校验程序（读源+比对），返回 (state, source)。"""

    def test_check_id_kind(self):
        with patch.object(title_cache, "fetch_from_zcode", return_value="t1"):
            self.assertEqual(title_validate.check("id", None, "s1"), (1, "t1"))   # 未命中但源存在
        with patch.object(title_cache, "fetch_from_zcode", return_value="t1"):
            self.assertEqual(title_validate.check("id", "t1", "s1"), (1, None))   # 命中且对齐
        with patch.object(title_cache, "fetch_from_zcode", return_value="t2"):
            self.assertEqual(title_validate.check("id", "t1", "s1"), (2, "t2"))   # 命中但不一致→2，带回源
        with patch.object(title_cache, "fetch_from_zcode", return_value=""):
            self.assertEqual(title_validate.check("id", None, "s1"), (0, None))   # id 无行→0

    def test_check_title_kind(self):
        with patch.object(title_cache, "find_ids_by_title", return_value=["a"]):
            self.assertEqual(title_validate.check("title", None, "T"), (1, ["a"]))  # 未命中但源存在
        with patch.object(title_cache, "find_ids_by_title", return_value=["a"]):
            self.assertEqual(title_validate.check("title", ["a"], "T"), (1, None))  # 命中且对齐
        with patch.object(title_cache, "find_ids_by_title", return_value=["a", "b"]):
            self.assertEqual(title_validate.check("title", ["a"], "T"), (2, ["a", "b"]))  # 不一致→2
        with patch.object(title_cache, "find_ids_by_title", return_value=[]):
            self.assertEqual(title_validate.check("title", None, "T"), (0, None))  # title 无匹配→0


class SessionTests(IsolatedCase):
    def test_unscoped_and_explicit_sessions(self):
        self.assertIsNone(title_dispatcher.resolve_session(None))
        self.assertIsNone(title_dispatcher.resolve_session(" "))
        self.assertEqual(title_dispatcher.resolve_session("sess_A"), "sess_A")
        self.assertEqual(title_dispatcher.resolve_session("current", "sess_A"), "sess_A")

    def test_unique_title(self):
        with patch.object(title_cache, "find_ids_by_title", return_value=["sess_A"]):
            self.assertEqual(title_dispatcher.resolve_session("A"), "sess_A")

    def test_missing_and_ambiguous_titles(self):
        for ids, error in (([], title_dispatcher.SessionNotFound),
                           (["sess_A", "sess_B"], title_dispatcher.AmbiguousTitle)):
            with self.subTest(ids=ids), patch.object(title_cache, "find_ids_by_title", return_value=ids):
                with self.assertRaises(error):
                    title_dispatcher.resolve_session("A")

    def test_current_without_context_must_not_broaden_scope(self):
        with self.assertRaises(ValueError):
            title_dispatcher.resolve_session("current", None)

    def test_resolve_reverse_falls_back_to_cache_when_zcode_down(self):
        # resolve 会把 (id->title) 写进单一映射；之后 ZCode 挂，反向扫描缓存得出 id
        with patch.object(title_cache, "find_ids_by_title",
                          side_effect=[["sess_A"], RuntimeError("zcode down")]):
            self.assertEqual(title_dispatcher.resolve_session("A"), "sess_A")
            self.assertEqual(title_dispatcher.resolve_session("A"), "sess_A")  # 校验失败 → 反查缓存

    def test_resolve_reverse_detects_ambiguous_when_not_cached(self):
        # 标题未缓存时按 ZCode 当前结果判定：多个 → AmbiguousTitle
        with patch.object(title_cache, "find_ids_by_title",
                          return_value=["sess_A", "sess_B"]):
            with self.assertRaises(title_dispatcher.AmbiguousTitle):
                title_dispatcher.resolve_session("A")

    def test_kind_sets(self):
        self.assertEqual(db._kind_set(None), {"user", "final"})
        self.assertEqual(db._kind_set("all"), {"user", "final", "mid", "tool"})
        self.assertEqual(db._kind_set(" user, final,user "), {"user", "final"})

    def test_context_metadata(self):
        for meta in ({"session_id": "sess_A"},
                     {"com.zcode/request-context": {"session_id": "sess_A"}},
                     SimpleNamespace(model_dump=lambda: {"session_id": "sess_A"})):
            with self.subTest(meta=meta):
                ctx = SimpleNamespace(request_context=SimpleNamespace(meta=meta))
                self.assertEqual(mcp_tools._extract_session_id(ctx), "sess_A")

    def test_context_params_and_missing(self):
        params = SimpleNamespace(_meta={"session_id": "sess_A"})
        ctx = SimpleNamespace(request_context=SimpleNamespace(meta=None, params=params))
        self.assertEqual(mcp_tools._extract_session_id(ctx), "sess_A")
        self.assertIsNone(mcp_tools._extract_session_id(None))
        self.assertIsNone(mcp_tools._extract_session_id(SimpleNamespace()))

    def test_current_error_codes(self):
        for error, code in ((errors.InvalidInput(), "E_INVALID"),
                            (title_dispatcher.SessionNotFound(), "E_SESSIONNOTFOUND"),
                            (title_dispatcher.AmbiguousTitle(), "E_AMBIGUOUSTITLE")):
            self.assertEqual(core._error_msg(error), f"失败 [{code}]：{errors.ERROR_REASONS[code]}")
