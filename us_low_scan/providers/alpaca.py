"""Alpaca Market Data 数据源。

依据产品构想第 5 节的提醒，这里把"覆盖范围"写得很明确：

* 资产列表接口 ``/v2/assets`` 提供 symbol / exchange / name / status / tradable，
  但**不含市值**，需要配合 ``universe.market_cap_file`` 补齐，否则报告会标注
  "市值未覆盖"。
* 日线接口 ``/v2/stocks/bars`` 的 ``feed`` 参数很关键：``iex`` 只有 IEX 一个
  交易所的成交，**不能代表全市场成交量**；只有付费的 ``sip``（consolidated）
  才接近全市场口径。默认 ``iex``，因此报告会注明该限制。
* ``adjustment=split``：拆分调整 —— 低位判定与技术指标必须使用此口径。

需要的环境变量
--------------
``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from ..models import Bar, Symbol
from .base import DataProviderError, MarketDataProvider, http_json

ASSETS_URL = "https://api.alpaca.markets/v2/assets"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
PAGE_LIMIT = 10000

# Alpaca 的 exchange 代码 -> 本工具的交易所键
_EXCHANGE_MAP = {
    "NYSE": "NYSE",
    "NASDAQ": "NASDAQ",
    "AMEX": "NYSE_AMERICAN",
    "ARCA": "ARCA",
    "BATS": "BATS",
    "OTC": "OTC",
    "NYSEARCA": "ARCA",
}


class AlpacaProvider(MarketDataProvider):
    name = "alpaca"

    def __init__(self, settings):
        super().__init__(settings)
        import os

        self.key = os.environ.get("ALPACA_API_KEY", "")
        self.secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not (self.key and self.secret):
            raise DataProviderError(
                "Alpaca 需要环境变量 ALPACA_API_KEY 与 ALPACA_SECRET_KEY"
            )
        cfg = settings.data_source
        self.feed = cfg.alpaca_feed or "iex"
        self.adjustment = cfg.alpaca_adjustment or "split"
        self.coverage_note = (
            f"Alpaca Market Data，feed={self.feed}，adjustment={self.adjustment}。"
            + (
                "feed=iex 只覆盖 IEX 交易所的成交，不能代表全美市场成交量，"
                "全市场口径需要 sip 订阅。"
                if self.feed != "sip"
                else "feed=sip 为 consolidated 全市场行情。"
            )
        )
        self._symbols: Optional[List[Symbol]] = None

    # ------------------------------------------------------------------
    @property
    def headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
        }

    def list_symbols(self) -> List[Symbol]:
        if self._symbols is not None:
            return list(self._symbols)
        payload = http_json(
            ASSETS_URL,
            headers=self.headers,
            params={"status": "active", "asset_class": "us_equity"},
            timeout=self.timeout,
            max_retries=self.max_retries,
            limiter=self.limiter,
        )
        if not isinstance(payload, list):
            raise DataProviderError(f"Alpaca assets 返回异常：{payload!r}")

        symbols = []
        for item in payload:
            if not item.get("tradable"):
                continue
            exchange = _EXCHANGE_MAP.get(
                (item.get("exchange") or "").upper(), (item.get("exchange") or "UNKNOWN").upper()
            )
            name = item.get("name") or item.get("symbol")
            symbols.append(
                Symbol(
                    ticker=item["symbol"].upper(),
                    name=name,
                    exchange=exchange,
                    security_type="common_stock",
                    is_otc=exchange == "OTC",
                    market_cap=None,          # Alpaca 不提供市值
                    sector="",
                    listing_status="active",
                    source="alpaca",
                )
            )
        self._symbols = symbols
        return list(symbols)

    # ------------------------------------------------------------------
    def get_bars_multi(
        self, symbols: Sequence[str], start: Optional[date] = None, end: Optional[date] = None
    ) -> Dict[str, List[Bar]]:
        out: Dict[str, List[Bar]] = {s.upper(): [] for s in symbols}
        pending = list(out.keys())
        if not pending:
            return out

        start_iso = (start or (date.today() - timedelta(days=420))).isoformat()
        end_iso = (end or date.today()).isoformat()
        chunk_size = 100  # 单次请求的 symbol 上限，保守取值

        for i in range(0, len(pending), chunk_size):
            chunk = pending[i:i + chunk_size]
            token = None
            while True:
                params = {
                    "symbols": ",".join(chunk),
                    "timeframe": "1Day",
                    "start": start_iso,
                    "end": end_iso,
                    "adjustment": self.adjustment,
                    "feed": self.feed,
                    "limit": PAGE_LIMIT,
                    "sort": "asc",
                }
                if token:
                    params["page_token"] = token
                payload = http_json(
                    BARS_URL,
                    headers=self.headers,
                    params=params,
                    timeout=self.timeout,
                    max_retries=self.max_retries,
                    limiter=self.limiter,
                )
                if not isinstance(payload, dict):
                    raise DataProviderError(f"Alpaca bars 返回异常：{payload!r}")
                for symbol, rows in (payload.get("bars") or {}).items():
                    key = symbol.upper()
                    out.setdefault(key, [])
                    out[key].extend(_to_bars(key, rows, self.adjustment))
                token = payload.get("next_page_token")
                if not token:
                    break
        return out

    def get_daily_bars(
        self, symbol: str, start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Bar]:
        return self.get_bars_multi([symbol], start=start, end=end).get(symbol.upper(), [])


def _to_bars(symbol: str, rows: Sequence[dict], adjustment: str) -> List[Bar]:
    bars = []
    for row in rows or []:
        timestamp = row.get("t")
        if not timestamp:
            continue
        day = _parse_timestamp(timestamp)
        bars.append(
            Bar(
                symbol=symbol,
                day=day,
                open=float(row.get("o", 0.0)),
                high=float(row.get("h", 0.0)),
                low=float(row.get("l", 0.0)),
                close=float(row.get("c", 0.0)),
                volume=float(row.get("v", 0.0)),
                source="alpaca",
                adjustment=adjustment,
            )
        )
    bars.sort(key=lambda b: b.day)
    return bars


def _parse_timestamp(value) -> date:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return date.fromisoformat(text[:10])


def _sleep(seconds: float) -> None:  # pragma: no cover - 保留给将来调用
    time.sleep(seconds)
