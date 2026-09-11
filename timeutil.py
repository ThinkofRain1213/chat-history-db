# -*- coding: utf-8 -*-
"""时间工具：统一用固定东八区(_TZ8)计算，返回可读北京时间字符串。"""
import datetime as _dt
import re
import time

from config import _TZ8, _TIME_FMT


def _dt_to_str(dt: _dt.datetime) -> str:
    """格式化 datetime → "YYYY-MM-DD HH:MM:SS"。

    约定：只接收 naive 的北京时间（本工具统一由 _now_naive 等构造）；若传入带时区的 aware
    datetime，它会按其自身时区字符串化（不向东八区归一），请勿误用。
    """
    return dt.strftime(_TIME_FMT)


def _epoch_to_time_str(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(int(epoch), tz=_TZ8).strftime(_TIME_FMT)


def _cur_time_str() -> str:
    return _epoch_to_time_str(int(time.time()))


def _now_naive() -> _dt.datetime:
    """当前北京时间，返回 naive datetime（去掉 tzinfo，供 _dt_to_str 直接格式化）。

    统一用固定 +8 的 _TZ8 取时间，避免依赖操作系统本地时区的 datetime.now()。
    """
    return _dt.datetime.now(tz=_TZ8).replace(tzinfo=None)


def _hhmm(text: str) -> _dt.time | None:
    """'HH:MM' → time（时 0-23、分 0-59，分钟必须两位）；解析不了返回 None。"""
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return _dt.time(hour, minute)


def _day_span(year: int, month: int, day: int):
    """某日的段 [当日 00:00, 次日 00:00)；日期不存在返回 None。"""
    try:
        start = _dt.datetime(year, month, day)
    except ValueError:
        return None
    return start, start + _dt.timedelta(days=1)


def _month_span(year: int, month: int):
    """某月的段 [1 日 00:00, 次月 1 日 00:00)；月份越界返回 None。"""
    if not 1 <= month <= 12:
        return None
    start = _dt.datetime(year, month, 1)
    end = _dt.datetime(year + 1, 1, 1) if month == 12 else _dt.datetime(year, month + 1, 1)
    return start, end


def _bound(text: str, now: _dt.datetime):
    """端点串 → (形态, 缺什么, 取值)；解析不了返回 None。

    形态：'span' 日期级整段 / 'moment' 日期时刻 / 'bare' 纯时刻。
    取值：span → (段首, 段尾)，moment → 该时刻，bare → time。
    缺什么：None 不缺 / 'year' 缺年（按今年）/ 'ymd' 缺年月日（按今天）。

    只按写法定形态，不做"向另一端借"的推断——两端缺法是否一致由调用方判。
    """
    t = text.strip()
    if not t:
        return None
    # 日期 + 时刻（YYYY-MM-DD HH:MM / MM-DD HH:MM）
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\S+)$", t)
    if m:
        span = _day_span(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        clock = _hhmm(m.group(4))
        if span is None or clock is None:
            return None
        return ("moment", None, _dt.datetime.combine(span[0].date(), clock))
    m = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\S+)$", t)
    if m:
        span = _day_span(now.year, int(m.group(1)), int(m.group(2)))
        clock = _hhmm(m.group(3))
        if span is None or clock is None:
            return None
        return ("moment", "year", _dt.datetime.combine(span[0].date(), clock))
    # 纯时刻（缺年月日）
    clock = _hhmm(t)
    if clock is not None:
        return ("bare", "ymd", clock)
    # 日期级整段：整年 / 整月 / 整天
    m = re.match(r"^(\d{4})$", t)
    if m:
        year = int(m.group(1))
        return ("span", None, (_dt.datetime(year, 1, 1), _dt.datetime(year + 1, 1, 1)))
    m = re.match(r"^(\d{4})-(\d{1,2})$", t)
    if m:
        span = _month_span(int(m.group(1)), int(m.group(2)))
        return None if span is None else ("span", None, span)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
    if m:
        span = _day_span(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return None if span is None else ("span", None, span)
    # 缺年的整天（按今年）
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", t)
    if m:
        span = _day_span(now.year, int(m.group(1)), int(m.group(2)))
        return None if span is None else ("span", "year", span)
    return None


def _value(bound, side: str, now: _dt.datetime) -> _dt.datetime:
    """端点贡献的时刻：段取段首（左）/段尾（右），时刻就是那一刻，纯时刻按今天。"""
    kind, _lack, payload = bound
    if kind == "span":
        return payload[0] if side == "left" else payload[1]
    if kind == "moment":
        return payload
    return _dt.datetime.combine(now.date(), payload)


def _parse_time_range(range_str: str) -> tuple[str, str] | None:
    """range → (start_str, end_str)（北京时间 "YYYY-MM-DD HH:MM:SS"，可直接字符串比较）。

    恒左闭右开 [起, 止)。两种写法：
      单值（不含 `/`，只接受日期级整段）：'YYYY' 整年 / 'YYYY-MM' 整月 /
        'YYYY-MM-DD' 整天 / 'MM-DD' 整天（缺年按今年）。
      区间（必须含 `/`）：'起点/终点'，或 '起点/'（终点留空 = 到此刻）。

    补全只补更粗的粒度——缺年按今年、缺年月日按今天，**不从另一端借**；因此
    **两端缺法必须一致**（都写全 / 都缺年 / 都缺年月日），一端有一端没有则非法。
    段取值左端取段首、右端取段尾；止 < 起（负宽）非法，止 == 起（零宽）合法。
    解析失败返回 None。
    """
    s = range_str.strip()
    if not s:
        return None
    now = _now_naive()
    if "/" not in s:
        bound = _bound(s, now)
        if bound is None or bound[0] != "span":
            return None
        start, end = bound[2]
        return (_dt_to_str(start), _dt_to_str(end))
    if s.count("/") != 1:
        return None
    left_text, right_text = s.split("/")
    if not left_text.strip():
        return None
    left = _bound(left_text, now)
    if left is None:
        return None
    if not right_text.strip():
        end = now  # 右端留空 = 到此刻（不受"缺法一致"约束）
    else:
        right = _bound(right_text, now)
        if right is None or right[1] != left[1]:
            return None
        end = _value(right, "right", now)
    start = _value(left, "left", now)
    if end < start:
        return None
    return (_dt_to_str(start), _dt_to_str(end))
