"""Nasdaq 数据源测试（全部离线：把 HTTP 调用替换成固定样本）。"""

import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from tests.support import test_settings

from us_low_scan.providers import nasdaq as nasdaq_module
from us_low_scan.providers.nasdaq import NasdaqProvider, _clean_name, _parse_date, _to_float

SCREENER_FIXTURE = {
    "data": {
        "rows": [
            {"symbol": "AAPL", "name": "Apple Inc. Common Stock",
             "marketCap": "3500000000000.00", "sector": "Technology"},
            {"symbol": "MCD", "name": "McDonald's Corporation Common Stock",
             "marketCap": "220000000000.00", "sector": "Consumer Discretionary"},
            {"symbol": "ACMEW", "name": "Acme Acquisition Corp. Warrant",
             "marketCap": "0.00", "sector": "Finance"},
        ]
    }
}

HISTORY_FIXTURE = {
    "data": {
        "tradesTable": {
            "rows": [
                {"date": "10/09/2026", "close": "$336.64", "volume": "37,881,200",
                 "open": "$331.695", "high": "$338.61", "low": "$330.70"},
                {"date": "10/08/2026", "close": "$334.10", "volume": "28,000,000",
                 "open": "$333.00", "high": "$335.00", "low": "$331.00"},
            ]
        }
    }
}


class TestParsers(unittest.TestCase):
    def test_to_float_strips_currency_and_commas(self):
        self.assertEqual(_to_float("$1,234.50"), 1234.5)
        self.assertEqual(_to_float("37,881,200"), 37881200.0)
        self.assertIsNone(_to_float("N/A"))
        self.assertIsNone(_to_float(None))

    def test_parse_date_mm_dd_yyyy(self):
        self.assertEqual(_parse_date("10/09/2026"), date(2026, 10, 9))
        self.assertIsNone(_parse_date("2026-10-09"))
        self.assertIsNone(_parse_date(""))

    def test_clean_name(self):
        cases = {
            "Apple Inc. Common Stock": "Apple Inc",
            "Alphabet Inc. Class A Common Stock": "Alphabet Inc Class A",
            "AstraZeneca PLC American Depositary Shares": "AstraZeneca PLC",
            "Some Co Ordinary Shares": "Some Co",
            "Just A Name": "Just A Name",
        }
        for raw, expected in cases.items():
            self.assertEqual(_clean_name(raw), expected, raw)


class TestProvider(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self._tmp.name))
        self.settings = replace(
            settings,
            data_source=replace(settings.data_source, provider="nasdaq"),
        )
        self.provider = NasdaqProvider(self.settings)
        self._original = nasdaq_module.http_json

    def tearDown(self):
        nasdaq_module.http_json = self._original
        self._tmp.cleanup()

    def _patch(self, payload):
        def fake(url, **kwargs):
            if "screener" in url:
                return SCREENER_FIXTURE
            return HISTORY_FIXTURE
        nasdaq_module.http_json = fake

    def test_list_symbols(self):
        self._patch(None)
        symbols = {s.ticker: s for s in self.provider.list_symbols()}
        self.assertIn("AAPL", symbols)
        self.assertEqual(symbols["AAPL"].name, "Apple Inc")
        self.assertEqual(symbols["AAPL"].market_cap, 3.5e12)
        self.assertEqual(symbols["AAPL"].sector, "Technology")
        # 接口不提供交易所 -> 留空，由阶段 A 标注"未覆盖"
        self.assertEqual(symbols["AAPL"].exchange, "")
        # 权证要被标成非普通股
        self.assertEqual(symbols["ACMEW"].security_type, "non_common")

    def test_get_daily_bars(self):
        self._patch(None)
        bars = self.provider.get_daily_bars("AAPL")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].day, date(2026, 10, 8))
        self.assertAlmostEqual(bars[-1].close, 336.64)
        self.assertAlmostEqual(bars[-1].volume, 37881200.0)
        self.assertEqual(bars[-1].source, "nasdaq")
        self.assertEqual(bars[-1].adjustment, "split")

    def test_bars_are_cached_per_instance(self):
        calls = {"n": 0}

        def counting(url, **kwargs):
            calls["n"] += 1
            return SCREENER_FIXTURE if "screener" in url else HISTORY_FIXTURE

        nasdaq_module.http_json = counting
        self.provider.get_daily_bars("AAPL")
        self.provider.get_daily_bars("AAPL")
        self.assertEqual(calls["n"], 1, "同一只股票不应该重复请求")

    def test_etf_fallback_for_benchmark(self):
        """SPY 不在股票清单里，第一次请求会返回空，第二次要用 etf 口径再试。"""
        seen = []

        def fake(url, **kwargs):
            asset_class = (kwargs.get("params") or {}).get("assetclass")
            seen.append(asset_class)
            if asset_class == "etf":
                return HISTORY_FIXTURE
            return {"data": {"tradesTable": {"rows": []}}}

        nasdaq_module.http_json = fake
        bars = self.provider.get_daily_bars("SPY")
        self.assertEqual(seen, ["stocks", "etf"])
        self.assertEqual(len(bars), 2)

    def test_multi_fetch_survives_single_failure(self):
        def flaky(url, **kwargs):
            if "screener" in url:
                return SCREENER_FIXTURE
            if "/MSFT/" in url:
                raise RuntimeError("boom")
            return HISTORY_FIXTURE

        nasdaq_module.http_json = flaky
        result = self.provider.get_bars_multi(["AAPL", "MSFT"])
        self.assertEqual(len(result["AAPL"]), 2)
        self.assertEqual(result["MSFT"], [], "单只失败不能影响整轮")

    def test_empty_screener_raises(self):
        nasdaq_module.http_json = lambda url, **kwargs: {"data": {"rows": []}}
        with self.assertRaises(Exception):
            NasdaqProvider(self.settings).list_symbols()


if __name__ == "__main__":
    unittest.main()
