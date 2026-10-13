"""阶段 D：美股特有风险检查 + 市场环境 + 失效条件。

核心原则：**没有数据不等于没有风险**。任何无法核实的项目都显式标注
"未核实"，不会因为"查不到"就当成"没问题"。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

from . import indicators as ind
from .models import Bar, Features, RiskTag, Symbol, UNVERIFIED


# --------------------------------------------------------------------------
# 市场环境
# --------------------------------------------------------------------------
def market_regime(benchmark_bars: Sequence[Bar], session: date, ma_window: int = 200) -> Dict[str, object]:
    """基准指数（默认 SPY）的状态：是否低于长期均线。"""
    usable = [b for b in benchmark_bars if b.day <= session]
    if not usable:
        return {
            "benchmark": None,
            "status": UNVERIFIED,
            "below_ma": None,
            "detail": "缺少基准行情数据，无法判断整体市场环境",
        }
    closes = [b.close for b in usable]
    ma = ind.sma(closes, ma_window)
    trend_1m = ind.percent_change(closes, 21)
    state: Dict[str, object] = {
        "benchmark_close": round(closes[-1], 4),
        "ma": round(ma, 4) if ma else None,
        "ma_window": ma_window,
        "below_ma": (closes[-1] < ma) if ma else None,
        "trend_1m_pct": round(trend_1m * 100, 2) if trend_1m is not None else None,
        "as_of": usable[-1].day.isoformat(),
    }
    if ma is None:
        state["status"] = UNVERIFIED
        state["detail"] = f"基准历史不足 {ma_window} 个交易日"
        state["below_ma"] = None
    elif closes[-1] < ma:
        state["status"] = "整体下行环境"
        state["detail"] = (
            f"基准收盘 {closes[-1]:.2f} 低于 {ma_window} 日均线 {ma:.2f}，"
            "市场处于普遍下跌环境，低位候选股的失败概率相应上升"
        )
    else:
        state["status"] = "整体上行/中性环境"
        state["detail"] = f"基准收盘 {closes[-1]:.2f} 高于 {ma_window} 日均线 {ma:.2f}"
    return state


# --------------------------------------------------------------------------
# 事件摘要（SEC 申报 / 财报）
# --------------------------------------------------------------------------
def summarize_events(ticker: str, events_client, session: date, lookback_days: int = 90) -> Dict[str, object]:
    """汇总 SEC 申报与（推算的）下次财报日期。

    返回字典里每个字段都可能为 None，表示"未核实"。
    """
    summary: Dict[str, object] = {
        "earnings_next_date": None,
        "earnings_is_estimate": False,
        "recent_filings": [],
        "dilution_filings": [],
        "delisting_filings": [],
        "last_periodic_filing": None,
        "checked": False,
    }
    if events_client is None:
        return summary

    try:
        from .providers.sec_edgar import DELISTING_FORMS, DILUTION_FORMS, PERIODIC_FORMS

        since = session - timedelta(days=lookback_days)
        filings = events_client.recent_filings(ticker, since=since)
        summary["checked"] = True
        summary["recent_filings"] = filings
        summary["dilution_filings"] = [
            f for f in filings if str(f["event_type"]).replace("SEC:", "").upper() in DILUTION_FORMS
        ]
        summary["delisting_filings"] = [
            f for f in filings if str(f["event_type"]).replace("SEC:", "").upper() in DELISTING_FORMS
        ]
        periodic = [
            f for f in filings if str(f["event_type"]).replace("SEC:", "").upper() in PERIODIC_FORMS
        ]
        if periodic:
            summary["last_periodic_filing"] = periodic[0]["event_date"]

        estimate = events_client.estimate_next_earnings(ticker, today=session)
        if estimate is not None:
            summary["earnings_next_date"] = estimate.isoformat()
            summary["earnings_is_estimate"] = True
    except Exception as exc:  # 网络/解析失败不能中断整条流程，但要留下痕迹
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary


# --------------------------------------------------------------------------
# 风险标签
# --------------------------------------------------------------------------
def build_risk_tags(
    features: Features,
    symbol: Optional[Symbol],
    settings,
    market_state: Optional[Dict[str, object]] = None,
    events: Optional[Dict[str, object]] = None,
    quality_available: bool = False,
) -> List[RiskTag]:
    cfg = settings.filters
    tags: List[RiskTag] = []

    # 0) 已经涨起来了（买跌偏好下这是最需要提前说清楚的一条）
    from .scoring import rebound_reason

    rebound = rebound_reason(features, settings)
    if rebound:
        tags.append(
            RiskTag(
                "already_rebounded",
                "已反弹",
                "high" if cfg.rebound_action == "exclude" else "warn",
                f"{rebound}；已经涨起来，不符合买跌偏好",
            )
        )

    # 1) 持续创新低
    if features.new_low_streak >= 2:
        tags.append(
            RiskTag(
                "persistent_new_low",
                "持续创新低",
                "high" if cfg.demote_on_persistent_new_low else "warn",
                f"连续 {features.new_low_streak} 个交易日刷新 20 日新低",
            )
        )

    # 2) 异常波动
    volatile_reasons = []
    if features.volatility_ratio is not None and features.volatility_ratio > cfg.volatility_spike_ratio:
        volatile_reasons.append(
            f"5 日波动率是 60 日均值的 {features.volatility_ratio:.1f} 倍"
        )
    if (
        features.max_single_day_move_20d is not None
        and features.max_single_day_move_20d > cfg.single_day_move_pct
    ):
        volatile_reasons.append(f"20 日内最大单日波动 {features.max_single_day_move_20d:.1f}%")
    if volatile_reasons:
        tags.append(
            RiskTag(
                "extreme_volatility",
                "异常波动",
                "high" if cfg.demote_on_extreme_volatility else "warn",
                "；".join(volatile_reasons),
            )
        )

    # 3) 低于长期均线
    if features.vs_ma200_pct is not None and features.vs_ma200_pct < 0:
        tags.append(
            RiskTag("below_ma200", "低于 200 日均线", "info",
                    f"收盘价低于 200 日均线 {abs(features.vs_ma200_pct):.1f}%")
        )

    # 4) 财报风险（含"未核实"）
    events = events or {}
    earnings_date = events.get("earnings_next_date")
    if earnings_date:
        try:
            days = (date.fromisoformat(str(earnings_date)) - features.session).days
        except ValueError:
            days = None
        if days is not None and 0 <= days <= cfg.earnings_alert_days:
            tags.append(
                RiskTag(
                    "earnings_soon",
                    "财报临近",
                    "high",
                    f"预计 {earnings_date} 公布财报（推算值，距报告日 {days} 天），"
                    "事件前后波动通常显著放大",
                )
            )
        elif days is not None and days > 0:
            tags.append(
                RiskTag(
                    "earnings_scheduled",
                    "财报日期已（推算）",
                    "info",
                    f"推算下次财报约在 {earnings_date}（推算值，非官方日历）",
                )
            )
    else:
        tags.append(
            RiskTag("earnings_unverified", "财报日期未核实", "warn",
                    "数据源未覆盖财报日历，无法排除财报临近风险")
        )

    # 5) SEC 事件
    if events.get("dilution_filings"):
        forms = sorted({str(f["event_type"]).replace("SEC:", "") for f in events["dilution_filings"]})
        tags.append(
            RiskTag("dilution_risk", "重大融资稀释", "high",
                    f"近期出现发行类申报：{', '.join(forms)}")
        )
    if events.get("delisting_filings"):
        forms = sorted({str(f["event_type"]).replace("SEC:", "") for f in events["delisting_filings"]})
        tags.append(
            RiskTag("delisting_risk", "退市风险文件", "high",
                    f"近期出现退市相关申报：{', '.join(forms)}")
        )
    if not events.get("checked"):
        tags.append(
            RiskTag("events_unverified", "重大事件未核实", "warn",
                    "未启用 SEC 事件检查（data_source.sec_enabled=false），"
                    "无法核实融资、退市等重大披露")
        )

    # 6) 基本面覆盖
    if not quality_available:
        tags.append(
            RiskTag("quality_missing", "基本面数据不足", "warn",
                    "缺少营收/盈利/现金流数据，公司质量维度未计入总分，本结果置信度较低")
        )

    # 7) 市值未覆盖
    if symbol is not None and symbol.market_cap is None:
        tags.append(
            RiskTag("market_cap_missing", "市值未覆盖", "warn",
                    "数据源未提供市值，无法排除微型股风险")
        )

    # 8) 市场环境
    if market_state and market_state.get("below_ma"):
        tags.append(
            RiskTag("market_regime", "市场环境风险较高", "warn",
                    f"{market_state.get('detail', '基准处于长期均线下方')}")
        )

    # 9) 放大下跌
    if features.volume_up_down_ratio is not None and features.volume_up_down_ratio < 0.7:
        tags.append(
            RiskTag("distribution_volume", "下跌放量", "warn",
                    f"近 10 日上涨日成交额仅为下跌日的 {features.volume_up_down_ratio:.2f} 倍，"
                    "存在放量下跌特征")
        )

    return tags


# --------------------------------------------------------------------------
# 失效条件
# --------------------------------------------------------------------------
def build_invalidation_conditions(features: Features, settings, stop_pct: float = 8.0) -> List[str]:
    """明确写出"什么变化会让它退出观察名单"。"""
    cfg = settings.universe
    filters = settings.filters
    conditions: List[str] = []

    new_low_trigger = (features.low_52w or features.close) * 0.995
    conditions.append(
        f"收盘价跌破 {new_low_trigger:.2f}（52 周低点 {features.low_52w:.2f} 附近）并创出新低"
    )
    conditions.append(
        f"较报告日收盘价 {features.close:.2f} 继续下跌超过 {stop_pct:.0f}%"
        f"（约 {features.close * (1 - stop_pct / 100):.2f}）"
    )
    if features.ma20:
        conditions.append(
            f"放量跌破 20 日均线 {features.ma20:.2f}，且 3 个交易日内未能收回"
        )
    conditions.append(
        f"20 日平均美元成交额跌破门槛 US${cfg.min_avg_dollar_volume_20d/1e6:.1f}M"
        f"（当前 US${(features.avg_dollar_volume_20d or 0)/1e6:.1f}M）"
    )
    conditions.append("连续 3 个交易日创 20 日新低，或出现放量长阴")
    conditions.append(
        "出现重大负面披露（融资稀释、退市风险、业绩大幅下修）或评级被显著下调"
    )
    if features.rsi14 is not None and features.rsi14 < 50:
        conditions.append(
            f"RSI(14) 回落至 30 以下且收盘价同步创 10 日新低（当前 RSI {features.rsi14:.1f}）"
        )
    if filters.earnings_alert_days:
        conditions.append("财报公布后单日跌幅超过 10%，或指引显著低于预期")
    return conditions
