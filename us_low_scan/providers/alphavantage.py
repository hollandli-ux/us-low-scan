"""Alpha Vantage 日线数据源。

注意额度：免费套餐的请求额度非常有限（历史上为 25 次/天），因此
**不要**用它在收盘后扫描全市场。正确用法是先做 100-300 只的原型验证，
或只对候选股做补充校验。跑到一半额度耗尽时，本工具会明确报错并写入
run_log，而不是安静地少扫几只。

使用的重要参数
--------------
* ``TIME_SERIES_DAILY_ADJUSTED`` + ``outputsize=full`` 返回 20 年日线，
  含 ``adjusted close`` / ``split coefficient`` / ``dividend amount``。
* 低位判定使用 adjusted close 序列（拆股与分红都调整过，前后口径一致）。
* 股票列表来自 ``LISTING_STATUS``（CSV），包含 exchange / assetType /
  delistingDate，可用来剔除已退市标的。

需要的环境变量：``ALPHAVANTAGE_API_KEY``
"""

from __future__ import annotations

import csv
import io
import os
import re
from datetime import date
from typing import Dict, List, Optional

from ..models import Bar, Symbol
from .base import DataProviderError, MarketDataProvider, http_json, http_text

BASE_URL = "https://www.alphavantage.co/query"

_EXCHANGE_MAP = {
    "NYSE": "NYSE",
    "NASDAQ": "NASDAQ",
    "NYSE ARCA": "ARCA",
    "AMEX": "NYSE_AMERICAN",
    "BATS": "BATS",
    "OTC": "OTC",
    "PNK": "OTC",
}


class AlphaVantageProvider(MarketDataProvider):
    name = "alphavantage"
    coverage_note = (
        "Alpha Vantage 日线（adjusted close + 拆股/分红调整）。免费套餐额度极低，"
        "只适合小股票池；全市场扫描需要付费额度。"
    )

    def __init__(self, settings):
        super().__init__(settings)
        self.key = os.environ.get("ALPHAVANTAGE_API_KEY", "")
        if not self.key:
            raise DataProviderError("Alpha Vantage 需要环境变量 ALPHAVANTAGE_API_KEY")
        self.outputsize = settings.data_source.alphavantage_outputsize or "full"
        self._symbols: Optional[List[Symbol]] = None
        self._cache: Dict[str, List[Bar]] = {}

    # ------------------------------------------------------------------
    def _query(self, **params):
        params["apikey"] = self.key
        payload = http_json(
            BASE_URL,
            params=params,
            timeout=self.timeout,
            max_retries=self.max_retries,
            limiter=self.limiter,
        )
        if isinstance(payload, dict):
            for key in ("Note", "Information", "Error Message"):
                if key in payload:
                    raise DataProviderError(
                        f"Alpha Vantage 拒绝请求（{key}）：{str(payload[key])[:200]}"
                    )
        return payload

    def list_symbols(self) -> List[Symbol]:
        if self._symbols is not None:
            return list(self._symbols)
        text = http_text(
            BASE_URL,
            params={"function": "LISTING_STATUS", "state": "active", "apikey": self.key},
            timeout=self.timeout,
            max_retries=self.max_retries,
            limiter=self.limiter,
        )
        symbols: List[Symbol] = []
        for row in csv.DictReader(io.StringIO(text)):
            ticker = (row.get("symbol") or "").strip().upper()
            if not ticker or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
                continue
            exchange_raw = (row.get("exchange") or "").strip().upper()
            asset_type = (row.get("assetType") or "").strip()
            security_type = "etf" if asset_type.lower().startswith("etf") else "common_stock"
            symbols.append(
                Symbol(
                    ticker=ticker,
                    name=(row.get("name") or ticker).strip(),
                    exchange=_EXCHANGE_MAP.get(exchange_raw, exchange_raw or "UNKNOWN"),
                    security_type=security_type,
                    is_otc=exchange_raw in ("OTC", "PNK"),
                    market_cap=None,
                    sector="",
                    listing_status="delisted" if row.get("delistingDate") not in (None, "", "null") else "active",
                    source="alphavantage",
                )
            )
        if not symbols:
            raise DataProviderError("Alpha Vantage LISTING_STATUS 返回空列表")
        self._symbols = symbols
        return list(symbols)

    # ------------------------------------------------------------------
    def get_daily_bars(
        self, symbol: str, start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Bar]:
        symbol = symbol.upper()
        if symbol not in self._cache:
            self._cache[symbol] = self._fetch(symbol)
        bars = self._cache[symbol]
        if start:
            bars = [b for b in bars if b.day >= start]
        if end:
            bars = [b for b in bars if b.day <= end]
        return list(bars)

    def _fetch(self, symbol: str) -> List[Bar]:
        payload = self._query(
            function="TIME_SERIES_DAILY_ADJUSTED",
            symbol=symbol,
            outputsize=self.outputsize,
        )
        series = payload.get("Time Series (Daily)") if isinstance(payload, dict) else None
        if not series:
            raise DataProviderError(f"Alpha Vantage 未返回 {symbol} 的日线数据")

        bars: List[Bar] = []
        for day_text, row in series.items():
            try:
                day = date.fromisoformat(day_text)
            except ValueError:
                continue
            close = _num(row.get("5. adjusted close")) or _num(row.get("4. close"))
            if close is None or close <= 0:
                continue
            volume = _num(row.get("6. volume")) or 0.0
            # 成交量也需要拆股调整，否则历史成交额口径不一致
            split_coeff = _num(row.get("8. split coefficient")) or 1.0
            if split_coeff and split_coeff != 1.0:
                volume *= split_coeff
            bars.append(
                Bar(
                    symbol=symbol,
                    day=day,
                    open=_num(row.get("1. open")) or close,
                    high=_num(row.get("2. high")) or close,
                    low=_num(row.get("3. low")) or close,
                    close=close,
                    volume=volume,
                    source="alphavantage",
                    adjustment="all",
                )
            )
        bars.sort(key=lambda b: b.day)
        return bars

    def get_fundamentals(self, symbol: str):
        """OVERVIEW 接口提供市值（MarketCapitalization）、行业（Sector）等。

        注意：该接口会额外消耗一次请求额度，默认只在 ``quality_enabled``
        或需要市值补齐时调用（由调用方控制）。
        """
        payload = self._query(function="OVERVIEW", symbol=symbol.upper())
        if not isinstance(payload, dict) or not payload.get("Symbol"):
            return None
        return {
            "market_cap": _num(payload.get("MarketCapitalization")),
            "sector": payload.get("Sector") or "",
            "pe_ratio": _num(payload.get("PERatio")),
            "profit_margin": _num(payload.get("ProfitMargin")),
            "revenue_growth_yoy": _num(payload.get("QuarterlyRevenueGrowthYOY")),
            "eps_growth_yoy": _num(payload.get("QuarterlyEarningsGrowthYOY")),
            "dividend_yield": _num(payload.get("DividendYield")),
        }


def _num(raw) -> Optional[float]:
    if raw in (None, "", "None", "-", "nan"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
