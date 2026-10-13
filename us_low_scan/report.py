"""报告输出：Markdown（人读）、CSV（表格）、JSON（程序读）。

Markdown 的排版严格按产品构想第 6 节的「报告示例」来组织：

    > **美股低位观察名单｜交易日：YYYY-MM-DD**
    > 数据更新：YYYY-MM-DD HH:MM America/New_York
    > 市场环境：SPY 相对 200 日均线的位置；是否处于整体下行环境
    >
    > **A. 低位企稳组**
    > - `TICKER`｜总分 XX/100
    >   - 低位：距 52 周低点 X%，较 52 周高点回撤 Y%。
    >   - 流动性：20 日平均美元成交额 US$X。
    >   - 企稳证据：列明具体指标。
    >   - 风险：列明财报/事件/趋势风险；没有覆盖的项目标注"未核实"。
    >   - 退出观察条件：列出明确条件。
    >
    > **B. 低位但尚未企稳组**
    > - `TICKER`｜说明仍在持续下跌或缺少反转证据，单独观察，不视为主要候选。
    >
    > **今日结论：** ...

即：整份报告放在一个引用块里，一行一个字段，每只股票是"一行标题 + 若干缩进子项"。
字段内容严格对齐构想第 6 节那张表（股票 / 最新行情 / 流动性 / 低位情况 / 技术信号 /
市场对比 / 综合分数 / 入选理由 / 风险标签 / 失效条件 / 资料来源）。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .models import Candidate, ScanResult

SEVERITY_MARK = {"high": "🔴 高风险", "warn": "🟡 注意", "info": ""}
UNVERIFIED = "未核实"


# --------------------------------------------------------------------------
# 格式化小工具
# --------------------------------------------------------------------------
def money(value: Optional[float], unit: str = "US$") -> str:
    if value is None:
        return UNVERIFIED
    if abs(value) >= 1e9:
        return f"{unit}{value/1e9:.2f}B"
    if abs(value) >= 1e6:
        return f"{unit}{value/1e6:.1f}M"
    if abs(value) >= 1e3:
        return f"{unit}{value/1e3:.1f}K"
    return f"{unit}{value:,.2f}"


def pct(value: Optional[float], digits: int = 1, signed: bool = True) -> str:
    if value is None:
        return UNVERIFIED
    fmt = f"{{:+.{digits}f}}%" if signed else f"{{:.{digits}f}}%"
    return fmt.format(value)


def price(value: Optional[float]) -> str:
    return UNVERIFIED if value is None else f"US${value:,.2f}"


def _num(value, digits: int = 2) -> str:
    if value is None:
        return UNVERIFIED
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def score_line(candidate: Candidate) -> str:
    score = candidate.score
    if score.quality_available:
        return f"{score.total:.1f}/100"
    return f"{score.total:.1f}/{score.max_available:.0f}"


def _quote(text: str = "") -> str:
    """引用块里的一行。非空行结尾加两个空格 —— Markdown 的硬换行，
    这样每个字段都独占一行，手机/终端里读起来也不会被折成一段。"""
    if not text.strip():
        return ">"
    return "> " + text + "  "


def _joined(items: List[str], sep: str = "；") -> str:
    return sep.join(x for x in items if x)


def _short(text: str, limit: int = 46) -> str:
    """把一条说明压成短句：只取第一个分句，超长就截断。

    构想要求"列明风险"，但一行 300 字的段落没人看 —— 细节保留在 CSV / JSON / 网页里。
    """
    text = (text or "").split("；")[0].strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# 每只股票的字段组装
# --------------------------------------------------------------------------
def _market_time(scan: ScanResult, settings) -> str:
    """数据更新时间。构想要求用 America/New_York，同时附上使用者本地时间。"""
    try:
        from zoneinfo import ZoneInfo

        market = scan.generated_at.astimezone(ZoneInfo(settings.general.market_timezone))
        local = scan.generated_at.astimezone(ZoneInfo(settings.general.display_timezone))
        return (
            f"{market.strftime('%Y-%m-%d %H:%M')} {settings.general.market_timezone}"
            f"（本地 {local.strftime('%Y-%m-%d %H:%M')} {settings.general.display_timezone}）"
        )
    except Exception:  # pragma: no cover - 时区数据缺失时退回原值
        return scan.generated_at.isoformat(timespec="minutes")


def _stabilization_evidence(candidate: Candidate) -> str:
    """「企稳证据」按构想要求列出具体指标，而不是给一个笼统结论。"""
    f = candidate.features
    bits: List[str] = []
    if f.rsi14 is None:
        bits.append("RSI 未核实")
    elif f.rsi14_slope_5d is not None and f.rsi14_slope_5d > 0:
        bits.append(f"RSI(14) 从低位回升（{f.rsi14:.1f}，近 5 日 {f.rsi14_slope_5d:+.1f}）")
    else:
        bits.append(f"RSI(14) {f.rsi14:.1f} 仍未见回升")
    if f.higher_low_pct is not None:
        bits.append(
            f"短期低点抬高 {f.higher_low_pct:+.1f}%"
            if f.higher_low_pct > 0
            else f"短期低点尚未抬高（{f.higher_low_pct:+.1f}%）"
        )
    if f.new_low_streak > 0:
        bits.append(f"仍在创新低（连续 {f.new_low_streak} 个交易日刷新 20 日新低）")
    ma_bits = []
    if f.vs_ma20_pct is not None:
        ma_bits.append(("站上" if f.vs_ma20_pct > 0 else "未站上") + f" 20 日均线（{f.vs_ma20_pct:+.1f}%）")
    if f.vs_ma50_pct is not None:
        ma_bits.append(("站上" if f.vs_ma50_pct > 0 else "未站上") + f" 50 日均线（{f.vs_ma50_pct:+.1f}%）")
    bits.extend(ma_bits)
    if f.volume_up_down_ratio is not None:
        bits.append(
            f"上涨日成交额为下跌日的 {f.volume_up_down_ratio:.2f} 倍"
            + ("（有改善）" if f.volume_up_down_ratio >= 1.0 else "（未改善）")
        )
    return _joined(bits)


def _risk_line(candidate: Candidate) -> str:
    tags = sorted(
        candidate.risk_tags, key=lambda t: {"high": 0, "warn": 1, "info": 2}[t.severity]
    )
    bits = []
    for tag in tags:
        mark = SEVERITY_MARK.get(tag.severity, "")
        # 只有"🔴 高风险"才附说明；🟡 注意 只留标签，否则这一行会变成一堵墙
        detail = f"（{_short(tag.detail)}）" if tag.severity == "high" and tag.detail else ""
        label = f"{mark} · {tag.label}" if mark else tag.label
        bits.append(f"{label}{detail}")
    return _joined(bits) or "无特别风险标签"


def _relative_line(features) -> str:
    """相对强弱。基准行情缺失时返回空串，由调用方写成「未核实」——
    真实数据下 SPY 拉不到是可能发生的，不能让整份报告崩掉。"""
    def vs(value: Optional[float], label: str) -> str:
        if value is None:
            return ""
        return f"{label}{'跑赢' if value > 0 else '跑输'} SPY {abs(value):.1f} 个百分点"

    bits = [
        vs(features.rel_strength_spy_20d, "近 1 个月"),
        vs(features.rel_strength_spy_60d, "近 3 个月"),
        (
            f"相对行业基准 {features.rel_strength_sector_20d:+.1f} 个百分点"
            if features.rel_strength_sector_20d is not None else ""
        ),
    ]
    return _joined(bits)


def _adjustment_note(settings) -> str:
    provider = settings.data_source.provider
    if provider == "alpaca":
        return f"拆股调整（adjustment={settings.data_source.alpaca_adjustment}，feed={settings.data_source.alpaca_feed}）"
    if provider == "alphavantage":
        return "拆股 + 分红调整（adjusted close）"
    if provider == "csv":
        return "以你导出的口径为准（若含 adj_close 优先使用）"
    return "拆股调整"


def _source_line(candidate: Candidate, settings) -> str:
    bits = [f"行情：{', '.join(candidate.data_sources) or settings.data_source.provider}"]
    bits.append(
        "基本面：" + ("已启用" if settings.scoring.quality_enabled else f"{UNVERIFIED}（未启用）")
    )
    bits.append(
        "事件（财报/申报）："
        + ("已检查" if settings.data_source.sec_enabled else f"{UNVERIFIED}（未启用 SEC 检查）")
    )
    return _joined(bits)


def _candidate_block(candidate: Candidate, settings, index: int) -> List[str]:
    f = candidate.features
    lines = [
        _quote(
            f"- `{candidate.symbol}`｜{candidate.name}｜{candidate.exchange or UNVERIFIED}"
            f"｜总分 {score_line(candidate)}"
        ),
        _quote(
            f"  - 最新行情：收盘 {price(f.close)}（{f.session.isoformat()}）；"
            f"数据源 {'/'.join(candidate.data_sources) or settings.data_source.provider}；"
            f"调整口径：{_adjustment_note(settings)}"
        ),
        _quote(
            f"  - 低位：距 52 周低点 {pct(f.distance_from_low_pct)}，"
            f"较 52 周高点回撤 {pct(-abs(f.drawdown_from_high_pct)) if f.drawdown_from_high_pct is not None else UNVERIFIED}"
            f"（52 周区间 {price(f.low_52w)} – {price(f.high_52w)}）。"
        ),
        _quote(
            f"  - 流动性：20 日平均美元成交额 {money(f.avg_dollar_volume_20d)}，"
            f"当日 {money(f.dollar_volume)}"
            + (f"（为 20 日均值的 {f.dollar_volume_ratio_20d:.2f} 倍）" if f.dollar_volume_ratio_20d is not None else "")
            + "。"
        ),
        _quote(
            f"  - 技术信号：RSI(14) {f'{f.rsi14:.1f}' if f.rsi14 is not None else UNVERIFIED}；"
            f"相对 20 日均线 {pct(f.vs_ma20_pct)}；相对 50 日均线 {pct(f.vs_ma50_pct)}；"
            + ("近期仍在创新低" if f.new_low_streak > 0 else "近期未再创新低")
            + f"；近 1 个月 {pct(f.change_1m_pct)}、近 3 个月 {pct(f.change_3m_pct)}。"
        ),
        _quote("  - 市场对比：" + (_relative_line(f) or UNVERIFIED) + "。"),
        _quote(f"  - 企稳证据：{_stabilization_evidence(candidate)}。"),
        _quote(
            f"  - 综合分数：{score_line(candidate)}"
            f"（流动性 {candidate.score.liquidity:.1f}"
            f"/{candidate.score.weights.get('liquidity', 25):.0f}、"
            f"低位程度 {candidate.score.low_position:.1f}"
            f"/{candidate.score.weights.get('low_position', 30):.0f}、"
            f"企稳证据 {candidate.score.stabilization:.1f}"
            f"/{candidate.score.weights.get('stabilization', 25):.0f}、"
            + (
                f"公司质量 {candidate.score.quality:.1f}"
                f"/{candidate.score.weights.get('quality', 20):.0f}"
                if candidate.score.quality is not None
                else f"公司质量 {UNVERIFIED}（0/{candidate.score.weights.get('quality', 20):.0f}，不补分）"
            )
            + "）。**不代表上涨概率**。"
        ),
    ]
    if candidate.reasons:
        lines.append(_quote("  - 入选理由："))
        for reason in candidate.reasons:
            lines.append(_quote(f"    - {reason}"))
    lines += [
        _quote(f"  - 风险：{_risk_line(candidate)}。"),
    ]
    if candidate.invalidation:
        lines.append(_quote("  - 退出观察条件："))
        for condition in candidate.invalidation:
            lines.append(_quote(f"    - {condition}"))
    lines.append(_quote(f"  - 资料来源：{_source_line(candidate, settings)}。"))
    return lines


# --------------------------------------------------------------------------
# Markdown 主入口
# --------------------------------------------------------------------------
def render_markdown(scan: ScanResult, settings) -> str:
    lines: List[str] = []
    regime = scan.market_regime or {}

    if settings.data_source.provider == "sample":
        lines.append(
            "> ⚠️⚠️ **这是演示数据：股票代码与公司名由程序合成，现实中不存在。**"
            "仅用于验证工具本身。要看真实股票："
            "`python3 -m us_low_scan.cli --provider nasdaq --top 150 scan`"
        )
        lines.append(">")

    lines.append(_quote(f"**美股低位观察名单｜交易日：{scan.session.isoformat()}**"))
    lines.append(_quote(f"数据更新：{_market_time(scan, settings)}"))
    lines.append(
        _quote(
            f"市场环境：{settings.general.benchmark} 收盘 "
            f"{_num(regime.get('benchmark_close'))}，"
            + ("低于" if regime.get("below_ma") else "高于")
            + f" {regime.get('ma_window', 200)} 日均线 {_num(regime.get('ma'))}，"
            + f"**{regime.get('status', UNVERIFIED)}**"
            + (
                f"（近 1 个月 {pct(regime.get('trend_1m_pct'))}）"
                if regime.get("trend_1m_pct") is not None else ""
            )
        )
    )
    lines.append(_quote(f"股票池：扫描 {scan.universe_size} 只（已通过阶段 A 硬性过滤）"))
    if scan.skipped_reason:
        lines.append(_quote())
        lines.append(_quote(f"**今日结论：** 跳过运行 —— {scan.skipped_reason}"))
        return "\n".join(lines) + "\n"

    preference = getattr(scan, "preference", settings.filters.dip_preference)
    primary_is_dip = scan.primary_group == "watch"
    lines.append(
        _quote(
            "买入偏好："
            + ("买跌（dip_preference = dip）—— 主名单是位置够低、且还没涨起来的股票"
               if primary_is_dip
               else "企稳（dip_preference = stabilized）—— 主名单是已出现止跌信号的股票")
        )
    )

    primary = list(scan.primary)
    secondary = list(scan.secondary)
    primary_title = "A. 低位观察组（主要关注：还没涨起来）" if primary_is_dip \
        else "A. 低位企稳组（主要关注）"
    secondary_title = "B. 已出现企稳迹象（次级观察：价格已经在回升）" if primary_is_dip \
        else "B. 低位但尚未企稳组（次级观察）"

    lines.append(_quote())
    lines.append(_quote(f"**{primary_title}**"))
    if primary:
        for position, candidate in enumerate(primary, 1):
            lines += _candidate_block(candidate, settings, position)
    else:
        lines.append(_quote("暂无符合条件的主要候选。"))

    lines.append(_quote())
    lines.append(_quote(f"**{secondary_title}**"))
    if secondary:
        for candidate in secondary:
            f = candidate.features
            note = candidate.group_reason or "缺少足够的止跌证据"
            if primary_is_dip:
                note = (
                    f"已出现止跌信号，从「买跌」角度看晚了一步"
                    f"（相对 20 日均线 {pct(f.vs_ma20_pct)}，RSI {f'{f.rsi14:.1f}' if f.rsi14 is not None else UNVERIFIED}）"
                )
            lines.append(
                _quote(
                    f"- `{candidate.symbol}`｜{candidate.name}｜总分 {score_line(candidate)}"
                    f"｜距 52 周低点 {pct(f.distance_from_low_pct)}"
                    f"｜{note}，单独观察，不视为主要候选。"
                )
            )
    else:
        lines.append(_quote("暂无。"))

    lines.append(_quote())
    lines.append(_quote(f"**今日结论：** {_conclusion(scan, settings, primary_is_dip)}"))

    lines.append(_quote())
    lines += _data_notes(scan, settings)
    lines.append("")
    lines.append(
        "本报告是**观察名单**，不是投资建议，也不代表上涨概率。"
        "「低位」指相对自身历史价格区间的位置，「企稳证据」指止跌迹象，"
        "两者都不保证价格不会继续下跌。"
    )
    return "\n".join(lines)


def _conclusion(scan: ScanResult, settings, primary_is_dip: bool) -> str:
    primary = list(scan.primary)
    if not primary:
        return (
            "暂无符合条件的主要候选。宁可不给名单，也不为了凑数把不符合门槛的股票列入。"
        )
    text = (
        "主要候选 "
        + "、".join(f"`{c.symbol}`" for c in primary)
        + f"，共 {len(primary)} 只。"
    )
    if primary_is_dip:
        text += (
            "它们都还在下跌或刚刚止跌（见「低位」「技术信号」两项），属于「买跌」的对象；"
            "**没有止跌证据意味着完全可能继续跌**，请按每只股票的「退出观察条件」设好离场线。"
        )
    else:
        text += "它们同时满足流动性、低位程度与企稳证据要求，但仍需自行判断。"
    return text


def _data_notes(scan: ScanResult, settings) -> List[str]:
    """构想在阶段 A 里要求：无法核实的关键项必须显式标注"未覆盖"。"""
    lines = [_quote("**数据与覆盖说明**")]
    if settings.data_source.provider == "sample":
        lines.append(_quote("- ⚠️ 本报告使用离线合成数据生成，**不是真实行情**，仅用于验证工具本身。"))
    lines.append(_quote(f"- 行情数据源：`{settings.data_source.provider}`"))
    lines.append(
        _quote(
            "- 基本面："
            + ("已启用" if settings.scoring.quality_enabled
               else "未启用，公司质量维度不计入总分，标注为「未核实」")
        )
    )
    lines.append(
        _quote(
            "- 事件与财报："
            + ("已启用 SEC EDGAR 检查" if settings.data_source.sec_enabled
               else "未启用，融资/退市等重大披露与财报日期标注为「未核实」")
        )
    )
    if scan.flagged:
        lines.append(
            _quote(
                f"- 已排除（{len(scan.flagged)} 只）："
                + "、".join(f"`{c.symbol}`" for c in scan.flagged)
                + " —— 已反弹 / 持续创新低 / 异常波动 / 退市融资类披露"
            )
        )
    for note in scan.notes:
        if note.startswith("买入偏好") or note.startswith("市场环境"):
            continue
        # provider 的覆盖说明在 sample 情况下与上面那行 ⚠️ 重复，跳过
        if settings.data_source.provider == "sample" and note.startswith("离线合成数据"):
            continue
        lines.append(_quote(f"- {note}"))
    lines.append(_quote("- 完整淘汰明细见同目录 `low_scan_rejected.csv`，全量指标见 `low_scan.csv`。"))
    return lines


def render_brief(scan: ScanResult, settings, max_items: int = 8) -> str:
    """纯文本短摘要，用于终端输出、邮件正文、IM 通知。"""
    regime = scan.market_regime or {}
    lines = [f"美股低位观察名单 · {scan.session.isoformat()}"]
    lines.append(
        f"市场环境：{settings.general.benchmark} {_num(regime.get('benchmark_close'))} "
        f"vs {regime.get('ma_window', 200)} 日均线 {_num(regime.get('ma'))}"
        f"（{regime.get('status', '未核实')}）"
    )
    lines.append("")
    primary = list(scan.primary)
    primary_label = "低位观察（还没涨起来）" if scan.primary_group == "watch" else "低位企稳"
    if primary:
        lines.append(f"A 组 {primary_label}（{len(primary)} 只）")
        for candidate in primary[:max_items]:
            f = candidate.features
            rsi_text = f"RSI {f.rsi14:.0f}" if f.rsi14 is not None else "RSI 未核实"
            lines.append(
                f"  {candidate.symbol:<7} {score_line(candidate):>8}  "
                f"距低点 {pct(f.distance_from_low_pct):>7}  "
                f"近1月 {pct(f.change_1m_pct):>7}  "
                f"20日均额 {money(f.avg_dollar_volume_20d):>8}  {rsi_text}"
            )
    else:
        lines.append("A 组 今日无符合条件的主要候选")
    lines.append("")
    if scan.secondary:
        lines.append(
            f"B 组 次级（{len(scan.secondary)} 只）："
            + "、".join(c.symbol for c in scan.secondary[:max_items])
        )
    if scan.flagged:
        lines.append(
            f"已排除（{len(scan.flagged)} 只）："
            + "、".join(c.symbol for c in scan.flagged[:max_items])
        )
    if scan.skipped_reason:
        lines.append(f"跳过原因：{scan.skipped_reason}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CSV / JSON
# --------------------------------------------------------------------------
CANDIDATE_COLUMNS = [
    "session", "group", "symbol", "name", "exchange", "sector",
    "total_score", "max_score", "score_liquidity", "score_low", "score_stab", "score_quality",
    "close", "avg_dollar_volume_20d", "dollar_volume", "dollar_volume_ratio_20d", "volume_cv_20d",
    "low_52w", "high_52w", "distance_from_low_pct", "drawdown_from_high_pct",
    "change_1m_pct", "change_3m_pct", "change_6m_pct",
    "ma20", "ma50", "ma200", "vs_ma20_pct", "vs_ma50_pct", "vs_ma200_pct",
    "rsi14", "rsi14_slope_5d", "higher_low_pct", "new_low_streak",
    "volume_up_down_ratio", "volatility_20d", "volatility_60d",
    "rel_strength_spy_20d", "rel_strength_spy_60d", "rel_strength_sector_20d",
    "group_reason", "reasons", "risk_tags", "invalidation", "data_sources",
]


def candidate_rows(scan: ScanResult) -> List[Dict[str, object]]:
    rows = []
    for candidate in scan.candidates:
        f = candidate.features
        rows.append(
            {
                "session": scan.session.isoformat(),
                "group": "低位企稳组" if candidate.group == "stabilized" else "低位未企稳观察组",
                "symbol": candidate.symbol,
                "name": candidate.name,
                "exchange": candidate.exchange,
                "sector": candidate.sector,
                "total_score": candidate.score.total,
                "max_score": candidate.score.max_available,
                "score_liquidity": candidate.score.liquidity,
                "score_low": candidate.score.low_position,
                "score_stab": candidate.score.stabilization,
                "score_quality": candidate.score.quality,
                "close": f.close,
                "avg_dollar_volume_20d": f.avg_dollar_volume_20d,
                "dollar_volume": f.dollar_volume,
                "dollar_volume_ratio_20d": f.dollar_volume_ratio_20d,
                "volume_cv_20d": f.volume_cv_20d,
                "low_52w": f.low_52w,
                "high_52w": f.high_52w,
                "distance_from_low_pct": f.distance_from_low_pct,
                "drawdown_from_high_pct": f.drawdown_from_high_pct,
                "change_1m_pct": f.change_1m_pct,
                "change_3m_pct": f.change_3m_pct,
                "change_6m_pct": f.change_6m_pct,
                "ma20": f.ma20,
                "ma50": f.ma50,
                "ma200": f.ma200,
                "vs_ma20_pct": f.vs_ma20_pct,
                "vs_ma50_pct": f.vs_ma50_pct,
                "vs_ma200_pct": f.vs_ma200_pct,
                "rsi14": f.rsi14,
                "rsi14_slope_5d": f.rsi14_slope_5d,
                "higher_low_pct": f.higher_low_pct,
                "new_low_streak": f.new_low_streak,
                "volume_up_down_ratio": f.volume_up_down_ratio,
                "volatility_20d": f.volatility_20d,
                "volatility_60d": f.volatility_60d,
                "rel_strength_spy_20d": f.rel_strength_spy_20d,
                "rel_strength_spy_60d": f.rel_strength_spy_60d,
                "rel_strength_sector_20d": f.rel_strength_sector_20d,
                "group_reason": candidate.group_reason,
                "reasons": " | ".join(candidate.reasons),
                "risk_tags": " | ".join(f"{t.label}:{t.detail}" for t in candidate.risk_tags),
                "invalidation": " | ".join(candidate.invalidation),
                "data_sources": ",".join(candidate.data_sources),
            }
        )
    return rows


def write_outputs(scan: ScanResult, settings) -> Dict[str, Path]:
    """写 Markdown / CSV / JSON 三份日报，返回文件路径。"""
    out_dir = Path(settings.general.output_dir) / scan.session.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}

    if settings.output.write_markdown:
        path = out_dir / "low_scan.md"
        path.write_text(render_markdown(scan, settings), encoding="utf-8")
        written["markdown"] = path

    rows = candidate_rows(scan)
    if settings.output.write_csv:
        path = out_dir / "low_scan.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=CANDIDATE_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        written["csv"] = path

        rejected_path = out_dir / "low_scan_rejected.csv"
        with open(rejected_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["symbol", "stage", "reason"])
            writer.writeheader()
            for row in scan.rejected:
                writer.writerow(row)
        written["rejected_csv"] = rejected_path

    if settings.output.write_json:
        path = out_dir / "low_scan.json"
        path.write_text(
            json.dumps(scan.as_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        written["json"] = path

    if settings.output.write_markdown:
        latest = Path(settings.general.output_dir) / "latest.md"
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(render_markdown(scan, settings), encoding="utf-8")
        written["latest"] = latest

        brief = Path(settings.general.output_dir) / "latest_brief.txt"
        brief.write_text(render_brief(scan, settings), encoding="utf-8")
        written["brief"] = brief

    return written


# --------------------------------------------------------------------------
# 从 JSON 还原 ScanResult（用于只改报告格式、不重跑扫描）
# --------------------------------------------------------------------------
def scan_result_from_dict(data: Dict[str, object]) -> ScanResult:
    from datetime import date as _date
    from datetime import datetime as _datetime

    from .models import Features, RiskTag, ScoreBreakdown

    def build_candidate(raw: Dict[str, object]) -> Candidate:
        feature_data = dict(raw.get("features") or {})
        feature_data["session"] = _date.fromisoformat(str(feature_data["session"]))
        features = Features(**feature_data)
        score_data = dict(raw.get("score") or {})
        weights = score_data.pop("weights", None)
        score_data.pop("total", None)
        score_data.pop("max_available", None)
        score = ScoreBreakdown(**score_data)
        if weights:
            score.weights = weights
        return Candidate(
            symbol=str(raw["symbol"]),
            name=str(raw.get("name", "")),
            exchange=str(raw.get("exchange", "")),
            session=_date.fromisoformat(str(raw["session"])),
            group=str(raw["group"]),
            score=score,
            features=features,
            reasons=list(raw.get("reasons") or []),
            risk_tags=[
                RiskTag(
                    code=str(t.get("code", "")),
                    label=str(t.get("label", "")),
                    severity=str(t.get("severity", "info")),
                    detail=str(t.get("detail", "")),
                )
                for t in (raw.get("risk_tags") or [])
            ],
            invalidation=list(raw.get("invalidation") or []),
            sector=str(raw.get("sector", "")),
            data_sources=list(raw.get("data_sources") or []),
            group_reason=str(raw.get("group_reason", "")),
        )

    generated = data.get("generated_at")
    try:
        generated_at = _datetime.fromisoformat(str(generated))
    except (TypeError, ValueError):
        generated_at = _datetime.now()

    return ScanResult(
        session=_date.fromisoformat(str(data["session"])),
        generated_at=generated_at,
        market_regime=dict(data.get("market_regime") or {}),
        stabilized=[build_candidate(c) for c in (data.get("stabilized") or [])],
        watch=[build_candidate(c) for c in (data.get("watch") or [])],
        flagged=[build_candidate(c) for c in (data.get("flagged") or [])],
        rejected=list(data.get("rejected") or []),
        notes=list(data.get("notes") or []),
        universe_size=int(data.get("universe_size") or 0),
        skipped_reason=data.get("skipped_reason"),
        primary_group=str(data.get("primary_group") or "stabilized"),
        preference=str(data.get("preference") or "stabilized"),
    )


def render_saved_report(settings, session: str, output_dir: Optional[Path] = None) -> str:
    """读取已保存的 JSON 并重新渲染 Markdown（不需要重新扫描）。"""
    base = Path(output_dir or settings.general.output_dir) / session
    path = base / "low_scan.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到已保存的扫描结果：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    markdown = render_markdown(scan_result_from_dict(data), settings)
    (base / "low_scan.md").write_text(markdown, encoding="utf-8")
    return markdown
