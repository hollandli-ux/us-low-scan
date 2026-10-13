"""Nasdaq 官方公开接口数据源（免费、无需 API key）—— 接真实行情用这个。

为什么做它
----------
Alpaca / Alpha Vantage 都要注册拿 key，而且免费档限制很多；很多用户只是想先看看
"真实的股票长什么样"。Nasdaq 官网自己的两个公开接口正好够用：

* ``/api/screener/stocks?download=true``
  返回全部在交易所上市的股票（约 7000 只），带**名称、市值、行业**——
  市值这一项恰好是 Alpaca / Alpha Vantage 都不提供的。
  **不含 ETF**，所以不用自己再剔一遍。

* ``/api/quote/<TICKER>/historical?assetclass=stocks``
  返回该股票的日线 OHLCV（实测可以一次拿到 2 年 / 500 多根）。

注意事项（写在覆盖说明里，也会进报告）
--------------------------------------
* 这是**非官方的公开网页接口**，不在 Nasdaq 的正式 API 合约里，字段和限流随时可能变。
  请保持克制的请求频率（默认 3-5 次/秒），不要拿它做高频或商业分发。
* 接口不返回交易所（NYSE/Nasdaq 分不出来），所以阶段 A 的交易所白名单对它形同虚设；
  好消息是这份清单本身就是交易所上市股票，不含 OTC。
* 价格口径按 Nasdaq 的拆股调整，不含分红调整。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

from ..models import Bar, Symbol
from .base import DataProviderError, MarketDataProvider, http_json

SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
HISTORY_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"

# 有些接口会对默认 UA 返回 403/429，带一个正常的浏览器 UA 更稳
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

# 名字里带这些词的通常不是普通股（权证/单位/优先股/存托凭证/SPAC 壳）
# Nasdaq 的名字带一长串尾巴（"McDonald's Corporation Common Stock"），报告里太占地方
_NAME_TAIL = re.compile(
    r"^(?P<base>.*?)(?:\s+(?P<cls>Class\s+[A-Z]))?\s+"
    r"(?:Common\s+Stock|Common\s+Shares|Ordinary\s+Shares|Capital\s+Stock|"
    r"American\s+Depositary\s+Shares|Depositary\s+Shares)$",
    re.IGNORECASE,
)


def _clean_name(name: str) -> str:
    match = _NAME_TAIL.match(name.strip())
    if not match:
        return name.strip()
    base = match.group("base").strip().rstrip(",.")
    cls = match.group("cls")
    return f"{base} {cls}" if cls else base


_NON_COMMON_HINTS = (
    "warrant", "right", "unit", "preferred", "depositary", "deposit",
    "notes", "acquisition corp", "acquisition corporation", "acquisition ltd",
)


class NasdaqProvider(MarketDataProvider):
    name = "nasdaq"
    coverage_note = (
        "Nasdaq 公开网页接口（免费、无需 API key）：清单约 7000 只交易所上市普通股，"
        "含名称 / 市值 / 行业；日线为拆股调整后的 OHLCV，实测一次可回溯 2 年。"
        "基准与行业 ETF（SPY 等）走 same 接口的 etf 口径。"
        "接口不提供交易所字段（无法区分 NYSE / Nasdaq），且价格不含分红调整。"
        "这是非官方接口，字段与限流可能变化，请保持克制频率、勿用于商业分发。"
    )

    def __init__(self, settings):
        super().__init__(settings)
        self.headers = dict(DEFAULT_HEADERS)
        self._symbols: Optional[List[Symbol]] = None
        self._cache: Dict[str, List[Bar]] = {}
        self.lookback_days = settings.filters.lookback_days
        # 并发数：接口没有批量下载，靠并发把「200 只」从十几分钟压到两三分钟
        self.concurrent_workers = max(1, int(settings.data_source.requests_per_second) * 2)

    # ------------------------------------------------------------------
    def list_symbols(self) -> List[Symbol]:
        if self._symbols is not None:
            return list(self._symbols)

        payload = http_json(
            SCREENER_URL,
            headers=self.headers,
            params={"tableonly": "true", "limit": 25000, "offset": 0, "download": "true"},
            timeout=self.timeout,
            max_retries=self.max_retries,
            limiter=self.limiter,
        )
        rows = ((payload or {}).get("data") or {}).get("rows") or []
        if not rows:
            raise DataProviderError("Nasdaq 清单接口返回空数据（可能被限流，稍后重试）")

        symbols: List[Symbol] = []
        for row in rows:
            ticker = (row.get("symbol") or "").strip().upper()
            name = (row.get("name") or "").strip()
            if not ticker or not name:
                continue
            lowered = name.lower()
            if any(hint in lowered for hint in _NON_COMMON_HINTS):
                security_type = "non_common"
            else:
                security_type = "common_stock"
            symbols.append(
                Symbol(
                    ticker=ticker,
                    name=_clean_name(name),
                    exchange="",              # 接口不提供，留空由阶段 A 标注"未覆盖"
                    security_type=security_type,
                    is_otc=False,
                    market_cap=_to_float(row.get("marketCap")) or None,
                    sector=(row.get("sector") or "").strip(),
                    listing_status="active",
                    source="nasdaq",
                )
            )
        self._symbols = symbols
        return list(symbols)

    # ------------------------------------------------------------------
    def get_daily_bars(
        self, symbol: str, start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Bar]:
        symbol = symbol.upper()
        if symbol not in self._cache:
            self._cache[symbol] = self._fetch(symbol, end or date.today())
        bars = self._cache[symbol]
        if start:
            bars = [b for b in bars if b.day >= start]
        if end:
            bars = [b for b in bars if b.day <= end]
        return list(bars)

    def _fetch(self, symbol: str, end: date) -> List[Bar]:
        # 多要一点余量：低位判定需要 252 个交易日，MA200 需要 200 个
        start = end - timedelta(days=int(self.lookback_days * 1.9) + 40)
        rows: List[dict] = []
        # 这份清单里只有股票，没有 ETF；而基准（SPY）和行业 ETF 都要用，
        # 所以股票口径拉不到就换 ETF 口径再试一次。
        for asset_class in ("stocks", "etf"):
            payload = http_json(
                HISTORY_URL.format(symbol=symbol),
                headers=self.headers,
                params={
                    "assetclass": asset_class,
                    "fromdate": start.isoformat(),
                    "todate": end.isoformat(),
                    "limit": 9999,
                },
                timeout=self.timeout,
                max_retries=self.max_retries,
                limiter=self.limiter,
            )
            rows = (
                (payload or {}).get("data") or {}
            ).get("tradesTable", {}).get("rows") or []
            if rows:
                break
        bars: List[Bar] = []
        for row in rows:
            day = _parse_date(row.get("date"))
            close = _to_float(row.get("close"))
            if day is None or close is None or close <= 0:
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    day=day,
                    open=_to_float(row.get("open")) or close,
                    high=_to_float(row.get("high")) or close,
                    low=_to_float(row.get("low")) or close,
                    close=close,
                    volume=_to_float(row.get("volume")) or 0.0,
                    source="nasdaq",
                    adjustment="split",
                )
            )
        bars.sort(key=lambda b: b.day)
        return bars

    def get_bars_multi(
        self, symbols: Sequence[str], start: Optional[date] = None, end: Optional[date] = None
    ) -> Dict[str, List[Bar]]:
        """这个接口没有批量下载，只能逐只拉。

        单次请求延迟约 2-4 秒，纯串行的话 200 只要十几分钟，所以开几个线程并发，
        同时用 :class:`RateLimiter` 把整体速率压住（默认 3-5 次/秒，服务端友好）。
        失败的单只记为空、不中断整轮。
        """
        symbols = [s for s in symbols]
        out: Dict[str, List[Bar]] = {}
        if not symbols:
            return out

        cached = {s: self._cache[s] for s in symbols if s in self._cache}
        pending = [s for s in symbols if s not in self._cache]
        out.update(cached)
        if not pending:
            return out

        workers = max(1, min(self.concurrent_workers, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.get_daily_bars, s, start, end): s for s in pending}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    out[symbol] = future.result()
                except Exception:
                    out[symbol] = []
        return out


def _to_float(raw) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip().replace("$", "").replace(",", "").replace("%", "")
    if text in ("", "--", "N/A", "nan", "None", "UNCH"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(raw) -> Optional[date]:
    """Nasdaq 返回的是 MM/DD/YYYY。"""
    text = str(raw or "").strip()
    parts = text.split("/")
    if len(parts) != 3:
        return None
    try:
        month, day, year = (int(p) for p in parts)
        return date(year, month, day)
    except ValueError:
        return None
