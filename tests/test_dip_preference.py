"""买跌 / 买涨偏好测试 —— 这是"为什么推荐了涨的股票"的直接修复。

核心逻辑：止跌信号（站上均线、RSI 回到 50 上方）本身就意味着价格已经在回升，
所以对"买跌不买涨"的人来说，主名单必须是"位置够低、且还没涨起来"的那一组。
"""

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from tests.support import test_settings

from us_low_scan import db, pipeline, report, scoring
from us_low_scan.config import load_settings
from us_low_scan.models import Features
from tests.support import CONFIG_PATH

SESSION = date(2026, 10, 9)


def features(**kwargs) -> Features:
    base = dict(
        symbol="TEST", session=SESSION, close=50.0, dollar_volume=8e7,
        avg_dollar_volume_20d=1e8, volume_cv_20d=0.3, dollar_volume_ratio_20d=1.0,
        high_52w=100.0, low_52w=48.0, distance_from_low_pct=4.2,
        drawdown_from_high_pct=50.0, change_1m_pct=-3.0, change_3m_pct=-15.0,
        change_6m_pct=-35.0, ma20=49.0, ma50=52.0, ma200=70.0,
        vs_ma20_pct=-1.0, vs_ma50_pct=-3.8, vs_ma200_pct=-28.6,
        rsi14=45.0, rsi14_slope_5d=1.0, higher_low_pct=0.5, new_low_streak=0,
        volume_up_down_ratio=1.0, volatility_20d=0.35, volatility_60d=0.30,
        volatility_ratio=1.16, max_single_day_move_20d=6.0,
    )
    base.update(kwargs)
    return Features(**base)


class DipCase(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(CONFIG_PATH)

    def with_filters(self, **overrides):
        return replace(self.settings, filters=replace(self.settings.filters, **overrides))


class TestReboundReason(DipCase):
    def test_still_falling_is_not_rebound(self):
        self.assertIsNone(scoring.rebound_reason(features(), self.settings))

    def test_recent_surge_is_rebound(self):
        reason = scoring.rebound_reason(features(change_1m_pct=25.0), self.settings)
        self.assertIsNotNone(reason)
        self.assertIn("近 1 个月已涨", reason)

    def test_three_month_surge_is_rebound(self):
        reason = scoring.rebound_reason(features(change_3m_pct=45.0), self.settings)
        self.assertIn("近 3 个月已涨", reason)

    def test_high_rsi_is_rebound(self):
        reason = scoring.rebound_reason(features(rsi14=72.0), self.settings)
        self.assertIn("RSI", reason)

    def test_thresholds_come_from_config(self):
        relaxed = self.with_filters(max_gain_1m_pct=50.0, max_rsi_for_dip=90.0)
        self.assertIsNone(
            scoring.rebound_reason(features(change_1m_pct=25.0, rsi14=72.0), relaxed)
        )

    def test_require_below_ma50(self):
        strict = self.with_filters(require_below_ma50=True, max_rsi_for_dip=100.0,
                                   max_gain_1m_pct=999, max_gain_3m_pct=999)
        reason = scoring.rebound_reason(features(vs_ma50_pct=2.0), strict)
        self.assertIn("50 日均线", reason)
        self.assertIsNone(scoring.rebound_reason(features(vs_ma50_pct=-2.0), strict))

    def test_off_disables_the_filter(self):
        off = self.with_filters(rebound_action="off")
        self.assertIsNone(scoring.rebound_reason(features(change_1m_pct=99.0), off))


class TestGroupingByPreference(DipCase):
    def _classify(self, feat, settings):
        score = scoring.score_all(feat, settings)
        return scoring.classify_group(feat, score, [], settings)

    def test_dip_mode_puts_unstabilized_in_the_main_list(self):
        self.assertEqual(scoring.primary_group(self.settings), "watch")

    def test_stabilized_mode_puts_stabilized_in_the_main_list(self):
        settings = self.with_filters(dip_preference="stabilized")
        self.assertEqual(scoring.primary_group(settings), "stabilized")

    def test_rebounded_never_enters_the_main_list(self):
        hot = features(change_1m_pct=30.0)
        self.assertEqual(self._classify(hot, self.settings)[0], "excluded")

    def test_rebounded_can_be_demoted_instead_of_excluded(self):
        settings = self.with_filters(rebound_action="demote")
        self.assertEqual(self._classify(features(change_1m_pct=30.0), settings)[0], "rebounded")

    def test_stabilized_stock_goes_to_the_watch_bucket_in_dip_mode(self):
        calm = features(rsi14=52.0, rsi14_slope_5d=5.0, higher_low_pct=1.5,
                        vs_ma20_pct=1.0, vs_ma50_pct=1.0, volume_up_down_ratio=1.4,
                        new_low_streak=0)
        self.assertEqual(self._classify(calm, self.settings)[0], "stabilized")


class TestScanOrdering(unittest.TestCase):
    """用合成市场跑一遍，确认 A 组真的都是"还没涨起来"的。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        base = test_settings(cls.tmp)
        cls.settings = replace(
            base, data_source=replace(base.data_source, sample_symbols=60, sample_days=320)
        )
        from us_low_scan.providers import get_provider

        cls.provider = get_provider("sample", cls.settings)
        cls.conn = db.connect(cls.settings.general.database)
        db.init_db(cls.conn)
        cls.scan = pipeline.run_scan(cls.settings, conn=cls.conn, provider=cls.provider)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._tmp.cleanup()

    def test_primary_list_is_all_un_rebounded(self):
        self.assertTrue(self.scan.primary, "买跌主名单不该为空（合成数据里有下跌股）")
        for candidate in self.scan.primary:
            self.assertIsNone(
                scoring.rebound_reason(candidate.features, self.settings),
                f"{candidate.symbol} 已经涨起来了，不该进主名单",
            )

    def test_primary_contains_only_watch_group(self):
        for candidate in self.scan.primary:
            self.assertEqual(candidate.group, "watch")

    def test_rebounded_stocks_are_flagged_not_listed(self):
        for candidate in self.scan.flagged:
            reason = scoring.rebound_reason(candidate.features, self.settings)
            self.assertTrue(
                reason or candidate.group_reason,
                f"{candidate.symbol} 在已排除名单里但没有给出理由",
            )

    def test_report_headings_follow_the_preference(self):
        markdown = report.render_markdown(self.scan, self.settings)
        self.assertIn("A. 低位观察组", markdown)
        self.assertIn("买跌（dip_preference = dip）", markdown)
        self.assertIn("近 1 个月", markdown)

    def test_stabilized_preference_flips_the_headings(self):
        flipped = replace(
            self.settings,
            filters=replace(self.settings.filters, dip_preference="stabilized"),
        )
        result = pipeline.run_scan(flipped, provider=self.provider, persist=False)
        self.assertEqual(result.primary_group, "stabilized")
        markdown = report.render_markdown(result, flipped)
        self.assertIn("A. 低位企稳组", markdown)


class TestCatchUp(unittest.TestCase):
    """关机 / 睡眠错过定时任务后的补跑。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        base = test_settings(self.tmp)
        self.settings = replace(
            base,
            data_source=replace(base.data_source, sample_symbols=40, sample_days=300),
            general=replace(base.general, catch_up_max_days=5),
        )
        from us_low_scan.providers import get_provider

        self.provider = get_provider("sample", self.settings)
        self.conn = db.connect(self.settings.general.database)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_pending_sessions_when_never_run(self):
        pending = pipeline.pending_sessions(self.conn, self.settings)
        self.assertEqual(len(pending), 5)   # 受 catch_up_max_days 限制
        self.assertEqual(pending[-1], pipeline.calendar_us.last_completed_session())

    def test_catch_up_fills_every_missed_session(self):
        results = pipeline.catch_up(self.conn and self.settings, conn=self.conn,
                                    provider=self.provider)
        self.assertEqual(len(results), 5)
        sessions = sorted(db.list_candidate_sessions(self.conn))
        self.assertEqual(len(sessions), 5)

    def test_nothing_pending_after_catch_up(self):
        pipeline.catch_up(self.settings, conn=self.conn, provider=self.provider)
        self.assertEqual(pipeline.pending_sessions(self.conn, self.settings), [])

    def test_catch_up_does_not_use_future_data(self):
        """补跑 2026-09-25 那天时，不能看到 09-28 之后的行情。"""
        results = pipeline.catch_up(self.settings, conn=self.conn, provider=self.provider)
        first = results[0]
        for candidate in first.primary + first.secondary:
            self.assertLessEqual(candidate.features.session, first.session)
            bars = self.provider.get_daily_bars(candidate.symbol)
            cutoff = [b for b in bars if b.day <= first.session]
            self.assertAlmostEqual(candidate.features.close, cutoff[-1].close, places=6)


if __name__ == "__main__":
    unittest.main()
