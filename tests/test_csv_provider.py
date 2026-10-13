"""CSV 数据源测试：用供应商导出的表格跑通链路。"""

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from tests.support import test_settings

from us_low_scan import calendar_us, db, pipeline
from us_low_scan.providers import get_provider
from us_low_scan.providers.csv_provider import CsvProvider
from us_low_scan.providers.base import DataProviderError

DAYS = 300


def _write_symbol(csv_dir: Path, ticker: str, start_price: float, decline: float,
                  tail_up: float, adv: float, meta_cap: float) -> None:
    sessions = calendar_us.trading_days(
        calendar_us.last_completed_session() - timedelta(days=int(DAYS * 1.6)),
        calendar_us.last_completed_session(),
    )[-DAYS:]
    price = start_price
    lines = ["date,open,high,low,close,volume"]
    for index, day in enumerate(sessions):
        rate = -decline if index < DAYS - 40 else tail_up
        price *= (1 + rate)
        volume = adv / price
        lines.append(
            f"{day.isoformat()},{price*0.999:.4f},{price*1.01:.4f},{price*0.99:.4f},"
            f"{price:.4f},{volume:.0f}"
        )
    (csv_dir / f"{ticker}.csv").write_text("\n".join(lines), encoding="utf-8")


class TestCsvProvider(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.csv_dir = self.tmp / "csv"
        self.csv_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _settings(self, **overrides):
        settings = test_settings(self.tmp)
        data_source = replace(
            settings.data_source, provider="csv", csv_dir=self.csv_dir, **overrides
        )
        return replace(settings, data_source=data_source)

    def test_reads_bars_and_derives_symbols_from_filenames(self):
        _write_symbol(self.csv_dir, "AAA", 100.0, 0.004, 0.002, 5e7, 5e9)
        provider = CsvProvider(self._settings())
        symbols = provider.list_symbols()
        self.assertEqual([s.ticker for s in symbols], ["AAA"])
        bars = provider.get_daily_bars("AAA")
        self.assertEqual(len(bars), DAYS)
        self.assertEqual(bars[-1].day, calendar_us.last_completed_session())
        self.assertAlmostEqual(bars[-1].dollar_volume, bars[-1].close * bars[-1].volume)

    def test_symbols_file_overrides_metadata(self):
        _write_symbol(self.csv_dir, "AAA", 100.0, 0.004, 0.002, 5e7, 5e9)
        (self.csv_dir / "symbols.csv").write_text(
            "ticker,name,exchange,security_type,market_cap,sector\n"
            "AAA,Alpha Corp,NASDAQ,common_stock,2500000000,Technology\n",
            encoding="utf-8",
        )
        provider = CsvProvider(self._settings())
        symbol = provider.list_symbols()[0]
        self.assertEqual(symbol.exchange, "NASDAQ")
        self.assertEqual(symbol.market_cap, 2.5e9)
        self.assertEqual(symbol.name, "Alpha Corp")

    def test_missing_directory_raises(self):
        settings = self._settings()
        settings = replace(
            settings,
            data_source=replace(settings.data_source, csv_dir=self.tmp / "nope"),
        )
        with self.assertRaises(DataProviderError):
            CsvProvider(settings)

    def test_full_scan_on_csv_data(self):
        # 三只股票：一只低位企稳、一只持续下跌、一只在 52 周高点附近
        _write_symbol(self.csv_dir, "AAA", 100.0, 0.004, 0.002, 5e7, 5e9)
        # BBB 跌完之后走平（不再创新低、也没涨起来）—— 这正是"买跌"想要的那种状态
        _write_symbol(self.csv_dir, "BBB", 100.0, 0.003, 0.0, 4e7, 4e9)
        _write_symbol(self.csv_dir, "CCC", 40.0, 0.0005, 0.0015, 6e7, 6e9)
        (self.csv_dir / "symbols.csv").write_text(
            "ticker,name,exchange,security_type,market_cap,sector\n"
            "AAA,Alpha Corp,NASDAQ,common_stock,5000000000,Technology\n"
            "BBB,Beta Corp,NYSE,common_stock,4000000000,Industrials\n"
            "CCC,Gamma Corp,NYSE,common_stock,6000000000,Health Care\n",
            encoding="utf-8",
        )
        settings = self._settings()
        conn = db.connect(settings.general.database)
        db.init_db(conn)
        provider = get_provider("csv", settings)
        result = pipeline.run_scan(settings, conn=conn, provider=provider)
        conn.close()

        self.assertEqual(result.universe_size, 3)
        everything = {c.symbol for c in result.candidates} | {c.symbol for c in result.flagged}
        self.assertNotIn("CCC", everything)   # 距 52 周高点太近，阶段 B 就淘汰了
        # BBB 跌到位后走平 -> 属于"买跌"对象，应该进主名单
        self.assertIn("BBB", {c.symbol for c in result.primary})
        # AAA 先跌后涨，属于"已经涨起来"，不进主名单（可能在次级或已排除里）
        self.assertNotIn("AAA", {c.symbol for c in result.primary})

    def test_handles_common_export_variants(self):
        # 大小写不同的列名、斜杠日期、千分位数字
        rows = ["Date,Open,High,Low,Close,Volume"]
        sessions = calendar_us.trading_days(date(2025, 1, 1), date(2026, 10, 9))[-260:]
        price = 80.0
        for day in sessions:
            price *= 1.0005
            rows.append(
                f"{day.strftime('%Y/%m/%d')},{price:.2f},{price:.2f},{price:.2f},"
                f"{price:.2f},\"1,234,567\""
            )
        (self.csv_dir / "vz.csv").write_text("\n".join(rows), encoding="utf-8")
        provider = CsvProvider(self._settings())
        bars = provider.get_daily_bars("VZ")
        self.assertEqual(len(bars), 260)
        self.assertAlmostEqual(bars[0].volume, 1234567.0)


if __name__ == "__main__":
    unittest.main()
