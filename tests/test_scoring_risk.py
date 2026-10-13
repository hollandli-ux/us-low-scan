"""评分、分组、风险标签、失效条件测试。"""

import unittest
from dataclasses import replace
from datetime import date

from tests.support import test_settings

from us_low_scan import risk, scoring
from us_low_scan.models import Features, RiskTag, ScoreBreakdown, Symbol

SESSION = date(2026, 10, 9)


def features(**kwargs) -> Features:
    base = dict(
        symbol="TEST",
        session=SESSION,
        close=50.0,
        dollar_volume=8e7,
        avg_dollar_volume_20d=1e8,
        volume_cv_20d=0.3,
        dollar_volume_ratio_20d=1.0,
        high_52w=100.0,
        low_52w=48.0,
        distance_from_low_pct=4.2,
        drawdown_from_high_pct=50.0,
        change_1m_pct=-2.0,
        change_3m_pct=-15.0,
        change_6m_pct=-35.0,
        ma20=49.0,
        ma50=52.0,
        ma200=70.0,
        vs_ma20_pct=2.0,
        vs_ma50_pct=-3.8,
        vs_ma200_pct=-28.6,
        rsi14=48.0,
        rsi14_slope_5d=6.0,
        higher_low_pct=1.5,
        new_low_streak=0,
        volume_up_down_ratio=1.3,
        volatility_20d=0.35,
        volatility_60d=0.30,
        volatility_ratio=1.16,
        max_single_day_move_20d=6.0,
    )
    base.update(kwargs)
    return Features(**base)


class TestLowPositionFilter(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.settings = test_settings(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_pass(self):
        ok, _ = scoring.passes_low_position_filter(features(), self.settings)
        self.assertTrue(ok)

    def test_too_far_from_low(self):
        ok, why = scoring.passes_low_position_filter(
            features(distance_from_low_pct=22.0), self.settings
        )
        self.assertFalse(ok)
        self.assertIn("不够低", why)

    def test_not_deep_enough_drawdown(self):
        ok, why = scoring.passes_low_position_filter(
            features(drawdown_from_high_pct=8.0), self.settings
        )
        self.assertFalse(ok)
        self.assertIn("回调不够深", why)

    def test_missing_data_is_rejected(self):
        ok, why = scoring.passes_low_position_filter(
            features(distance_from_low_pct=None), self.settings
        )
        self.assertFalse(ok)
        self.assertIn("缺少", why)


class TestScoring(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.settings = test_settings(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_score_is_bounded(self):
        score = scoring.score_all(features(), self.settings)
        self.assertLessEqual(score.liquidity, self.settings.scoring.weight_liquidity + 1e-6)
        self.assertLessEqual(score.low_position, self.settings.scoring.weight_low_position + 1e-6)
        self.assertLessEqual(
            score.stabilization, self.settings.scoring.weight_stabilization + 1e-6
        )

    def test_quality_missing_does_not_fill_points(self):
        score = scoring.score_all(features(), self.settings)
        self.assertIsNone(score.quality)
        self.assertFalse(score.quality_available)
        # 基本面缺失 -> 满分是 80，而不是 100
        self.assertEqual(score.max_available, 80.0)
        self.assertLessEqual(score.total, 80.0)
        self.assertTrue(any("基本面" in note for note in score.notes))

    def test_quality_scored_when_enabled(self):
        settings = replace(
            self.settings,
            scoring=replace(
                self.settings.scoring, quality_enabled=True, quality_source="csv"
            ),
        )
        score = scoring.score_all(
            features(),
            settings,
            fundamentals={
                "revenue_growth_yoy": 0.25,
                "eps_growth_yoy": 0.3,
                "profit_margin": 0.18,
                "current_ratio": 2.0,
            },
        )
        self.assertIsNotNone(score.quality)
        self.assertGreater(score.quality, 0)
        self.assertEqual(score.max_available, 100.0)

    def test_liquidity_saturates(self):
        rich = scoring.score_liquidity(
            features(avg_dollar_volume_20d=10 * 10e6, volume_cv_20d=0.0), self.settings
        )
        self.assertAlmostEqual(rich, self.settings.scoring.weight_liquidity)
        thin = scoring.score_liquidity(
            features(avg_dollar_volume_20d=10e6, volume_cv_20d=2.0), self.settings
        )
        self.assertLess(thin, rich)

    def test_deeper_drawdown_scores_more_until_saturation(self):
        shallow = scoring.score_low_position(
            features(drawdown_from_high_pct=25.0, distance_from_low_pct=10.0), self.settings
        )
        deep = scoring.score_low_position(
            features(drawdown_from_high_pct=60.0, distance_from_low_pct=10.0), self.settings
        )
        self.assertGreater(deep, shallow)

    def test_free_fall_gets_penalised(self):
        normal = scoring.score_low_position(features(change_6m_pct=-40.0), self.settings)
        crash = scoring.score_low_position(features(change_6m_pct=-85.0), self.settings)
        self.assertGreater(normal, crash)

    def test_stabilization_rewards_recovery(self):
        recovering = scoring.score_stabilization(
            features(rsi14=52.0, rsi14_slope_5d=8.0, higher_low_pct=2.0,
                     vs_ma20_pct=1.5, vs_ma50_pct=1.0, volume_up_down_ratio=1.6),
            self.settings,
        )
        falling = scoring.score_stabilization(
            features(rsi14=28.0, rsi14_slope_5d=-9.0, higher_low_pct=-6.0, new_low_streak=4,
                     vs_ma20_pct=-9.0, vs_ma50_pct=-14.0, volume_up_down_ratio=0.5),
            self.settings,
        )
        self.assertGreater(recovering, falling)
        self.assertGreater(recovering, self.settings.filters.min_stabilization_score)

    def test_new_low_streak_zeroes_higher_low_points(self):
        with_streak = scoring.score_stabilization(
            features(higher_low_pct=3.0, new_low_streak=3), self.settings
        )
        without = scoring.score_stabilization(
            features(higher_low_pct=3.0, new_low_streak=0), self.settings
        )
        self.assertLess(with_streak, without)


class TestGrouping(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.settings = test_settings(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, feat, tags):
        score = scoring.score_all(feat, self.settings)
        return scoring.classify_group(feat, score, tags, self.settings)

    def test_stabilized_without_high_risk(self):
        group, reason = self._run(features(), [RiskTag("below_ma200", "低于 200 日均线", "info")])
        self.assertEqual(group, "stabilized")
        self.assertEqual(reason, "")

    def test_persistent_new_low_is_demoted(self):
        # 买跌模式下，主名单就是 watch，所以严重风险必须走 flagged 桶才算真正降级
        group, reason = self._run(
            features(new_low_streak=4),
            [RiskTag("persistent_new_low", "持续创新低", "high")],
        )
        self.assertEqual(group, "flagged")
        self.assertIn("创新低", reason)

    def test_extreme_volatility_is_demoted(self):
        group, reason = self._run(
            features(), [RiskTag("extreme_volatility", "异常波动", "high")]
        )
        self.assertEqual(group, "flagged")
        self.assertIn("波动", reason)

    def test_severe_risk_goes_to_watch_in_stabilized_mode(self):
        from us_low_scan import scoring as scoring_module

        self.settings = replace(
            self.settings,
            filters=replace(self.settings.filters, dip_preference="stabilized"),
        )
        feat = features(new_low_streak=4)
        score = scoring_module.score_all(feat, self.settings)
        group, reason = scoring_module.classify_group(
            feat, score, [RiskTag("persistent_new_low", "持续创新低", "high")], self.settings
        )
        self.assertEqual(group, "watch")

    def test_weak_stabilization_goes_to_watch(self):
        weak = features(rsi14=30.0, rsi14_slope_5d=-4.0, higher_low_pct=-4.0,
                        vs_ma20_pct=-6.0, vs_ma50_pct=-9.0, volume_up_down_ratio=0.6)
        group, reason = self._run(weak, [])
        self.assertEqual(group, "watch")
        self.assertIn("企稳证据不足", reason)

    def test_dilution_filing_is_demoted(self):
        group, reason = self._run(features(), [RiskTag("dilution_risk", "重大融资稀释", "high")])
        self.assertEqual(group, "flagged")
        self.assertIn("稀释", reason)


class TestRiskTags(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.settings = test_settings(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _tags(self, feat, **kwargs):
        symbol = kwargs.pop("symbol", Symbol("TEST", market_cap=5e9))
        return risk.build_risk_tags(feat, symbol, self.settings, **kwargs)

    def test_earnings_soon_is_high_severity(self):
        tags = self._tags(
            features(), events={"checked": True, "earnings_next_date": "2026-10-11"}
        )
        codes = {t.code: t.severity for t in tags}
        self.assertEqual(codes.get("earnings_soon"), "high")

    def test_earnings_unknown_when_no_calendar(self):
        tags = self._tags(features(), events={"checked": True})
        self.assertIn("earnings_unverified", {t.code for t in tags})

    def test_unverified_events_are_flagged_not_ignored(self):
        tags = self._tags(features())
        codes = {t.code for t in tags}
        self.assertIn("events_unverified", codes)
        self.assertIn("quality_missing", codes)
        self.assertIn("earnings_unverified", codes)

    def test_missing_market_cap_is_flagged(self):
        tags = risk.build_risk_tags(features(), Symbol("TEST", market_cap=None), self.settings)
        self.assertIn("market_cap_missing", {t.code for t in tags})

    def test_market_regime_tag(self):
        tags = self._tags(features(), market_state={"below_ma": True, "detail": "基准在均线下"})
        self.assertIn("market_regime", {t.code for t in tags})

    def test_volatility_spike_detected(self):
        tags = self._tags(features(volatility_ratio=5.0))
        self.assertIn("extreme_volatility", {t.code for t in tags})

    def test_invalidation_conditions_are_specific(self):
        conditions = risk.build_invalidation_conditions(features(), self.settings)
        self.assertGreaterEqual(len(conditions), 4)
        joined = " ".join(conditions)
        self.assertIn("52 周低点", joined)
        self.assertIn("20 日平均美元成交额", joined)

    def test_market_regime_detects_downtrend(self):
        from tests.support import make_bars

        closes = [100.0 + i * 0.5 for i in range(220)] + [160.0 - i for i in range(20)]
        bars = make_bars("SPY", closes)
        state = risk.market_regime(bars, bars[-1].day, 200)
        self.assertTrue(state["below_ma"])
        self.assertEqual(state["status"], "整体下行环境")


if __name__ == "__main__":
    unittest.main()
