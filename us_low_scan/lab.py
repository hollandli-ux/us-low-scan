"""参数实验台：在不重新抓行情的前提下，试算不同的门槛与权重。

为什么可行：``daily_features`` 表里已经存了每只股票当天的**全部特征**
（距 52 周低点、回撤、RSI、均线、相对强弱、成交额…）。改门槛只是对这些
特征重新做一遍筛选与打分，不需要再碰数据源。

因此在网页上拖几个滑块，就能立刻看到「放宽到 20% 会多出哪几只」
「把企稳权重提到 30 分后排序怎么变」，再决定要不要写进参数方案。

口径提醒：实验台不重算来自外部事件的风险（财报、SEC 申报），
这类标签统一显示为「未核实」；正式名单仍以 ``scan`` 的结果为准。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Dict, List, Optional, Sequence

from . import db, risk, scoring
from .config import Settings, apply_preset, rescale_sub_scores, validate_settings
from .models import Features, Candidate, Symbol


@dataclass
class LabParams:
    """实验台可调的参数子集（对应界面上最常动的几个旋钮）。"""

    near_low_pct: float
    min_drawdown_from_high_pct: float
    min_avg_dollar_volume_20d: float
    min_price: float
    min_stabilization_score: float
    weight_liquidity: float
    weight_low_position: float
    weight_stabilization: float
    weight_quality: float
    max_main_candidates: int
    max_watch_candidates: int

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


def params_from_settings(settings: Settings) -> LabParams:
    return LabParams(
        near_low_pct=settings.filters.near_low_pct,
        min_drawdown_from_high_pct=settings.filters.min_drawdown_from_high_pct,
        min_avg_dollar_volume_20d=settings.universe.min_avg_dollar_volume_20d,
        min_price=settings.universe.min_price,
        min_stabilization_score=settings.filters.min_stabilization_score,
        weight_liquidity=settings.scoring.weight_liquidity,
        weight_low_position=settings.scoring.weight_low_position,
        weight_stabilization=settings.scoring.weight_stabilization,
        weight_quality=settings.scoring.weight_quality,
        max_main_candidates=settings.output.max_main_candidates,
        max_watch_candidates=settings.output.max_watch_candidates,
    )


def coerce_params(base: LabParams, raw: Dict[str, object]) -> LabParams:
    """把网页表单/命令行传进来的字符串安全地转换成 LabParams。"""
    values = asdict(base)
    for key, value in (raw or {}).items():
        if key not in values or value is None or value == "":
            continue
        try:
            values[key] = int(float(value)) if isinstance(values[key], int) else float(value)
        except (TypeError, ValueError):
            continue
    return LabParams(**values)


def build_settings(settings: Settings, params: LabParams) -> Settings:
    """把实验台参数套到配置上，并自动保持分项权重之和等于维度权重。"""
    previous = settings.scoring
    scoring_cfg = replace(
        previous,
        weight_liquidity=params.weight_liquidity,
        weight_low_position=params.weight_low_position,
        weight_stabilization=params.weight_stabilization,
        weight_quality=params.weight_quality,
    )
    scoring_cfg = rescale_sub_scores(scoring_cfg, previous)
    updated = replace(
        settings,
        universe=replace(
            settings.universe,
            min_avg_dollar_volume_20d=params.min_avg_dollar_volume_20d,
            min_price=params.min_price,
        ),
        filters=replace(
            settings.filters,
            near_low_pct=params.near_low_pct,
            min_drawdown_from_high_pct=params.min_drawdown_from_high_pct,
            min_stabilization_score=params.min_stabilization_score,
        ),
        scoring=scoring_cfg,
        output=replace(
            settings.output,
            max_main_candidates=params.max_main_candidates,
            max_watch_candidates=params.max_watch_candidates,
        ),
    )
    validate_settings(updated)
    return updated


def load_features(conn: sqlite3.Connection, session: date) -> List[Features]:
    """读取某日已保存的全部股票特征。"""
    out: List[Features] = []
    for row in conn.execute(
        "SELECT payload FROM daily_features WHERE session = ?", (session.isoformat(),)
    ):
        try:
            data = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        try:
            data["session"] = date.fromisoformat(str(data["session"]))
            out.append(Features(**data))
        except (TypeError, ValueError):
            continue
    return out


def available_sessions(conn: sqlite3.Connection, limit: int = 60) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT session FROM daily_features ORDER BY session DESC LIMIT ?", (limit,)
    )
    return [r["session"] for r in rows]


def run_lab(
    conn: sqlite3.Connection,
    settings: Settings,
    session: date,
    params: Optional[LabParams] = None,
) -> Dict[str, object]:
    """在 ``session`` 的特征快照上重算名单，并给出与当日正式名单的差异。"""
    params = params or params_from_settings(settings)
    lab_settings = build_settings(settings, params)

    features_list = load_features(conn, session)
    if not features_list:
        return {
            "session": session.isoformat(),
            "error": "该交易日没有保存特征快照（daily_features 为空）。"
            "请先运行 scan，或确认 [output] save_all_features = true。",
        }

    symbols = {s.ticker: s for s in db.load_symbols(conn)}
    market_state = _market_state(conn, session, lab_settings)

    after_price, price_rejected = _apply_price_and_liquidity(features_list, lab_settings)
    kept, low_rejected = [], 0
    for features in after_price:
        ok, _ = scoring.passes_low_position_filter(features, lab_settings)
        if ok:
            kept.append(features)
        else:
            low_rejected += 1

    candidates: List[Candidate] = []
    for features in kept:
        symbol = symbols.get(features.symbol)
        score = scoring.score_all(features, lab_settings)
        tags = risk.build_risk_tags(
            features, symbol, lab_settings, market_state=market_state,
            events=None, quality_available=False,
        )
        group, reason = scoring.classify_group(features, score, tags, lab_settings)
        candidates.append(
            Candidate(
                symbol=features.symbol,
                name=symbol.name if symbol else features.symbol,
                exchange=symbol.exchange if symbol else "",
                session=session,
                group=group,
                score=score,
                features=features,
                reasons=scoring.build_reasons(
                    features, score, lab_settings, limit=lab_settings.output.top_reasons
                ),
                risk_tags=tags,
                invalidation=risk.build_invalidation_conditions(features, lab_settings),
                sector=symbol.sector if symbol else "",
                data_sources=["daily_features 快照"],
                group_reason=reason,
            )
        )

    def rank(group: str):
        return sorted(
            [c for c in candidates if c.group == group],
            key=lambda c: (c.score.total, c.score.low_position or 0), reverse=True,
        )

    stabilized = rank("stabilized")
    watch = rank("watch")
    flagged = rank("rebounded") + rank("flagged")

    primary = scoring.primary_group(lab_settings)
    main_cap = lab_settings.output.max_main_candidates
    second_cap = lab_settings.output.max_watch_candidates
    if primary == "watch":
        watch, stabilized = watch[:main_cap], stabilized[:second_cap]
    else:
        stabilized, watch = stabilized[:main_cap], watch[:second_cap]
    flagged = flagged[:second_cap]

    # 与当日正式名单对比时，必须是"同组比同组"（主要候选只跟主要候选比），
    # 否则观察名单里的股票会被误判成"消失"。
    from .history import normalize_group

    saved_rows = db.load_candidates(conn, session)
    saved_stabilized = {
        r["symbol"] for r in saved_rows
        if normalize_group(r["group_name"]) == "stabilized"
    }
    saved_watch = {
        r["symbol"] for r in saved_rows
        if normalize_group(r["group_name"]) == "watch"
    }
    # 主名单是哪一个由偏好决定，比对时必须同角色比同角色
    if primary == "watch":
        current, saved_current = {c.symbol for c in watch}, saved_watch
        current_watch, saved_watch = {c.symbol for c in stabilized}, saved_stabilized
    else:
        current, saved_current = {c.symbol for c in stabilized}, saved_stabilized
        current_watch = {c.symbol for c in watch}
    return {
        "session": session.isoformat(),
        "params": params.as_dict(),
        "universe": len(features_list),
        "after_price_liquidity": len(after_price),
        "after_low_position": len(kept),
        "rejected": {
            "价格/流动性": len(price_rejected),
            "不在低位区间": low_rejected,
        },
        "market_regime": market_state,
        "primary_group": primary,
        "stabilized": [c.as_dict() for c in stabilized],
        "watch": [c.as_dict() for c in watch],
        "flagged": [c.as_dict() for c in flagged],
        "primary": [c.as_dict() for c in (watch if primary == "watch" else stabilized)],
        "secondary": [c.as_dict() for c in (stabilized if primary == "watch" else watch)],
        "diff": {
            "added": sorted(current - saved_current),
            "removed": sorted(saved_current - current),
            "kept": sorted(current & saved_current),
            "watch_added": sorted(current_watch - saved_watch),
            "watch_removed": sorted(saved_watch - current_watch),
            "has_saved_list": bool(saved_rows),
        },
        "caveat": "实验台不会重算财报/SEC 等外部事件风险（统一显示为未核实），"
                  "正式名单请以 scan 的结果为准。",
    }


def _apply_price_and_liquidity(features_list: Sequence[Features], settings) -> tuple:
    from .universe import filter_liquidity

    symbol_lookup: Dict[str, Symbol] = {}
    return filter_liquidity(list(features_list), settings, symbol_lookup)


def _market_state(conn: sqlite3.Connection, session: date, settings) -> Dict[str, object]:
    benchmark = settings.general.benchmark
    bars = db.load_bars(conn, benchmark, end=session)
    return risk.market_regime(bars, session, settings.filters.market_regime_ma)


def run_lab_with_preset(
    conn: sqlite3.Connection, settings: Settings, session: date, preset: str
) -> Dict[str, object]:
    """先套用参数方案，再进实验台试算（网页上的「方案 + 微调」组合）。"""
    preset_settings = apply_preset(settings, preset)
    return run_lab(conn, preset_settings, session, params_from_settings(preset_settings))


def _date_from(text: str) -> date:
    """把 ``YYYY-MM-DD`` 转成 date（网页/静态导出共用的小工具）。"""
    return date.fromisoformat(str(text).strip())
