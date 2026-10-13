"""美股（NYSE / Nasdaq）交易日历。

为什么自己实现：本工具要求零第三方依赖，而且只需要"是不是交易日 /
上一个交易日 / 最近一个已收盘的交易日"这几个能力。

规则来源：NYSE 官方交易时间与假日页面（见 README 参考资料）。
观察日规则：假日落在周六 -> 前一个周五休市；落在周日 -> 后一个周一休市。
纽约证交所同时有提前收市（13:00 ET）安排：感恩节次日、7 月 3 日、
12 月 24 日（若它们是工作日且不是休市日）。

注意：临时休市（如国葬、极端天气）无法用规则推演，若需要精确到个别年份，
请把额外休市日写进 ``EXTRA_HOLIDAYS`` 或用配置覆盖。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# 规则无法覆盖的临时休市（历史事件，按需补充）
EXTRA_HOLIDAYS = {
    date(2012, 10, 29),  # 飓风 Sandy
    date(2012, 10, 30),
    date(2018, 12, 5),   # 老布什国葬
    date(2025, 1, 9),    # 吉米·卡特国葬
}

NYSE_LAUNCH = date(1792, 5, 17)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """某年某月第 n 个星期 ``weekday``（0=周一）。n 为负数表示倒数第 |n| 个。"""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))
    # 倒数
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset + 7 * (-n - 1))


def _observed(day: date) -> date:
    """周六 -> 提前到周五；周日 -> 顺延到周一。"""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm（Meeus/Jones/Butcher）。"""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def holidays(year: int) -> set:
    """NYSE 全年休市日（含观察日调整）。"""
    result = set()

    result.add(_observed(date(year, 1, 1)))                 # New Year's Day
    result.add(_nth_weekday(year, 1, 0, 3))                 # MLK Day（1986 起）
    result.add(_nth_weekday(year, 2, 0, 3))                 # Washington's Birthday
    result.add(easter_sunday(year) - timedelta(days=2))     # Good Friday
    result.add(_nth_weekday(year, 5, 0, -1))                # Memorial Day
    if year >= 2022:
        result.add(_observed(date(year, 6, 19)))            # Juneteenth
    result.add(_observed(date(year, 7, 4)))                 # Independence Day
    result.add(_nth_weekday(year, 9, 0, 1))                 # Labor Day
    result.add(_nth_weekday(year, 11, 3, 4))                # Thanksgiving
    result.add(_observed(date(year, 12, 25)))               # Christmas

    # 元旦落在周六时，观察日会提前到上一年的 12 月 31 日，需要补进本年的休市表
    if date(year + 1, 1, 1).weekday() == 5:
        result.add(date(year, 12, 31))

    # 观察日有可能被推到相邻年份（例如 12 月 25 日是周日 -> 12 月 26 日本年休市，
    # 这仍在年内；而 1 月 1 日是周六则在上面单独处理），这里统一过滤。
    return {day for day in result if day.year == year}


def early_closes(year: int) -> set:
    """提前收市（13:00 ET）的交易日。"""
    closed = holidays(year)
    candidates = [
        _nth_weekday(year, 11, 3, 4) + timedelta(days=1),  # 黑色星期五
        date(year, 7, 3),
        date(year, 12, 24),
    ]
    return {
        day
        for day in candidates
        if day.weekday() < 5 and day not in closed and day.year == year
    }


_HOLIDAY_CACHE: dict = {}
_EARLY_CACHE: dict = {}


def _holiday_set(year: int) -> set:
    if year not in _HOLIDAY_CACHE:
        _HOLIDAY_CACHE[year] = holidays(year) | {d for d in EXTRA_HOLIDAYS if d.year == year}
    return _HOLIDAY_CACHE[year]


def _early_set(year: int) -> set:
    if year not in _EARLY_CACHE:
        _EARLY_CACHE[year] = early_closes(year)
    return _EARLY_CACHE[year]


def is_trading_day(day: date) -> bool:
    if day < NYSE_LAUNCH:
        return False
    if day.weekday() >= 5:
        return False
    return day not in _holiday_set(day.year)


def is_early_close(day: date) -> bool:
    return is_trading_day(day) and day in _early_set(day.year)


def close_time(day: date) -> time:
    return EARLY_CLOSE if is_early_close(day) else REGULAR_CLOSE


def session_close(day: date) -> datetime:
    """某个交易日的收盘时刻（带美国东部时区）。"""
    return datetime.combine(day, close_time(day), tzinfo=MARKET_TZ)


def previous_trading_day(day: date, inclusive: bool = False) -> date:
    """``day`` 之前的交易日；inclusive=True 时若当天就是交易日则返回当天。"""
    cursor = day if inclusive else day - timedelta(days=1)
    for _ in range(400):
        if is_trading_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    raise RuntimeError(f"无法在 400 天内找到 {day} 之前的交易日")


def next_trading_day(day: date, inclusive: bool = False) -> date:
    cursor = day if inclusive else day + timedelta(days=1)
    for _ in range(400):
        if is_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    raise RuntimeError(f"无法在 400 天内找到 {day} 之后的交易日")


def trading_days(start: date, end: date) -> List[date]:
    """[start, end] 闭区间内的所有交易日。"""
    if end < start:
        return []
    days = []
    cursor = start
    while cursor <= end:
        if is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def last_completed_session(now: Optional[datetime] = None) -> date:
    """最近一个"已经收盘"的交易日。

    收盘后还需要等待数据源更新，因此调用方应再加一段延迟（见
    ``next_run_after_close``）。
    """
    if now is None:
        now = datetime.now(MARKET_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)

    today = now.date()
    if is_trading_day(today) and now.time() >= close_time(today):
        return today
    return previous_trading_day(today)


def next_run_after_close(
    session: date, delay_minutes: int = 45, tz: Optional[ZoneInfo] = None
) -> datetime:
    """某交易日收盘 + 延迟（等数据源更新）后的时刻，用于展示/定时任务。"""
    moment = session_close(session) + timedelta(minutes=delay_minutes)
    if tz is not None:
        moment = moment.astimezone(tz)
    return moment


def shift_trading_days(day: date, offset: int) -> date:
    """把 ``day`` 前后移动 ``offset`` 个交易日（offset 可为负）。"""
    cursor = day
    step = 1 if offset >= 0 else -1
    for _ in range(abs(offset)):
        cursor = next_trading_day(cursor) if step > 0 else previous_trading_day(cursor)
    return cursor


def describe_next_session(now: Optional[datetime] = None) -> str:
    """给报告用的"下一步动作"提示。"""
    last = last_completed_session(now)
    upcoming = next_trading_day(last)
    return (
        f"最近已收盘交易日 {last.isoformat()}；下一交易日 {upcoming.isoformat()}"
        f"（{'提前收市 13:00 ET' if is_early_close(upcoming) else '常规收市 16:00 ET'}）"
    )
