"""100 分制评分：用于排序，**不等于上涨概率**。

设计原则（产品构想第 4 节）：
* 四个维度：流动性 25 / 低位程度 30 / 企稳证据 25 / 公司质量 20（权重可配）。
* 每个分项都有明确的刻度与饱和点，可以从报告里逐条追溯。
* 公司质量数据缺失时，**不补满分也不补平均分**，而是返回 None，
  总分改按可用满分（默认 80）展示，并标注"基本面覆盖不足、置信度较低"。
* 风险用独立标签表达，不用总分去抵消。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .models import Features, RiskTag, ScoreBreakdown


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# 阶段 B：是否处于相对低位
# --------------------------------------------------------------------------
def passes_low_position_filter(features: Features, settings) -> Tuple[bool, str]:
    cfg = settings.filters
    if features.distance_from_low_pct is None or features.drawdown_from_high_pct is None:
        return False, "缺少 52 周高低点数据"
    if features.distance_from_low_pct > cfg.near_low_pct:
        return False, (
            f"距 52 周低点 {features.distance_from_low_pct:.1f}% > {cfg.near_low_pct:.1f}%"
            "（不够低）"
        )
    if features.drawdown_from_high_pct < cfg.min_drawdown_from_high_pct:
        return False, (
            f"较 52 周高点仅回撤 {features.drawdown_from_high_pct:.1f}% < "
            f"{cfg.min_drawdown_from_high_pct:.1f}%（回调不够深）"
        )
    return True, ""


# --------------------------------------------------------------------------
# 分项评分
# --------------------------------------------------------------------------
def score_liquidity(features: Features, settings) -> float:
    cfg = settings.scoring
    ucfg = settings.universe
    threshold = ucfg.min_avg_dollar_volume_20d
    adv = features.avg_dollar_volume_20d

    volume_points = 0.0
    if adv and threshold > 0:
        import math

        ratio = adv / threshold
        saturation = max(cfg.liquidity_volume_saturation_x, 1.01)
        scale = math.log10(ratio) / math.log10(saturation)
        volume_points = cfg.liquidity_volume_share * _clamp(scale)

    stability_points = 0.0
    cv = features.volume_cv_20d
    if cv is not None:
        stability_points = cfg.liquidity_stability_share * _clamp(1.0 - (cv - 0.2) / 0.8)
    else:
        # 数据不足时不给分，也不给满分
        stability_points = 0.0

    return round(volume_points + stability_points, 2)


def score_low_position(features: Features, settings) -> float:
    cfg = settings.scoring
    fcfg = settings.filters

    near_low = 0.0
    if features.distance_from_low_pct is not None and fcfg.near_low_pct > 0:
        d = features.distance_from_low_pct / 100.0
        near_low = cfg.low_near_low_share * _clamp(1.0 - d / (fcfg.near_low_pct / 100.0))

    drawdown = 0.0
    if features.drawdown_from_high_pct is not None:
        min_dd = fcfg.min_drawdown_from_high_pct
        sat = max(cfg.low_drawdown_saturation_pct, min_dd + 1.0)
        drawdown = cfg.low_drawdown_share * _clamp(
            (features.drawdown_from_high_pct - min_dd) / (sat - min_dd)
        )

    decline = 0.0
    if features.change_6m_pct is not None:
        r6 = features.change_6m_pct / 100.0
        decline = cfg.low_decline_share * _clamp(-r6 / 0.25)
        if r6 < -0.70:
            # 半年腰斩以上属于自由落体，不能因为是"低位"就加分
            decline *= 0.5

    return round(near_low + drawdown + decline, 2)


def score_stabilization(features: Features, settings) -> float:
    cfg = settings.scoring

    # RSI：从低位回升，而不是持续走低
    rsi_points = 0.0
    if features.rsi14 is not None:
        base = _clamp((features.rsi14 - 25.0) / 25.0)      # 25 -> 0，50 -> 1
        if features.rsi14 > 68:
            base *= 0.5                                     # 已经涨起来，不再是"低位"
        slope = features.rsi14_slope_5d
        if slope is None:
            factor = 0.7
        elif slope >= 3:
            factor = 1.0
        elif slope > 0:
            factor = 0.85
        elif slope > -5:
            factor = 0.6
        else:
            factor = 0.3
        rsi_points = cfg.stab_rsi_share * base * factor

    # 低点抬高 / 不再连续创新低
    higher_low_points = 0.0
    if features.higher_low_pct is not None:
        higher_low_points = cfg.stab_higher_low_share * _clamp(
            (features.higher_low_pct + 2.0) / 6.0
        )
    if features.new_low_streak > 0:
        higher_low_points = 0.0

    # 站上 20 日 / 50 日均线
    ma_points = 0.0
    if features.vs_ma20_pct is not None:
        if features.vs_ma20_pct > 0:
            ma_points += cfg.stab_ma_share * 0.6
        elif features.vs_ma20_pct > -3.0:
            ma_points += cfg.stab_ma_share * 0.3
    if features.vs_ma50_pct is not None and features.vs_ma50_pct > 0:
        ma_points += cfg.stab_ma_share * 0.4

    # 上涨日成交额相对改善
    volume_points = 0.0
    if features.volume_up_down_ratio is not None:
        volume_points = cfg.stab_volume_share * _clamp(
            (features.volume_up_down_ratio - 0.8) / 0.7
        )

    return round(rsi_points + higher_low_points + ma_points + volume_points, 2)


def score_quality(fundamentals: Optional[Dict[str, object]], settings) -> Optional[float]:
    """公司质量维度（营收/盈利/现金流趋势、资产负债风险）。

    没有可靠数据时返回 None —— 调用方会按 80 分制展示并降低置信度。
    """
    cfg = settings.scoring
    if not cfg.quality_enabled or not fundamentals:
        return None
    max_points = cfg.weight_quality
    parts: List[float] = []

    def add(value, scale_bounds, weight_share):
        if value is None:
            return
        low, high = scale_bounds
        parts.append(weight_share * _clamp((float(value) - low) / (high - low)))

    add(fundamentals.get("revenue_growth_yoy"), (-0.2, 0.4), 0.30)
    add(fundamentals.get("eps_growth_yoy"), (-0.2, 0.6), 0.25)
    add(fundamentals.get("profit_margin"), (-0.1, 0.25), 0.25)
    # 负债/流动性：数据缺失时不给分
    add(fundamentals.get("current_ratio"), (0.8, 2.5), 0.20)

    if len(parts) < 2:
        # 覆盖太少，仍然视为"基本面数据不足"，避免用 1 个指标冒充全面评估
        return None
    return round(sum(parts) / (0.30 + 0.25 + 0.25 + 0.20) * max_points, 2)


def score_all(
    features: Features, settings, fundamentals: Optional[Dict[str, object]] = None
) -> ScoreBreakdown:
    cfg = settings.scoring
    quality = score_quality(fundamentals, settings)
    breakdown = ScoreBreakdown(
        liquidity=score_liquidity(features, settings),
        low_position=score_low_position(features, settings),
        stabilization=score_stabilization(features, settings),
        quality=quality,
        quality_available=quality is not None,
        weights={
            "liquidity": cfg.weight_liquidity,
            "low_position": cfg.weight_low_position,
            "stabilization": cfg.weight_stabilization,
            "quality": cfg.weight_quality,
        },
    )
    if quality is None:
        breakdown.notes.append(
            "公司质量维度（满分 "
            f"{cfg.weight_quality:.0f}）因基本面数据不足未计入，总分按 "
            f"{breakdown.max_available:.0f} 分制展示，置信度较低。"
        )
    return breakdown


# --------------------------------------------------------------------------
# 买跌 / 买涨：判断这只股票是不是"已经涨起来了"
# --------------------------------------------------------------------------
def rebound_reason(features: Features, settings) -> Optional[str]:
    """已反弹的判定。返回原因字符串表示"已经涨起来了"，返回 None 表示还在跌/刚止跌。

    这是"买跌不买涨"偏好的核心实现：止跌信号（站上均线、RSI 回升）本身就意味着
    价格已经在涨，所以要把已经涨了一段的对象挡在主名单之外，而不是当成优点。
    """
    cfg = settings.filters
    if cfg.rebound_action == "off":
        return None
    reasons: List[str] = []
    if features.change_1m_pct is not None and features.change_1m_pct > cfg.max_gain_1m_pct:
        reasons.append(f"近 1 个月已涨 {features.change_1m_pct:.1f}%")
    if features.change_3m_pct is not None and features.change_3m_pct > cfg.max_gain_3m_pct:
        reasons.append(f"近 3 个月已涨 {features.change_3m_pct:.1f}%")
    if features.rsi14 is not None and features.rsi14 > cfg.max_rsi_for_dip:
        reasons.append(f"RSI(14) 已回到 {features.rsi14:.1f}")
    if cfg.require_below_ma50 and features.vs_ma50_pct is not None and features.vs_ma50_pct > 0:
        reasons.append(f"已站上 50 日均线 {features.vs_ma50_pct:.1f}%")
    return "；".join(reasons) if reasons else None


def primary_group(settings) -> str:
    """当前买入偏好下，哪一组当主名单。

    ``dip``        -> ``watch``（低位且还没涨起来，符合"买跌不买涨"）
    ``stabilized`` -> ``stabilized``（已经出现止跌信号；这类股票其实已经在涨）
    ``both``       -> ``stabilized``（两组都列，主名单仍是企稳组）
    """
    preference = settings.filters.dip_preference
    if preference == "dip":
        return "watch"
    return "stabilized"


GROUP_TITLES = {
    "stabilized": "低位企稳组",
    "watch": "低位观察组",
    "rebounded": "已反弹",
    "flagged": "风险提示",
}


def classify_group(features: Features, score: ScoreBreakdown, risk_tags: List[RiskTag], settings) -> Tuple[str, str]:
    """把候选股分到五个桶之一。

    ``stabilized``  已出现止跌/企稳信号
    ``watch``       低位但还没企稳（还在跌）
    ``rebounded``   已经涨起来了（仅当 rebound_action = demote）
    ``flagged``     一票降级的严重风险（仅在"买跌"模式下单独列出）
    ``excluded``    直接不进名单

    哪一组是主名单由 ``filters.dip_preference`` 决定，见 :func:`primary_group`。
    这一点很关键：止跌信号本身就意味着价格已经在回升，所以"买跌"的人要的
    主名单是 ``watch``，不是 ``stabilized``。
    """
    cfg = settings.filters
    dip_mode = cfg.dip_preference == "dip"
    codes = {t.code for t in risk_tags}
    demoting = {t.code for t in risk_tags if t.severity == "high"}

    # 1) 已经涨起来的：绝不进 A/B 名单
    rebounded = rebound_reason(features, settings)
    if rebounded:
        if cfg.rebound_action == "exclude":
            return "excluded", f"已经涨起来（{rebounded}），不符合买跌偏好"
        return "rebounded", f"已经涨起来（{rebounded}），仅供参考"

    # 2) 严重风险一票降级
    severe = ""
    if "persistent_new_low" in demoting:
        severe = "仍在持续创新低，缺少止跌证据"
    elif "extreme_volatility" in demoting:
        severe = "近期波动异常，风险高于一般低位股"
    elif "delisting_risk" in demoting or "dilution_risk" in demoting:
        severe = "存在退市/融资稀释类重大披露"
    if severe:
        # 买跌模式下主名单就是 watch，再把风险股塞进 watch 等于没有降级，
        # 所以这种情况下单独放 flagged 桶，在报告里以"风险提示"呈现。
        return ("flagged" if dip_mode else "watch"), severe

    # 3) 企稳分决定归到哪一组
    stabilization = score.stabilization or 0.0
    if stabilization >= cfg.min_stabilization_score:
        return "stabilized", ""
    if "earnings_soon" in codes:
        return "watch", "财报临近，等事件落地后再评估企稳质量"
    return "watch", (
        f"企稳证据不足（{stabilization:.1f} / {cfg.min_stabilization_score:.1f} 分）"
    )


# --------------------------------------------------------------------------
# 入选理由
# --------------------------------------------------------------------------
def build_reasons(features: Features, score: ScoreBreakdown, settings, limit: int = 4) -> List[str]:
    """用真实指标写出可追溯的入选理由（不是套话）。"""
    reasons: List[str] = []
    cfg = settings.universe

    if features.avg_dollar_volume_20d:
        reasons.append(
            f"流动性充足：20 日平均美元成交额 US${features.avg_dollar_volume_20d/1e6:.1f}M，"
            f"是门槛（US${cfg.min_avg_dollar_volume_20d/1e6:.1f}M）的 "
            f"{features.avg_dollar_volume_20d/cfg.min_avg_dollar_volume_20d:.1f} 倍"
        )

    if features.drawdown_from_high_pct is not None and features.distance_from_low_pct is not None:
        reasons.append(
            f"位置偏低：距 52 周低点仅 {features.distance_from_low_pct:.1f}%，"
            f"较 52 周高点回撤 {features.drawdown_from_high_pct:.1f}%"
        )

    if features.rsi14 is not None:
        slope = features.rsi14_slope_5d
        slope_text = ""
        if slope is not None:
            slope_text = f"，近 5 日变化 {slope:+.1f}"
        reasons.append(f"动能：RSI(14) = {features.rsi14:.1f}{slope_text}")

    if features.higher_low_pct is not None:
        if features.higher_low_pct > 0:
            reasons.append(
                f"低点抬高：近 5 日最低价高于此前 10 日最低价 {features.higher_low_pct:.1f}%"
            )
        else:
            reasons.append(
                f"低点尚未抬高：近 5 日最低价仍低于此前区间 {abs(features.higher_low_pct):.1f}%"
            )

    ma_bits = []
    if features.vs_ma20_pct is not None:
        ma_bits.append(
            ("站上" if features.vs_ma20_pct > 0 else "低于") + f" 20 日均线 {abs(features.vs_ma20_pct):.1f}%"
        )
    if features.vs_ma50_pct is not None:
        ma_bits.append(
            ("站上" if features.vs_ma50_pct > 0 else "低于") + f" 50 日均线 {abs(features.vs_ma50_pct):.1f}%"
        )
    if ma_bits:
        reasons.append("均线关系：" + "，".join(ma_bits))

    if features.rel_strength_spy_20d is not None:
        relation = "跑赢" if features.rel_strength_spy_20d > 0 else "跑输"
        reasons.append(
            f"相对市场：近 1 个月{relation} SPY {abs(features.rel_strength_spy_20d):.1f} 个百分点"
        )

    if features.new_low_streak == 0:
        reasons.append("最近 20 个交易日内未再刷新新低")

    # 买跌偏好最关心的一件事：它现在到底还在跌，还是已经涨起来了
    if features.change_1m_pct is not None:
        trend = "仍在下跌" if features.change_1m_pct < 0 else "已经走平或回升"
        ma_text = ""
        if features.vs_ma50_pct is not None:
            ma_text = (
                f"，且位于 50 日均线{'上方' if features.vs_ma50_pct > 0 else '下方'} "
                f"{abs(features.vs_ma50_pct):.1f}%"
            )
        reasons.append(
            f"跌势状态：近 1 个月 {features.change_1m_pct:+.1f}%（{trend}）{ma_text}"
        )

    return reasons[:limit]
