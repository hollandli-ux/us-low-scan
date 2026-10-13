"""阶段 B / C / D：低位程度、企稳证据、风险特征的计算。

所有输入都严格截断到 ``session`` 当天，**不使用任何未来数据**
（回测复用的也是这一份代码，因此不存在"实盘和回测两套逻辑"的问题）。
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Sequence

from . import indicators as ind
from .models import Bar, Features

# 交易日至自然月/年的近似换算（统一用交易日，避免自然日造成的口径混乱）
DAYS_1M = 21
DAYS_3M = 63
DAYS_6M = 126


def compute_features(
    symbol: str,
    bars: Sequence[Bar],
    session: date,
    lookback_days: int = 252,
    benchmark_closes: Optional[Dict[date, float]] = None,
    sector_closes: Optional[Dict[date, float]] = None,
) -> Optional[Features]:
    """计算单只股票在 ``session`` 当天的全部特征。

    数据不足（或当天没有 K 线）时返回 None —— 调用方应把它记为"数据缺失"，
    而不是当成"没有风险"。
    """
    usable = [b for b in bars if b.day <= session]
    if not usable:
        return None

    last = usable[-1]
    # 行情陈旧度：最新 K 线必须落在 session 当天；否则说明数据源没更新
    stale_days = (session - last.day).days if session > last.day else 0
    if len(usable) < max(lookback_days, 200):
        return None

    window = usable[-lookback_days:]
    closes = [b.close for b in window]
    highs = [b.high for b in window]
    lows = [b.low for b in window]
    dollar_volumes = [b.dollar_volume or (b.close * b.volume) for b in window]

    close = closes[-1]
    high_52w = max(highs)
    low_52w = min(lows)

    dv_days = min(20, len(dollar_volumes))
    avg_dv = ind.mean(dollar_volumes[-dv_days:]) if dv_days else None
    today_dv = dollar_volumes[-1]
    dv_ratio = None
    if avg_dv:
        dv_ratio = today_dv / avg_dv

    rsi = ind.rsi(closes, 14)
    rsi_slope = ind.rsi_slope(closes, 14, 5)

    vol20 = ind.realized_volatility(closes, 20)
    vol60 = ind.realized_volatility(closes, 60)
    vol_ratio = None
    if vol20 is not None and vol60:
        vol_ratio = vol20 / vol60

    ma20 = ind.sma(closes, 20)
    ma50 = ind.sma(closes, 50)
    ma200 = ind.sma(closes, 200)

    features = Features(
        symbol=symbol,
        session=session,
        close=close,
        dollar_volume=today_dv,
        avg_dollar_volume_20d=avg_dv,
        dollar_volume_ratio_20d=dv_ratio,
        volume_cv_20d=(
            ind.coefficient_of_variation(dollar_volumes[-dv_days:]) if dv_days > 1 else None
        ),
        high_52w=high_52w,
        low_52w=low_52w,
        distance_from_low_pct=_pct(ind.distance_above(close, low_52w)),
        drawdown_from_high_pct=_pct(ind.drawdown_from_high(close, high_52w)),
        change_1m_pct=_pct(ind.percent_change(closes, DAYS_1M)),
        change_3m_pct=_pct(ind.percent_change(closes, DAYS_3M)),
        change_6m_pct=_pct(ind.percent_change(closes, DAYS_6M)),
        ma20=ma20,
        ma50=ma50,
        ma200=ma200,
        vs_ma20_pct=_pct(ind.close_above_ma(closes, 20)),
        vs_ma50_pct=_pct(ind.close_above_ma(closes, 50)),
        vs_ma200_pct=_pct(ind.close_above_ma(closes, 200)),
        rsi14=rsi,
        rsi14_slope_5d=rsi_slope,
        higher_low_pct=_pct(ind.higher_low(lows, recent=5, prior=10)),
        new_low_streak=ind.new_low_streak(closes, 20),
        volume_up_down_ratio=ind.up_down_volume_ratio(closes, dollar_volumes, window=10),
        volatility_20d=vol20,
        volatility_60d=vol60,
        volatility_ratio=vol_ratio,
        max_single_day_move_20d=_pct(
            ind.max_abs_single_day_move(closes, 20), already_pct=False, scale=100.0
        ),
        bars_used=len(usable),
        data_stale_days=stale_days,
    )

    # 相对强弱：与 SPY、行业 ETF 比较同期收益
    if benchmark_closes:
        features.rel_strength_spy_20d = _relative_strength(
            window, benchmark_closes, DAYS_1M
        )
        features.rel_strength_spy_60d = _relative_strength(
            window, benchmark_closes, DAYS_3M
        )
    if sector_closes:
        features.rel_strength_sector_20d = _relative_strength(
            window, sector_closes, DAYS_1M
        )

    return features


def _pct(value: Optional[float], already_pct: bool = False, scale: float = 100.0) -> Optional[float]:
    if value is None:
        return None
    if already_pct:
        return value
    return value * scale


def _benchmark_return(
    benchmark_closes: Dict[date, float], day_now: date, day_past: date
) -> Optional[float]:
    now = _closest_on_or_before(benchmark_closes, day_now)
    past = _closest_on_or_before(benchmark_closes, day_past)
    if now is None or past is None or past <= 0:
        return None
    return now / past - 1.0


def _closest_on_or_before(benchmark_closes: Dict[date, float], day: date) -> Optional[float]:
    """取不晚于 ``day`` 的最近一个基准收盘价（防止把未来数据拉进来）。"""
    if day in benchmark_closes:
        return benchmark_closes[day]
    candidates = [d for d in benchmark_closes if d <= day]
    if not candidates:
        return None
    return benchmark_closes[max(candidates)]


def _relative_strength(
    window: Sequence[Bar], benchmark_closes: Dict[date, float], lookback: int
) -> Optional[float]:
    if len(window) < lookback + 1:
        return None
    stock_now = window[-1].close
    stock_past = window[-1 - lookback].close
    if stock_past <= 0:
        return None
    stock_return = stock_now / stock_past - 1.0
    bench_return = _benchmark_return(
        benchmark_closes, window[-1].day, window[-1 - lookback].day
    )
    if bench_return is None:
        return None
    return _pct(stock_return - bench_return)


def closes_by_day(bars: Sequence[Bar]) -> Dict[date, float]:
    return {b.day: b.close for b in bars}


def compute_many(
    bars_map: Dict[str, List[Bar]],
    symbols: Sequence[str],
    session: date,
    settings,
    benchmark_bars: Optional[Sequence[Bar]] = None,
    sector_closes_map: Optional[Dict[str, Dict[date, float]]] = None,
    symbol_sector: Optional[Dict[str, str]] = None,
) -> Dict[str, Features]:
    """批量计算特征。返回 {ticker: Features}，数据不足的股票不会出现在结果里。"""
    lookup = settings.filters.lookback_days
    bench_closes = closes_by_day(benchmark_bars) if benchmark_bars else None
    sector_closes_map = sector_closes_map or {}
    symbol_sector = symbol_sector or {}

    out: Dict[str, Features] = {}
    for symbol in symbols:
        bars = bars_map.get(symbol) or []
        sector_etf = symbol_sector.get(symbol)
        sector_closes = sector_closes_map.get(sector_etf) if sector_etf else None
        features = compute_features(
            symbol,
            bars,
            session,
            lookback_days=lookup,
            benchmark_closes=bench_closes,
            sector_closes=sector_closes,
        )
        if features is not None:
            out[symbol] = features
    return out
