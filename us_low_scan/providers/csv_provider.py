"""CSV 数据源：把任何供应商导出的日线数据喂给本工具。

目录结构
--------
``<csv_dir>/<TICKER>.csv``，每个文件一根 K 线一行，列名（大小写不敏感）：

    date,open,high,low,close,volume

可选列：``adj_close``（若存在，则用 ``close`` 计算涨跌、``adj_close`` 计算
收益率序列会混口径，本工具的做法是：**统一使用 ``adj_close``（若存在）作为
价格序列**，成交量按用户提供值使用，并在报告里注明所用口径）。

可选：``<csv_dir>/symbols.csv`` 提供股票池元信息，列名：
    ticker,name,exchange,security_type,market_cap,sector,sector_etf,is_otc,listing_status

把 ``config.ini`` 里的 ``data_source.provider`` 改成 ``csv`` 并设置
``data_source.csv_dir`` 即可使用。
"""

from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from .. import calendar_us
from ..models import Bar, Symbol
from .base import DataProviderError, MarketDataProvider

_DATE_FIELDS = ("date", "day", "timestamp", "time", "datetime")
_PRICE_FIELDS = ("adj_close", "adjclose", "adjusted_close", "close")


def _normalize(field: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (field or "").strip().lower())


def _parse_date(raw: str) -> date:
    text = (raw or "").strip()
    if not text:
        raise ValueError("空日期")
    # 兼容 2026-10-09 / 2026/10/09 / 20261009 / 2026-10-09T00:00:00Z
    text = text.replace("/", "-").split("T")[0].split(" ")[0]
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


class CsvProvider(MarketDataProvider):
    name = "csv"
    coverage_note = "本地 CSV 数据；覆盖范围与调整口径取决于数据提供方，请自行核对。"

    def __init__(self, settings):
        super().__init__(settings)
        cfg = settings.data_source
        if not cfg.csv_dir:
            raise DataProviderError("[data_source] csv_dir 未设置")
        self.dir = Path(cfg.csv_dir)
        if not self.dir.exists():
            raise DataProviderError(f"CSV 目录不存在：{self.dir}")
        self.date_column = cfg.csv_date_column
        self._symbols: Optional[List[Symbol]] = None
        self._bars: Dict[str, List[Bar]] = {}

    # ------------------------------------------------------------------
    def _symbol_file(self) -> Optional[Path]:
        candidate = self.dir / "symbols.csv"
        return candidate if candidate.exists() else None

    def list_symbols(self) -> List[Symbol]:
        if self._symbols is not None:
            return list(self._symbols)

        symbols: List[Symbol] = []
        meta = self._symbol_file()
        if meta:
            with open(meta, newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    norm = {_normalize(k): (v or "").strip() for k, v in row.items()}
                    ticker = (norm.get("ticker") or norm.get("symbol") or "").upper()
                    if not ticker:
                        continue
                    symbols.append(
                        Symbol(
                            ticker=ticker,
                            name=norm.get("name", ""),
                            exchange=norm.get("exchange", ""),
                            security_type=norm.get("securitytype", "common_stock") or "common_stock",
                            is_otc=norm.get("isotc", "false").lower() in ("1", "true", "yes"),
                            market_cap=_to_float(norm.get("marketcap")),
                            sector=norm.get("sector", ""),
                            sector_etf=norm.get("sectoretf") or None,
                            listing_status=norm.get("listingstatus", "active") or "active",
                            source="csv",
                        )
                    )
        else:
            # 没有元信息文件：用文件名字段兜底。缺少市值/交易所时，报告会提示
            # "元信息缺失"，阶段 A 里市值门槛无法判断 -> 由调用方决定如何处理。
            for path in sorted(self.dir.glob("*.csv")):
                if path.name.lower() == "symbols.csv":
                    continue
                ticker = path.stem.upper()
                symbols.append(
                    Symbol(
                        ticker=ticker,
                        name=ticker,
                        exchange="UNKNOWN",
                        security_type="common_stock",
                        is_otc=False,
                        market_cap=None,
                        sector="",
                        listing_status="active",
                        source="csv",
                    )
                )
        self._symbols = symbols
        return list(symbols)

    # ------------------------------------------------------------------
    def get_daily_bars(
        self, symbol: str, start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Bar]:
        symbol = symbol.upper()
        if symbol not in self._bars:
            self._bars[symbol] = self._read_file(symbol)
        bars = self._bars[symbol]
        if start:
            bars = [b for b in bars if b.day >= start]
        if end:
            bars = [b for b in bars if b.day <= end]
        return list(bars)

    def _read_file(self, symbol: str) -> List[Bar]:
        path = self.dir / f"{symbol}.csv"
        if not path.exists():
            # 允许小写文件名
            matches = list(self.dir.glob(f"{symbol.lower()}.csv"))
            if not matches:
                return []
            path = matches[0]

        bars: List[Bar] = []
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return []
            mapping = {_normalize(f): f for f in reader.fieldnames}

            date_key = None
            for field in (self.date_column,) + _DATE_FIELDS:
                if _normalize(field) in mapping:
                    date_key = mapping[_normalize(field)]
                    break
            if date_key is None:
                raise DataProviderError(f"{path}: 找不到日期列（期望 {self.date_column}）")

            close_key = None
            for field in _PRICE_FIELDS:
                if field in mapping:
                    close_key = mapping[field]
                    break
            if close_key is None:
                raise DataProviderError(f"{path}: 找不到收盘价列")

            open_key = mapping.get("open")
            high_key = mapping.get("high")
            low_key = mapping.get("low")
            volume_key = mapping.get("volume") or mapping.get("vol")

            for row in reader:
                try:
                    day = _parse_date(row[date_key])
                except (ValueError, TypeError):
                    continue
                close = _to_float(row.get(close_key))
                if close is None or close <= 0:
                    continue
                volume = _to_float(row.get(volume_key)) if volume_key else None
                volume = volume or 0.0
                open_ = _to_float(row.get(open_key)) if open_key else None
                high = _to_float(row.get(high_key)) if high_key else None
                low = _to_float(row.get(low_key)) if low_key else None
                bars.append(
                    Bar(
                        symbol=symbol,
                        day=day,
                        open=open_ if open_ is not None else close,
                        high=high if high is not None else close,
                        low=low if low is not None else close,
                        close=close,
                        volume=volume,
                        source="csv",
                        adjustment="adj_close" if close_key != mapping.get("close") else "raw",
                    )
                )
        bars.sort(key=lambda b: b.day)
        return bars

    def get_fundamentals(self, symbol: str):
        """可选：``<csv_dir>/fundamentals.csv``（列：ticker,revenue_growth,profit_margin,...）。"""
        path = self.dir / "fundamentals.csv"
        if not path.exists():
            return None
        with open(path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                norm = {_normalize(k): v for k, v in row.items()}
                if (norm.get("ticker") or "").strip().upper() == symbol.upper():
                    return {k: _to_float(v) for k, v in norm.items() if k != "ticker"}
        return None


def _to_float(raw) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("$", "")
    if text in ("", "-", "nan", "NaN", "null", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None
