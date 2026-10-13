"""配置解析、股票池过滤、市值与流动性门槛测试。"""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.support import CONFIG_PATH, symbol, test_settings

from us_low_scan import universe
from us_low_scan.config import ConfigError, load_settings
from us_low_scan.models import Features


class TestConfig(unittest.TestCase):
    def test_loads_all_sections(self):
        settings = load_settings(CONFIG_PATH)
        self.assertEqual(settings.universe.min_price, 5.0)
        self.assertEqual(settings.universe.min_avg_dollar_volume_20d, 10_000_000)
        self.assertEqual(settings.filters.lookback_days, 252)
        self.assertEqual(
            settings.scoring.technical_max,
            settings.scoring.weight_liquidity
            + settings.scoring.weight_low_position
            + settings.scoring.weight_stabilization,
        )

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load_settings("/definitely/not/here/config.ini")

    def test_env_expansion_with_default(self):
        """未设置环境变量时使用 ${VAR:-默认值} 里的默认值。"""
        os.environ.pop("SEC_USER_AGENT", None)
        settings = load_settings(CONFIG_PATH)
        self.assertIn("you@example.com", settings.data_source.sec_user_agent)

    def test_env_expansion_from_environment(self):
        """设置了环境变量则覆盖默认值。"""
        previous = os.environ.get("SEC_USER_AGENT")
        os.environ["SEC_USER_AGENT"] = "tester (contact: t@example.com)"
        try:
            settings = load_settings(CONFIG_PATH)
            self.assertIn("tester", settings.data_source.sec_user_agent)
        finally:
            if previous is None:
                os.environ.pop("SEC_USER_AGENT", None)
            else:
                os.environ["SEC_USER_AGENT"] = previous

    def test_invalid_bool_is_rejected(self):
        text = Path(CONFIG_PATH).read_text(encoding="utf-8").replace(
            "include_adr = false", "include_adr = maybe"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.ini"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)

    def test_weight_mismatch_is_rejected(self):
        text = Path(CONFIG_PATH).read_text(encoding="utf-8").replace(
            "liquidity_volume_share = 20", "liquidity_volume_share = 19"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.ini"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)

    def test_unknown_provider_is_rejected(self):
        import re

        # 不要写死默认值（默认数据源会变），直接改 [data_source] 里的 provider 行
        text = re.sub(
            r"(?m)^(provider\s*=\s*)\S+", r"\1yahoo",
            Path(CONFIG_PATH).read_text(encoding="utf-8"), count=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.ini"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)


class TestUniverse(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = test_settings(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_otc_and_type_and_market_cap_filters(self):
        symbols = [
            symbol("GOOD"),
            symbol("OTC1", is_otc=True),
            symbol("ETF1", security_type="etf", name="Sample ETF"),
            symbol("ADR1", security_type="adr"),
            symbol("WARR", name="Good Warrant Series A"),
            symbol("SMALL", market_cap=5e8),
            symbol("NOCAP", market_cap=None),
            symbol("HALT", listing_status="halted"),
            symbol("BADE", exchange="OTC"),
        ]
        result = universe.filter_instruments(symbols, self.settings)
        kept = {s.ticker for s in result.kept}
        rejected = {r.symbol: r.reason for r in result.rejected}
        self.assertIn("GOOD", kept)
        self.assertIn("NOCAP", kept)          # 缺失市值默认放行但要打标签
        self.assertNotIn("OTC1", kept)
        self.assertNotIn("ETF1", kept)
        self.assertNotIn("ADR1", kept)
        self.assertNotIn("WARR", kept)
        self.assertNotIn("SMALL", kept)
        self.assertNotIn("HALT", kept)
        self.assertNotIn("BADE", kept)
        self.assertTrue(result.notes)

    def test_adr_can_be_included_by_config(self):
        settings = replace(
            self.settings,
            universe=replace(self.settings.universe, include_adr=True),
        )
        result = universe.filter_instruments([symbol("ADR1", security_type="adr")], settings)
        self.assertEqual([s.ticker for s in result.kept], ["ADR1"])

    def test_missing_market_cap_can_exclude(self):
        settings = replace(
            self.settings,
            universe=replace(self.settings.universe, missing_market_cap_policy="exclude"),
        )
        result = universe.filter_instruments([symbol("NOCAP", market_cap=None)], settings)
        self.assertEqual(result.kept, [])

    def test_liquidity_filter(self):
        settings = self.settings
        cheap = Features(symbol="A", session=__import__("datetime").date(2026, 10, 9),
                         close=4.0, dollar_volume=1e6, avg_dollar_volume_20d=5e7)
        thin = Features(symbol="B", session=__import__("datetime").date(2026, 10, 9),
                        close=50.0, dollar_volume=1e5, avg_dollar_volume_20d=2e6)
        fine = Features(symbol="C", session=__import__("datetime").date(2026, 10, 9),
                        close=50.0, dollar_volume=1e7, avg_dollar_volume_20d=5e7)
        lookup = {"A": symbol("A"), "B": symbol("B"), "C": symbol("C")}
        kept, rejected = universe.filter_liquidity([cheap, thin, fine], settings, lookup)
        self.assertEqual([f.symbol for f in kept], ["C"])
        reasons = {r.symbol: r.reason for r in rejected}
        self.assertIn("股价", reasons["A"])
        self.assertIn("均额", reasons["B"])

    def test_stale_data_is_rejected(self):
        import datetime

        stale = Features(symbol="S", session=datetime.date(2026, 10, 9), close=50.0,
                         dollar_volume=1e7, avg_dollar_volume_20d=5e7, data_stale_days=30)
        kept, rejected = universe.filter_liquidity([stale], self.settings, {"S": symbol("S")})
        self.assertEqual(kept, [])
        self.assertIn("陈旧", rejected[0].reason)

    def test_top_by_liquidity(self):
        import datetime

        features = [
            Features(symbol=f"S{i}", session=datetime.date(2026, 10, 9), close=10.0,
                     dollar_volume=1e6, avg_dollar_volume_20d=float(i))
            for i in range(5)
        ]
        top = universe.top_by_liquidity(features, 3)
        self.assertEqual([f.symbol for f in top], ["S4", "S3", "S2"])
        self.assertEqual(len(universe.top_by_liquidity(features, 0)), 5)

    def test_watchlist_and_market_cap_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = Path(tmp) / "watch.csv"
            watch.write_text("ticker\nAAA\nBBB\n", encoding="utf-8")
            self.assertEqual(universe.load_watchlist(watch), {"AAA", "BBB"})
            caps = Path(tmp) / "caps.csv"
            caps.write_text("ticker,market_cap\nAAA,1500000000\n", encoding="utf-8")
            self.assertEqual(universe.load_market_caps(caps), {"AAA": 1.5e9})
            merged = universe.apply_market_caps([symbol("AAA", market_cap=None)], {"AAA": 1.5e9})
            self.assertEqual(merged[0].market_cap, 1.5e9)


if __name__ == "__main__":
    unittest.main()
