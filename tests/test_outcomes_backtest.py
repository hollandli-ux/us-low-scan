"""前瞻表现跟踪与历史回放测试。"""

import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from tests.support import make_bars, test_settings

from us_low_scan import backtest as backtest_module
from us_low_scan import db, outcomes, pipeline
from us_low_scan.models import Bar
from us_low_scan.providers import get_provider


class TestOutcomeMath(unittest.TestCase):
    def setUp(self):
        self.session = date(2026, 1, 5)
        self.forward = make_bars(
            "X", [100, 105, 95, 110, 120, 118, 130, 140, 135, 150, 160, 155, 170, 180, 175,
                  190, 200, 210, 205, 220, 230],
            start=date(2026, 1, 6),
        )

    def test_returns_and_drawdown(self):
        result = outcomes._measure(
            self.session, "X", 100.0, self.forward, {}, [], 90.0
        )
        self.assertAlmostEqual(result["return_5d"], 120 / 100 - 1)
        self.assertAlmostEqual(result["return_10d"], 150 / 100 - 1)
        self.assertAlmostEqual(result["return_20d"], 220 / 100 - 1)
        # 最大不利波动取窗口内最低价
        self.assertLess(result["max_adverse_5d"], 0)
        self.assertEqual(result["new_low_10d"], 0)  # 未跌破 90

    def test_new_low_flag(self):
        forward = make_bars("X", [95, 85, 90, 92, 96], start=date(2026, 1, 6))
        result = outcomes._measure(self.session, "X", 100.0, forward, {}, [], 90.0)
        self.assertEqual(result["new_low_10d"], 1)

    def test_horizon_used_limited_by_available_data(self):
        short = self.forward[:7]
        result = outcomes._measure(self.session, "X", 100.0, short, {}, [], 50.0)
        self.assertEqual(result["horizon_used"], 7)
        self.assertIsNotNone(result["return_5d"])
        self.assertIsNone(result["return_10d"])
        self.assertIsNone(result["return_20d"])

    def test_summary_statistics(self):
        rows = [
            {"return_20d": 0.10, "spy_return_20d": 0.02, "max_adverse_20d": -0.05, "new_low_10d": 0,
             "return_5d": 0.02, "return_10d": 0.05, "spy_return_5d": 0.01, "spy_return_10d": 0.01,
             "max_adverse_5d": -0.02, "max_adverse_10d": -0.03},
            {"return_20d": -0.10, "spy_return_20d": 0.02, "max_adverse_20d": -0.18, "new_low_10d": 1,
             "return_5d": -0.03, "return_10d": -0.06, "spy_return_5d": 0.01, "spy_return_10d": 0.01,
             "max_adverse_5d": -0.05, "max_adverse_10d": -0.09},
        ]
        summary = outcomes.summarize_rows(rows)
        self.assertEqual(summary["count"], 2)
        self.assertAlmostEqual(summary["horizon_20d"]["mean"], 0.0)
        self.assertAlmostEqual(summary["horizon_20d"]["hit_rate"], 0.5)
        self.assertAlmostEqual(summary["horizon_20d"]["mean_excess_vs_spy"], -0.02)
        self.assertAlmostEqual(summary["new_low_rate_10d"], 0.5)

    def test_empty_summary(self):
        summary = outcomes.summarize_rows([])
        self.assertEqual(summary["count"], 0)
        self.assertIn("暂无", summary["note"])
        self.assertIn("暂无", outcomes.render_summary_markdown(summary))


class OutcomeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        base = test_settings(self.tmp)
        self.settings = replace(
            base,
            data_source=replace(base.data_source, sample_symbols=50, sample_days=300),
        )
        self.provider = get_provider("sample", self.settings)
        self.conn = db.connect(self.settings.general.database)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestOutcomeTracking(OutcomeCase):
    def test_update_outcomes_after_scan(self):
        from us_low_scan import calendar_us

        session = calendar_us.shift_trading_days(calendar_us.last_completed_session(), -30)
        result = pipeline.run_scan(self.settings, session=session, conn=self.conn,
                                   provider=self.provider)
        self.assertTrue(result.candidates)
        updated = outcomes.update_outcomes(self.conn, self.settings)
        self.assertEqual(updated, len(result.candidates))
        summary = outcomes.summarize(self.conn)
        self.assertEqual(summary["count"], len(result.candidates))
        self.assertIsNotNone(summary["horizon_20d"]["mean"])
        markdown = outcomes.render_summary_markdown(summary)
        self.assertIn("平均最大不利波动", markdown)

    def test_no_forward_data_is_not_fabricated(self):
        """最后一个交易日入选的股票还没有前瞻数据，不能凭空生成收益。"""
        from us_low_scan import calendar_us

        pipeline.run_scan(self.settings, conn=self.conn, provider=self.provider)
        updated = outcomes.update_outcomes(self.conn, self.settings)
        self.assertEqual(updated, 0)
        summary = outcomes.summarize(self.conn)
        self.assertEqual(summary["count"], 0)


class TestBacktest(OutcomeCase):
    def test_backtest_runs_and_separates_groups(self):
        from us_low_scan import calendar_us

        end = calendar_us.last_completed_session()
        start = calendar_us.shift_trading_days(end, -15)
        report = backtest_module.run_backtest(
            self.settings, start=start, end=end, provider=self.provider
        )
        self.assertGreater(report["picks"], 0)
        self.assertIn("低位企稳组", report["by_group"])
        self.assertIn("低位但尚未企稳组", report["by_group"])
        self.assertIn("全部低位候选（不分组）", report["by_group"])
        for row in report["rows"]:
            self.assertIsNotNone(row["entry_close"])
            self.assertLessEqual(row["session"], end)

    def test_backtest_has_no_future_leakage_in_entry_price(self):
        """入选价必须等于当日收盘价（而不是窗口内的最优价）。"""
        from us_low_scan import calendar_us

        end = calendar_us.last_completed_session()
        start = calendar_us.shift_trading_days(end, -8)
        report = backtest_module.run_backtest(
            self.settings, start=start, end=end, provider=self.provider
        )
        bars_by_symbol = {
            s.ticker: self.provider.get_daily_bars(s.ticker)
            for s in self.provider.list_symbols()
        }
        for row in report["rows"][:50]:
            day_map = {b.day: b.close for b in bars_by_symbol[row["symbol"]]}
            self.assertAlmostEqual(day_map[row["session"]], row["entry_close"], places=6)

    def test_backtest_report_renders(self):
        from us_low_scan import calendar_us

        end = calendar_us.last_completed_session()
        start = calendar_us.shift_trading_days(end, -12)
        report = backtest_module.run_backtest(
            self.settings, start=start, end=end, provider=self.provider
        )
        markdown = backtest_module.render_backtest_markdown(report, self.settings)
        self.assertIn("历史回放报告", markdown)
        self.assertIn("幸存者偏差", markdown)
        self.assertIn("未来数据泄漏", markdown)


if __name__ == "__main__":
    unittest.main()
