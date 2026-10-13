#!/usr/bin/env python3
"""把离线合成市场导出成 CSV，用来演示 ``provider = csv`` 的用法。

用法：
    python3 tools/make_sample_csv.py                  # 默认写到 data/sample_csv
    python3 tools/make_sample_csv.py --out /tmp/csv --symbols 300

生成的目录结构：
    <out>/<TICKER>.csv      date,open,high,low,close,volume
    <out>/symbols.csv       ticker,name,exchange,security_type,market_cap,sector,sector_etf
    <out>/fundamentals.csv  ticker,revenue_growth_yoy,eps_growth_yoy,profit_margin,current_ratio
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from us_low_scan.config import load_settings  # noqa: E402
from us_low_scan.providers import get_provider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导出示例 CSV 行情数据")
    parser.add_argument("--out", default=str(ROOT / "data" / "sample_csv"))
    parser.add_argument("--config", default=str(ROOT / "config" / "config.ini"))
    parser.add_argument("--symbols", type=int, default=120)
    parser.add_argument("--days", type=int, default=420)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--with-fundamentals", action="store_true",
                        help="同时导出 fundamentals.csv（演示 quality_enabled=true）")
    args = parser.parse_args()

    settings = load_settings(args.config)
    data_source = replace(
        settings.data_source,
        provider="sample",
        sample_symbols=args.symbols,
        sample_days=args.days,
        sample_seed=args.seed if args.seed is not None else settings.data_source.sample_seed,
    )
    settings = replace(settings, data_source=data_source)
    provider = get_provider("sample", settings)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    symbols = provider.list_symbols()
    rng = random.Random(data_source.sample_seed)

    written = 0
    with open(out / "symbols.csv", "w", newline="", encoding="utf-8") as meta:
        writer = csv.writer(meta)
        writer.writerow([
            "ticker", "name", "exchange", "security_type", "market_cap",
            "sector", "sector_etf", "is_otc", "listing_status",
        ])
        for symbol in symbols:
            writer.writerow([
                symbol.ticker, symbol.name, symbol.exchange, symbol.security_type,
                symbol.market_cap or "", symbol.sector, symbol.sector_etf or "",
                "true" if symbol.is_otc else "false", symbol.listing_status,
            ])
            bars = provider.get_daily_bars(symbol.ticker)
            if not bars:
                continue
            with open(out / f"{symbol.ticker}.csv", "w", newline="", encoding="utf-8") as handle:
                bar_writer = csv.writer(handle)
                bar_writer.writerow(["date", "open", "high", "low", "close", "volume"])
                for bar in bars:
                    bar_writer.writerow([
                        bar.day.isoformat(), f"{bar.open:.4f}", f"{bar.high:.4f}",
                        f"{bar.low:.4f}", f"{bar.close:.4f}", f"{bar.volume:.0f}",
                    ])
            written += 1

    if args.with_fundamentals:
        with open(out / "fundamentals.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "ticker", "revenue_growth_yoy", "eps_growth_yoy",
                "profit_margin", "current_ratio",
            ])
            for symbol in symbols:
                writer.writerow([
                    symbol.ticker,
                    f"{rng.uniform(-0.25, 0.45):.4f}",
                    f"{rng.uniform(-0.35, 0.65):.4f}",
                    f"{rng.uniform(-0.15, 0.32):.4f}",
                    f"{rng.uniform(0.6, 3.2):.2f}",
                ])

    print(f"已导出 {written} 只股票的 CSV 到 {out}")
    print("把 config/config.ini 改成：")
    print("    [data_source]")
    print("    provider = csv")
    print(f"    csv_dir  = {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
