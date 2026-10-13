"""交易日历测试：假日、提前收市、已收盘交易日的判定。"""

import unittest
from datetime import date, datetime

from tests.support import calendar_us


class TestCalendar(unittest.TestCase):
    def test_weekends_are_not_trading_days(self):
        self.assertFalse(calendar_us.is_trading_day(date(2026, 10, 10)))  # 周六
        self.assertFalse(calendar_us.is_trading_day(date(2026, 10, 11)))  # 周日
        self.assertTrue(calendar_us.is_trading_day(date(2026, 10, 9)))    # 周五

    def test_fixed_holidays(self):
        self.assertFalse(calendar_us.is_trading_day(date(2026, 1, 1)))    # 元旦
        self.assertFalse(calendar_us.is_trading_day(date(2026, 12, 25)))  # 圣诞
        self.assertFalse(calendar_us.is_trading_day(date(2026, 6, 19)))   # Juneteenth
        self.assertFalse(calendar_us.is_trading_day(date(2026, 7, 3)))    # 7/4 为周六 -> 周五休市

    def test_floating_holidays(self):
        self.assertFalse(calendar_us.is_trading_day(date(2026, 1, 19)))   # MLK：1 月第 3 个周一
        self.assertFalse(calendar_us.is_trading_day(date(2026, 2, 16)))   # 华盛顿诞辰
        self.assertFalse(calendar_us.is_trading_day(date(2026, 5, 25)))   # 阵亡将士纪念日
        self.assertFalse(calendar_us.is_trading_day(date(2026, 9, 7)))    # 劳动节
        self.assertFalse(calendar_us.is_trading_day(date(2026, 11, 26)))  # 感恩节
        self.assertFalse(calendar_us.is_trading_day(date(2026, 4, 3)))    # 耶稣受难日

    def test_new_year_observed_in_previous_year(self):
        # 2022-01-01 是周六 -> 2021-12-31（周五）休市
        self.assertFalse(calendar_us.is_trading_day(date(2021, 12, 31)))
        # 2028-01-01 是周六 -> 2027-12-31 休市
        self.assertFalse(calendar_us.is_trading_day(date(2027, 12, 31)))

    def test_early_closes(self):
        self.assertTrue(calendar_us.is_early_close(date(2026, 11, 27)))   # 黑五
        self.assertTrue(calendar_us.is_early_close(date(2026, 12, 24)))   # 平安夜
        self.assertEqual(calendar_us.close_time(date(2026, 11, 27)).hour, 13)
        self.assertEqual(calendar_us.close_time(date(2026, 10, 9)).hour, 16)

    def test_previous_and_next_trading_day_skip_holidays(self):
        self.assertEqual(
            calendar_us.previous_trading_day(date(2026, 12, 28)), date(2026, 12, 24)
        )
        self.assertEqual(
            calendar_us.next_trading_day(date(2026, 12, 24)), date(2026, 12, 28)
        )

    def test_last_completed_session_uses_market_timezone(self):
        # 2026-10-09（周五）美东 16:30 -> 当天已收盘
        moment = datetime(2026, 10, 9, 16, 30, tzinfo=calendar_us.MARKET_TZ)
        self.assertEqual(calendar_us.last_completed_session(moment), date(2026, 10, 9))
        # 美东 10:00 仍在盘中 -> 应返回上一个交易日（10-08）
        moment = datetime(2026, 10, 9, 10, 0, tzinfo=calendar_us.MARKET_TZ)
        self.assertEqual(calendar_us.last_completed_session(moment), date(2026, 10, 8))
        # 周六 -> 上周五
        moment = datetime(2026, 10, 10, 9, 0, tzinfo=calendar_us.MARKET_TZ)
        self.assertEqual(calendar_us.last_completed_session(moment), date(2026, 10, 9))

    def test_utc_input_is_converted(self):
        # 新加坡时间 2026-10-10 05:00 = 美东 2026-10-09 17:00（已收盘）
        from zoneinfo import ZoneInfo

        moment = datetime(2026, 10, 10, 5, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(calendar_us.last_completed_session(moment), date(2026, 10, 9))

    def test_shift_trading_days(self):
        session = calendar_us.last_completed_session(
            datetime(2026, 10, 9, 18, 0, tzinfo=calendar_us.MARKET_TZ)
        )
        self.assertEqual(session, date(2026, 10, 9))
        self.assertEqual(calendar_us.shift_trading_days(session, 5), date(2026, 10, 16))
        self.assertEqual(calendar_us.shift_trading_days(session, -1), date(2026, 10, 8))


if __name__ == "__main__":
    unittest.main()
