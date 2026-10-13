"""阶段 A：股票池构建与硬性过滤。

原则（产品构想第 2、3 节）：
* 只纳入允许的交易所与证券类型；默认排除 OTC、ETF、优先股、权证等。
* 门槛全部来自配置，代码里不写死。
* 数据缺失时**明确标注"未覆盖"**，绝不默认为"安全"。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Symbol, UNVERIFIED


@dataclass
class Rejection:
    symbol: str
    stage: str
    reason: str

    def as_dict(self) -> Dict[str, str]:
        return {"symbol": self.symbol, "stage": self.stage, "reason": self.reason}


@dataclass
class UniverseResult:
    kept: List[Symbol] = field(default_factory=list)
    rejected: List[Rejection] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def load_watchlist(path: Optional[Path]) -> Optional[set]:
    """读取只扫描的代码清单（CSV/纯文本，每行一个 ticker，可带表头）。"""
    if not path:
        return None
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"watchlist_file 不存在：{path}")
    tickers = set()
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            if not row:
                continue
            value = (row[0] or "").strip().upper()
            if not value or value in ("TICKER", "SYMBOL"):
                continue
            tickers.add(value)
    return tickers


def load_market_caps(path: Optional[Path]) -> Dict[str, float]:
    """读取外部市值补充文件（列：ticker,market_cap）。"""
    if not path:
        return {}
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"market_cap_file 不存在：{path}")
    out: Dict[str, float] = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        keys = {k.strip().lower(): k for k in (reader.fieldnames or [])}
        ticker_key = keys.get("ticker") or keys.get("symbol")
        cap_key = keys.get("market_cap") or keys.get("marketcap") or keys.get("mktcap")
        if not ticker_key or not cap_key:
            raise ValueError(f"{path}: 需要 ticker 与 market_cap 两列")
        for row in reader:
            ticker = (row.get(ticker_key) or "").strip().upper()
            raw = (row.get(cap_key) or "").strip().replace(",", "")
            if not ticker or not raw:
                continue
            try:
                out[ticker] = float(raw)
            except ValueError:
                continue
    return out


def apply_market_caps(symbols: Iterable[Symbol], caps: Dict[str, float]) -> List[Symbol]:
    if not caps:
        return list(symbols)
    out = []
    for symbol in symbols:
        cap = caps.get(symbol.ticker)
        if cap is not None and symbol.market_cap is None:
            out.append(_replace_market_cap(symbol, cap))
        else:
            out.append(symbol)
    return out


def _replace_market_cap(symbol: Symbol, market_cap: float) -> Symbol:
    from dataclasses import replace

    return replace(symbol, market_cap=market_cap)


def filter_instruments(symbols: Sequence[Symbol], settings) -> UniverseResult:
    """静态过滤：交易所、证券类型、OTC、名称关键词、上市状态、市值。"""
    cfg = settings.universe
    allowed_exchanges = {e.upper() for e in cfg.exchanges} if cfg.exchanges else None
    allowed_types = set(cfg.security_types)
    if cfg.include_adr:
        allowed_types.add("adr")
    allowed_types.discard("adr") if not cfg.include_adr else None

    result = UniverseResult()
    missing_cap = []
    missing_exchange = 0

    for symbol in symbols:
        if symbol.listing_status and symbol.listing_status.lower() not in ("active", ""):
            result.rejected.append(Rejection(symbol.ticker, "universe", f"非正常上市状态：{symbol.listing_status}"))
            continue
        if cfg.exclude_otc and symbol.is_otc:
            result.rejected.append(Rejection(symbol.ticker, "universe", "OTC / 粉单市场"))
            continue
        if not symbol.exchange:
            # 数据源没有交易所字段（例如 Nasdaq 公开清单）：放行但记一笔，
            # 报告里会写清楚"交易所未覆盖"，而不是假装核对过了。
            missing_exchange += 1
        elif allowed_exchanges and symbol.exchange_key not in allowed_exchanges:
            result.rejected.append(
                Rejection(symbol.ticker, "universe", f"交易所不在允许范围：{symbol.exchange}")
            )
            continue
        if allowed_types and symbol.security_type not in allowed_types:
            result.rejected.append(
                Rejection(symbol.ticker, "universe", f"证券类型不在允许范围：{symbol.security_type}")
            )
            continue
        matched_keyword = None
        if symbol.name:
            lowered = symbol.name.lower()
            for keyword in cfg.exclude_name_keywords:
                if keyword and keyword.lower() in lowered:
                    matched_keyword = keyword
                    break
        if matched_keyword:
            result.rejected.append(
                Rejection(symbol.ticker, "universe", f"名称包含排除关键词：{matched_keyword}")
            )
            continue
        if symbol.market_cap is None:
            if cfg.missing_market_cap_policy == "exclude":
                result.rejected.append(
                    Rejection(symbol.ticker, "universe", "市值未覆盖（策略=exclude）")
                )
                continue
            missing_cap.append(symbol.ticker)
        elif symbol.market_cap < cfg.min_market_cap:
            result.rejected.append(
                Rejection(
                    symbol.ticker,
                    "universe",
                    f"市值 {symbol.market_cap:,.0f} 低于门槛 {cfg.min_market_cap:,.0f}",
                )
            )
            continue
        result.kept.append(symbol)

    if missing_cap:
        result.notes.append(
            f"{len(missing_cap)} 只股票缺少市值数据，已放行并打上「市值未覆盖」标签"
            f"（可在配置里改成 exclude）"
        )
    if missing_exchange:
        result.notes.append(
            f"{missing_exchange} 只股票缺少交易所信息（数据源未提供），"
            "已放行但标注为「交易所未覆盖」；OTC 风险由数据源自身的清单范围兜底"
        )
    return result


def filter_liquidity(
    features_list, settings, symbol_lookup: Dict[str, Symbol]
) -> Tuple[List, List[Rejection]]:
    """阶段 A 的动态部分：股价、20 日均额、数据完整性。"""
    cfg = settings.universe
    filters = settings.filters
    kept = []
    rejected: List[Rejection] = []

    for features in features_list:
        symbol = symbol_lookup.get(features.symbol)
        if features.data_stale_days is not None and features.data_stale_days > filters.max_stale_days:
            rejected.append(
                Rejection(features.symbol, "filters", f"行情陈旧 {features.data_stale_days} 天（> {filters.max_stale_days}）")
            )
            continue
        if features.close < cfg.min_price:
            rejected.append(
                Rejection(features.symbol, "filters", f"股价 {features.close:.2f} 低于 {cfg.min_price:.2f}")
            )
            continue
        if features.avg_dollar_volume_20d is None:
            rejected.append(Rejection(features.symbol, "filters", "20 日均额数据不足"))
            continue
        if features.avg_dollar_volume_20d < cfg.min_avg_dollar_volume_20d:
            rejected.append(
                Rejection(
                    features.symbol,
                    "filters",
                    f"20 日均额 {features.avg_dollar_volume_20d/1e6:.1f}M 低于门槛 {cfg.min_avg_dollar_volume_20d/1e6:.1f}M",
                )
            )
            continue
        if symbol is not None and symbol.market_cap is not None and symbol.market_cap < cfg.min_market_cap:
            rejected.append(Rejection(features.symbol, "filters", "市值低于门槛"))
            continue
        kept.append(features)
    return kept, rejected


def top_by_liquidity(features_list, limit: int) -> List:
    """按 20 日均额从大到小截断股票池（用于控制 API 额度）。"""
    if not limit or limit <= 0 or len(features_list) <= limit:
        return list(features_list)
    ranked = sorted(
        features_list,
        key=lambda f: f.avg_dollar_volume_20d or 0.0,
        reverse=True,
    )
    return ranked[:limit]
