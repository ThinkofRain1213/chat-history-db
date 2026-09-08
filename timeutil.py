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


def _parse_time_range(range_str: str) -> tuple[str, str] | None:
    """range → (start_str, end_str)（北京时间 "YYYY-MM-DD HH:MM:SS"，可直接字符串比较）。

    支持（年 可选；纯时间默认今天；until<=since 视为跨天；since 在未来则取空）：
      'YYYY' / 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM[-HH:MM]'
      'MM-DD' / 'MM-DD HH:MM[-HH:MM]'      （沿用 engram）
      'HH:MM' / 'HH:MM-HH:MM'
    解析失败返回 None。
    """
    s = range_str.strip()
    if not s:
        return None
    now = _now_naive()

    def _tm(base: _dt.datetime, tstr: str) -> tuple[_dt.datetime, _dt.datetime] | None:
        """把时间串 'HH:MM' 或 'HH:MM-HH:MM' 落到 base 所在日期上。"""
        m = re.match(r"^(\d{1,2}):(\d{2})(?:-(\d{1,2}):(\d{2}))?$", tstr)
        if not m:
            return None
        h1, m1 = int(m.group(1)), int(m.group(2))
        if not (0 <= h1 <= 23 and 0 <= m1 <= 59):
            return None
        since = base.replace(hour=h1, minute=m1, second=0, microsecond=0)
        if m.group(3) is not None:
            h2, m2 = int(m.group(3)), int(m.group(4))
            if not (0 <= h2 <= 23 and 0 <= m2 <= 59):
                return None
            until = base.replace(hour=h2, minute=m2, second=0, microsecond=0)
            if until <= since:
                until += _dt.timedelta(days=1)  # 跨天
        else:
            until = now  # "HH:MM" → 到现在
            if since > until:
                until = since  # 未来：空区间
        return (since, until)

    def _whole_day(year, month, day):
        since = _dt.datetime(year, month, day)
        until = since + _dt.timedelta(days=1)
        return (_dt_to_str(since), _dt_to_str(until))

    # YYYY-MM-DD [time]
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(.+))?$", s)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            _dt.date(year, month, day)
        except ValueError:
            return None
        tstr = (m.group(4) or "").strip()
        if not tstr:
            return _whole_day(year, month, day)
        r = _tm(_dt.datetime(year, month, day), tstr)
        return (_dt_to_str(r[0]), _dt_to_str(r[1])) if r else None
    # YYYY（整年）
    m = re.match(r"^(\d{4})$", s)
    if m:
        year = int(m.group(1))
        return (_dt_to_str(_dt.datetime(year, 1, 1)), _dt_to_str(_dt.datetime(year + 1, 1, 1)))
    # MM-DD [time]（沿用 engram；无年、默认今年）
    m = re.match(r"^(\d{1,2})-(\d{1,2})(?:\s+(.+))?$", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            year = _dt.date(now.year, month, day).year
        except ValueError:
            return None
        tstr = (m.group(3) or "").strip()
        if not tstr:
            return _whole_day(year, month, day)
        r = _tm(_dt.datetime(year, month, day), tstr)
        return (_dt_to_str(r[0]), _dt_to_str(r[1])) if r else None
    # 纯时间（默认今天）
    r = _tm(now, s)
    return (_dt_to_str(r[0]), _dt_to_str(r[1])) if r else None
