"""测试公共工具：构造临时配置、合成 K 线。"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from us_low_scan import calendar_us  # noqa: E402
from us_low_scan.config import load_settings  # noqa: E402
from us_low_scan.models import Bar, Symbol  # noqa: E402

CONFIG_PATH = ROOT / "config" / "config.ini"


def test_settings(tmp_dir: Path, **data_source_overrides):
    """加载真实配置，但把数据库/报告目录指向临时目录，并使用离线数据源。"""
    settings = load_settings(CONFIG_PATH)
    data_source = replace(
        settings.data_source, provider="sample", **data_source_overrides
    )
    return replace(
        settings,
        data_source=data_source,
        general=replace(
            settings.general,
            database=tmp_dir / "test.db",
            output_dir=tmp_dir / "reports",
        ),
    )


def make_bars(
    symbol: str,
    closes: Sequence[float],
    start: Optional[date] = None,
    volumes: Optional[Sequence[float]] = None,
    wick: float = 0.01,
) -> List[Bar]:
    """用收盘价序列造一段连续的日线（日期按交易日推进）。"""
    start = start or date(2025, 1, 2)
    days = calendar_us.trading_days(start, start + timedelta(days=len(closes) * 2))[: len(closes)]
    bars = []
    for index, close in enumerate(closes):
        volume = volumes[index] if volumes else 1_000_000.0
        bars.append(
            Bar(
                symbol=symbol,
                day=days[index],
                open=close * (1 - wick / 2),
                high=close * (1 + wick),
                low=close * (1 - wick),
                close=close,
                volume=volume,
                source="test",
                adjustment="split",
            )
        )
    return bars


def declining_then_flat(n: int = 300, start: float = 100.0, decline: float = 0.004, up: float = 0.002, tail: int = 40) -> List[float]:
    """先下跌再走平/微升，用于制造"低位企稳"形态。"""
    closes = [start]
    for i in range(1, n):
        rate = -decline if i < n - tail else up
        closes.append(closes[-1] * (1 + rate))
    return closes


def symbol(ticker: str, **kwargs) -> Symbol:
    base = dict(
        ticker=ticker,
        name=f"{ticker} Inc",
        exchange="NYSE",
        security_type="common_stock",
        is_otc=False,
        market_cap=5e9,
        sector="Technology",
        sector_etf="XLK",
        source="test",
    )
    base.update(kwargs)
    return Symbol(**base)
