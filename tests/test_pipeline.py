"""端到端扫描测试（离线合成数据）与"不使用未来数据"的验证。"""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from tests.support import make_bars, test_settings

from us_low_scan import backtest as backtest_module
from us_low_scan import calendar_us, db, features as feature_module, pipeline, report
from us_low_scan.providers import get_provider


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        base = test_settings(self.tmp)
        # 小规模合成市场，保证测试秒级完成
        self.settings = replace(
            base,
            data_source=replace(base.data_source, sample_symbols=60, sample_days=320),
        )
        self.provider = get_provider("sample", self.settings)
        self.conn = db.connect(self.settings.general.database)
        db.init_db(self.conn)
        self.session = calendar_us.last_completed_session()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestScan(PipelineCase):
    def test_full_scan_produces_groups_and_reasons(self):
        result = pipeline.run_scan(
            self.settings, conn=self.conn, provider=self.provider
        )
        self.assertEqual(result.session, self.session)
        self.assertIsNone(result.skipped_reason)
        self.assertGreater(result.universe_size, 0)
        self.assertTrue(result.stabilized or result.watch)
        self.assertLessEqual(
            len(result.stabilized), self.settings.output.max_main_candidates
        )
        for candidate in result.candidates:
            self.assertTrue(candidate.reasons, f"{candidate.symbol} 缺少入选理由")
            self.assertTrue(candidate.invalidation)
            self.assertTrue(candidate.risk_tags)
            self.assertLessEqual(candidate.score.total, candidate.score.max_available + 1e-6)
            self.assertEqual(candidate.features.session, self.session)

    def test_every_candidate_has_traceable_metrics(self):
        result = pipeline.run_scan(self.settings, conn=self.conn, provider=self.provider)
        for candidate in result.candidates:
            features = candidate.features
            self.assertIsNotNone(features.distance_from_low_pct)
            self.assertIsNotNone(features.drawdown_from_high_pct)
            self.assertIsNotNone(features.avg_dollar_volume_20d)
            self.assertIsNotNone(features.rsi14)
            self.assertIsNotNone(features.high_52w)
            self.assertLessEqual(features.distance_from_low_pct, self.settings.filters.near_low_pct + 1e-9)
            self.assertGreaterEqual(
                features.drawdown_from_high_pct, self.settings.filters.min_drawdown_from_high_pct - 1e-9
            )
            self.assertGreaterEqual(
                features.avg_dollar_volume_20d, self.settings.universe.min_avg_dollar_volume_20d
            )
            self.assertGreaterEqual(features.close, self.settings.universe.min_price)

    def test_results_are_deterministic(self):
        first = pipeline.run_scan(self.settings, provider=self.provider, persist=False)
        second = pipeline.run_scan(self.settings, provider=self.provider, persist=False)
        self.assertEqual(
            [c.symbol for c in first.candidates], [c.symbol for c in second.candidates]
        )
        self.assertEqual(
            [c.score.total for c in first.candidates],
            [c.score.total for c in second.candidates],
        )

    def test_skip_on_non_trading_day(self):
        result = pipeline.run_scan(
            self.settings, session=date(2026, 10, 10), provider=self.provider, persist=False
        )
        self.assertIsNotNone(result.skipped_reason)
        self.assertIn("不是美股交易日", result.skipped_reason)

    def test_skip_when_data_not_updated(self):
        """行情最新日期早于目标交易日时必须跳过，而不是用陈旧数据出名单。"""
        class StaleProvider:
            name = "stale"
            coverage_note = "测试用陈旧数据源"

            def __init__(self, provider):
                self._provider = provider

            def list_symbols(self):
                return self._provider.list_symbols()

            def get_bars_multi(self, symbols, **kwargs):
                bars = self._provider.get_bars_multi(symbols)
                return {
                    s: [b for b in v if b.day <= date(2026, 9, 30)] for s, v in bars.items()
                }

            def get_fundamentals(self, symbol):
                return None

        result = pipeline.run_scan(
            self.settings,
            session=date(2026, 10, 9),
            provider=StaleProvider(self.provider),
            persist=False,
        )
        self.assertIsNotNone(result.skipped_reason)
        self.assertIn("尚未更新", result.skipped_reason)

    def test_persists_candidates_and_features(self):
        result = pipeline.run_scan(self.settings, conn=self.conn, provider=self.provider)
        rows = db.load_candidates(self.conn, self.session)
        self.assertEqual(len(rows), len(result.candidates))
        self.assertGreater(db.bar_count(self.conn), 0)
        outcome = self.conn.execute(
            "SELECT COUNT(*) AS c FROM daily_features"
        ).fetchone()
        self.assertGreater(outcome["c"], 0)

    def test_rejections_are_explained(self):
        result = pipeline.run_scan(self.settings, provider=self.provider, persist=False)
        self.assertTrue(result.rejected)
        for row in result.rejected:
            self.assertTrue(row["symbol"])
            self.assertTrue(row["stage"])
            self.assertTrue(row["reason"])


class TestNoLookahead(PipelineCase):
    """最关键的一条：任何一天的结果都不能用到该日之后的数据。"""

    def test_features_ignore_future_bars(self):
        symbol = self.provider.list_symbols()[0].ticker
        bars = self.provider.get_daily_bars(symbol)
        session = bars[-30].day
        truncated = [b for b in bars if b.day <= session]
        full = feature_module.compute_features(symbol, bars, session, 252)
        cut = feature_module.compute_features(symbol, truncated, session, 252)
        self.assertEqual(full.as_row(), cut.as_row())

    def test_scan_result_unchanged_when_future_data_is_hidden(self):
        session = calendar_us.shift_trading_days(self.session, -25)
        symbols = self.provider.list_symbols()
        full_bars = self.provider.get_bars_multi([s.ticker for s in symbols])
        with_future = pipeline.run_scan(
            self.settings, session=session, provider=self.provider, persist=False
        )
        hidden = backtest_module.HistoricalView(self.provider, full_bars, session, symbols)
        without_future = pipeline.run_scan(
            self.settings, session=session, provider=hidden, persist=False
        )
        self.assertEqual(
            [c.symbol for c in with_future.candidates],
            [c.symbol for c in without_future.candidates],
        )
        self.assertEqual(
            [round(c.score.total, 6) for c in with_future.candidates],
            [round(c.score.total, 6) for c in without_future.candidates],
        )


class TestReport(PipelineCase):
    def test_written_files_and_required_fields(self):
        result = pipeline.run_scan(self.settings, conn=self.conn, provider=self.provider)
        paths = report.write_outputs(result, self.settings)
        for key in ("markdown", "csv", "json", "rejected_csv", "latest"):
            self.assertIn(key, paths)
            self.assertTrue(paths[key].exists(), f"{key} 未写出")

        markdown = paths["markdown"].read_text(encoding="utf-8")
        # 排版照搬产品构想第 6 节的报告示例：整份报告在一个引用块里
        self.assertIn("> **美股低位观察名单｜交易日：", markdown)
        self.assertIn("> 数据更新：", markdown)
        self.assertIn("America/New_York", markdown)
        self.assertIn("> 市场环境：", markdown)
        # 默认偏好是"买跌"，所以 A 组是低位观察组、B 组才是企稳组
        self.assertIn("> **A. 低位观察组", markdown)
        self.assertIn("> **B. 已出现企稳迹象", markdown)
        self.assertIn("> **今日结论：**", markdown)
        self.assertIn("**数据与覆盖说明**", markdown)
        # 构想第 6 节要求的字段一个都不能少
        for field in ("最新行情：", "低位：", "流动性：", "技术信号：", "市场对比：",
                      "企稳证据：", "综合分数：", "入选理由：", "风险：",
                      "退出观察条件：", "资料来源："):
            self.assertIn(f"- {field}", markdown, f"缺少字段 {field}")
        self.assertIn("不代表上涨概率", markdown)
        self.assertIn("不是投资建议", markdown)
        if result.candidates:
            self.assertIn(result.candidates[0].symbol, markdown)

    def test_markdown_is_readable_not_a_wall_of_text(self):
        """排版回归：一行一个字段，不允许出现超长段落。"""
        result = pipeline.run_scan(self.settings, conn=self.conn, provider=self.provider)
        markdown = report.render_markdown(result, self.settings)
        longest = max(len(line) for line in markdown.splitlines())
        self.assertLess(longest, 200, f"最长的一行有 {longest} 字符，排版又退化了")
        # 每只股票是一行标题 + 缩进子项
        self.assertIn(">   - 退出观察条件：", markdown)
        self.assertIn(">     - ", markdown)
        # 引用块之外只有最后的免责声明
        outside = [l for l in markdown.splitlines() if l and not l.startswith(">")]
        self.assertEqual(len(outside), 1, f"引用块外多出了内容：{outside}")

    def test_brief_output_is_plain_text(self):
        result = pipeline.run_scan(self.settings, conn=self.conn, provider=self.provider)
        paths = report.write_outputs(result, self.settings)
        self.assertIn("brief", paths)
        brief = paths["brief"].read_text(encoding="utf-8")
        self.assertIn("A 组 低位观察", brief)
        self.assertNotIn("|", brief)

    def test_csv_has_expected_columns(self):
        import csv as csv_module

        result = pipeline.run_scan(self.settings, conn=self.conn, provider=self.provider)
        paths = report.write_outputs(result, self.settings)
        with open(paths["csv"], encoding="utf-8-sig") as handle:
            reader = csv_module.DictReader(handle)
            self.assertEqual(reader.fieldnames, report.CANDIDATE_COLUMNS)
            rows = list(reader)
        self.assertEqual(len(rows), len(result.candidates))

    def test_json_round_trip_renders_same_content(self):
        result = pipeline.run_scan(self.settings, conn=self.conn, provider=self.provider)
        paths = report.write_outputs(result, self.settings)
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        rebuilt = report.scan_result_from_dict(data)
        self.assertEqual(
            [c.symbol for c in rebuilt.candidates], [c.symbol for c in result.candidates]
        )
        self.assertEqual(report.render_markdown(rebuilt, self.settings),
                         report.render_markdown(result, self.settings))

    def test_empty_scan_says_no_candidates(self):
        strict = replace(
            self.settings,
            filters=replace(
                self.settings.filters,
                near_low_pct=0.05,
                min_drawdown_from_high_pct=95.0,
            ),
        )
        result = pipeline.run_scan(strict, provider=self.provider, persist=False)
        markdown = report.render_markdown(result, strict)
        self.assertIn("暂无符合条件的主要候选", markdown)
        self.assertIn("宁可不给名单", markdown)


if __name__ == "__main__":
    unittest.main()
