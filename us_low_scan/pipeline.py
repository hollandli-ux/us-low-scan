"""每日流程编排：抓数据 -> 硬性过滤 -> 特征 -> 评分 -> 分组 -> 输出。

整条链路只使用"截止到 ``session`` 当天"的数据，因此同一份代码既能跑
当日扫描，也能跑历史回放（backtest），不会出现两套逻辑。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import calendar_us, db, features as feature_module, risk, scoring
from .models import Bar, Candidate, ScanResult, Symbol
from .providers import get_provider
from .universe import (
    Rejection,
    apply_market_caps,
    filter_instruments,
    filter_liquidity,
    load_market_caps,
    load_watchlist,
    top_by_liquidity,
)

SCORE_TIEBREAK = ("stabilization", "liquidity", "low_position")


def run_scan(
    settings,
    session: Optional[date] = None,
    conn: Optional[sqlite3.Connection] = None,
    provider=None,
    events_client=None,
    persist: bool = True,
) -> ScanResult:
    """执行一次完整扫描。``session`` 为空时自动取最近一个已收盘交易日。"""
    session = session or calendar_us.last_completed_session()
    provider = provider or get_provider(settings.data_source.provider, settings)
    now = datetime.now(calendar_us.MARKET_TZ)

    result = ScanResult(session=session, generated_at=now)
    notes: List[str] = []

    if not calendar_us.is_trading_day(session):
        result.skipped_reason = (
            f"{session.isoformat()} 不是美股交易日（假日或周末），跳过本次运行"
        )
        _log(conn, result, settings, persist)
        return result

    # ---------- 1. 股票池 ----------
    selection = pick_universe(settings, provider)
    rejected: List[Dict[str, object]] = [r.as_dict() for r in selection.rejected]
    notes.extend(selection.notes)
    kept = selection.kept
    symbol_lookup = {s.ticker: s for s in kept}

    # ---------- 2. 行情 ----------
    tickers = [s.ticker for s in kept]
    benchmark = settings.general.benchmark
    sector_etfs = sorted({s.sector_etf for s in kept if s.sector_etf})
    fetch_list = sorted(set(tickers) | {benchmark} | set(sector_etfs))
    bars_map = _fetch_bars(settings, conn, provider, fetch_list, session, notes)

    if persist and conn is not None:
        db.upsert_symbols(conn, kept)
        all_bars = [b for bars in bars_map.values() for b in bars]
        db.upsert_bars(conn, all_bars)

    benchmark_bars = bars_map.get(benchmark, [])
    if not benchmark_bars:
        result.notes.append(f"⚠️ 未取到基准 {benchmark} 的行情，相对强弱与市场环境判断将缺失")
    latest = max((b.day for b in benchmark_bars), default=None) if benchmark_bars else None
    if latest is not None and latest < session:
        result.skipped_reason = (
            f"行情数据尚未更新到 {session.isoformat()}（最新只有 {latest.isoformat()}），"
            "跳过本次运行，避免用陈旧数据出名单"
        )
        _log(conn, result, settings, persist)
        return result

    # ---------- 3. 特征 ----------
    sector_closes = {
        etf: feature_module.closes_by_day(bars_map.get(etf, [])) for etf in sector_etfs
    }
    sector_lookup = {s.ticker: s.sector_etf for s in kept if s.sector_etf}
    computed = feature_module.compute_many(
        bars_map,
        tickers,
        session,
        settings,
        benchmark_bars=benchmark_bars,
        sector_closes_map=sector_closes,
        symbol_sector=sector_lookup,
    )
    missing_features = [t for t in tickers if t not in computed]
    for ticker in missing_features:
        rejected.append(Rejection(ticker, "data", "历史数据不足（少于 200/252 个交易日）").as_dict())

    # ---------- 4. 阶段 A 动态过滤 ----------
    liquid, liquidity_rejected = filter_liquidity(
        list(computed.values()), settings, symbol_lookup
    )
    rejected.extend(r.as_dict() for r in liquidity_rejected)
    liquid = top_by_liquidity(liquid, settings.universe.max_symbols)
    result.universe_size = len(liquid)

    # ---------- 5. 阶段 B：低位 ----------
    low_position: List = []
    for features in liquid:
        ok, why = scoring.passes_low_position_filter(features, settings)
        if ok:
            low_position.append(features)
        else:
            rejected.append(Rejection(features.symbol, "low_position", why).as_dict())
    if persist and conn is not None:
        if settings.output.save_all_features:
            # 保存全部通过阶段 A 的股票特征：网页"参数实验台"要用它来重算名单
            db.save_features(conn, list(computed.values()))
        elif low_position:
            db.save_features(conn, low_position)

    # ---------- 6. 市场环境 ----------
    regime = risk.market_regime(benchmark_bars, session, settings.filters.market_regime_ma)
    regime["benchmark"] = benchmark
    result.market_regime = regime
    notes.append(
        f"市场环境：{regime.get('status', '未核实')} —— {regime.get('detail', '')}"
    )

    # ---------- 7. 阶段 C/D：评分与风险 ----------
    candidates: List[Candidate] = []
    for features in low_position:
        symbol = symbol_lookup.get(features.symbol)
        events = None
        if events_client is not None:
            events = risk.summarize_events(features.symbol, events_client, session)
        fundamentals = None
        if settings.scoring.quality_enabled:
            try:
                fundamentals = provider.get_fundamentals(features.symbol)
            except Exception as exc:  # 基本面拉取失败不能拖垮整轮
                notes.append(f"{features.symbol} 基本面拉取失败：{exc}")
        score = scoring.score_all(features, settings, fundamentals)
        tags = risk.build_risk_tags(
            features,
            symbol,
            settings,
            market_state=regime,
            events=events,
            quality_available=score.quality_available,
        )
        group, reason = scoring.classify_group(features, score, tags, settings)
        candidate = Candidate(
            symbol=features.symbol,
            name=symbol.name if symbol else features.symbol,
            exchange=symbol.exchange if symbol else "",
            session=session,
            group=group,
            score=score,
            features=features,
            reasons=scoring.build_reasons(features, score, settings, limit=settings.output.top_reasons),
            risk_tags=tags,
            invalidation=risk.build_invalidation_conditions(features, settings),
            sector=symbol.sector if symbol else "",
            data_sources=_data_sources(provider, symbol),
            group_reason=reason,
        )
        candidates.append(candidate)

    # ---------- 8. 排序与截断 ----------
    excluded = [c for c in candidates if c.group == "excluded"]
    for candidate in excluded:
        rejected.append(Rejection(candidate.symbol, "rebound", candidate.group_reason).as_dict())

    ranked = sorted(candidates, key=lambda c: _rank_key(c, settings), reverse=True)
    stabilized_all = [c for c in ranked if c.group == "stabilized"]
    watch_all = [c for c in ranked if c.group == "watch"]
    flagged_all = [c for c in ranked if c.group in ("rebounded", "flagged")]

    # 数量上限按"角色"分配：主名单用 max_main_candidates，次级名单用 max_watch_candidates
    primary = scoring.primary_group(settings)
    main_cap = settings.output.max_main_candidates
    second_cap = settings.output.max_watch_candidates
    if primary == "watch":
        watch_all, stabilized_all = watch_all[:main_cap], stabilized_all[:second_cap]
    else:
        stabilized_all, watch_all = stabilized_all[:main_cap], watch_all[:second_cap]

    result.stabilized = stabilized_all
    result.watch = watch_all
    result.flagged = flagged_all[: max(0, settings.output.max_watch_candidates)]
    result.primary_group = primary
    result.preference = settings.filters.dip_preference

    kept = {id(c) for c in result.candidates} | {id(c) for c in result.flagged}
    for candidate in ranked:
        if id(candidate) in kept:
            continue
        rejected.append(
            Rejection(candidate.symbol, "output", "超出名单数量上限（按总分排序落选）").as_dict()
        )

    result.rejected = rejected
    notes.append(
        f"扫描统计：候选池 {selection.total} -> 硬性过滤 {selection.after_static} -> 流动性/价格 "
        f"{len(liquid)} -> 低位区间 {len(low_position)} -> 主名单 {len(result.primary)}、"
        f"次级 {len(result.secondary)}、一票否决 {len(result.flagged)}"
    )
    preference = settings.filters.dip_preference
    role_text = "低位观察组（还没涨起来）" if primary == "watch" else "低位企稳组（已出现止跌信号）"
    notes.append(
        f"买入偏好 dip_preference = {preference}：主名单是**{role_text}**；"
        + (
            "已反弹的股票（近 1 月 / 近 3 月涨幅超限或 RSI 偏高）已降到次级名单。"
            if preference == "dip"
            else "企稳信号本身意味着价格已经在回升，若你想避开已上涨的股票，"
                 "请把 dip_preference 改成 dip。"
        )
    )
    notes.append(provider.coverage_note)
    if provider.coverage_note and settings.data_source.provider in ("alpaca", "alphavantage"):
        notes.append(
            "⚠️ 请自行核对数据源的实际覆盖范围（是否 consolidated、是否含退市股票、"
            "历史年限、请求额度），覆盖不足会直接影响低位判定与流动性判断。"
        )
    result.notes = notes

    # ---------- 9. 落库 ----------
    if persist and conn is not None:
        db.save_candidates(conn, session, result.candidates)
        if events_client is not None:
            _persist_events(conn, candidates, events_client, session)
    _log(conn, result, settings, persist)
    return result


def _role(candidate: Candidate, primary: str) -> str:
    return "primary" if candidate.group == primary else "secondary"


@dataclass
class UniverseSelection:
    kept: List[Symbol]
    rejected: List[Rejection]
    notes: List[str]
    total: int = 0
    after_static: int = 0


def pick_universe(settings, provider) -> UniverseSelection:
    """选出这次真正要扫描的股票池。

    ``run_scan`` 与 ``catch_up`` 共用这一份逻辑 —— 否则补跑时会把整个市场都拉一遍
    （7000 只 × 每只一次请求，能跑上几个小时）。
    """
    symbols = provider.list_symbols()
    notes: List[str] = []

    caps = load_market_caps(settings.universe.market_cap_file)
    if caps:
        symbols = apply_market_caps(symbols, caps)
        notes.append(f"已从 {settings.universe.market_cap_file} 补充 {len(caps)} 条市值数据")

    watchlist = load_watchlist(settings.universe.watchlist_file)
    if watchlist:
        before = len(symbols)
        symbols = [s for s in symbols if s.ticker in watchlist]
        notes.append(f"按 watchlist_file 限制股票池：{before} -> {len(symbols)} 只")

    stage_a = filter_instruments(symbols, settings)
    notes.extend(stage_a.notes)

    kept = stage_a.kept
    prefilter = settings.universe.prefilter_top_market_cap
    if prefilter and len(kept) > prefilter:
        kept = sorted(kept, key=lambda item: item.market_cap or 0.0, reverse=True)[:prefilter]
        notes.append(
            f"抓行情前按市值取前 {prefilter} 只（共 {len(stage_a.kept)} 只可选），"
            "以控制请求量；把 [universe] prefilter_top_market_cap 改成 0 可取消"
        )
    return UniverseSelection(
        kept=kept,
        rejected=list(stage_a.rejected),
        notes=notes,
        total=len(symbols),
        after_static=len(stage_a.kept),
    )


def _fetch_bars(
    settings,
    conn: Optional[sqlite3.Connection],
    provider,
    fetch_list: Sequence[str],
    session: date,
    notes: List[str],
) -> Dict[str, List[Bar]]:
    """取行情，并优先复用数据库里已有的当天数据。

    没有缓存时，用真实数据源跑一次 200 只股票要一两分钟；有了缓存，
    同一天重复跑（改门槛、重渲染报告）是秒级的。
    """
    if not fetch_list:
        return {}
    cached: Dict[str, List[Bar]] = {}
    if conn is not None:
        for row in conn.execute(
            "SELECT symbol, MAX(day) AS latest FROM daily_bars GROUP BY symbol"
        ):
            if row["latest"] == session.isoformat():
                cached[row["symbol"]] = db.load_bars(conn, row["symbol"], end=session)
    to_fetch = [t for t in fetch_list if t not in cached]
    if cached:
        notes.append(
            f"{len(cached)} 只直接用了数据库里 {session.isoformat()} 的行情（跳过重复请求）"
        )
    fetched = provider.get_bars_multi(to_fetch) if to_fetch else {}
    merged = dict(cached)
    merged.update(fetched)
    return merged


def _rank_key(candidate: Candidate, settings) -> Tuple[float, float, float, float]:
    """排序键。买跌偏好下先看"够不够低"，否则先看"跌势是否止住"。"""
    score = candidate.score
    if settings.filters.dip_preference == "dip":
        return (
            score.total,
            score.low_position or 0.0,
            score.liquidity or 0.0,
            score.stabilization or 0.0,
        )
    return (
        score.total,
        score.stabilization or 0.0,
        score.liquidity or 0.0,
        score.low_position or 0.0,
    )


def _data_sources(provider, symbol: Optional[Symbol]) -> List[str]:
    sources = [provider.name]
    if symbol and symbol.source:
        sources.append(symbol.source)
    return sorted(set(sources))


def _persist_events(conn: sqlite3.Connection, candidates: Sequence[Candidate], events_client, session) -> None:
    for candidate in candidates:
        try:
            events = risk.summarize_events(candidate.symbol, events_client, session)
        except Exception:
            continue
        rows = list(events.get("recent_filings") or [])
        next_earnings = events.get("earnings_next_date")
        if next_earnings:
            rows.append(
                {
                    "symbol": candidate.symbol,
                    "event_date": next_earnings,
                    "event_type": "EARNINGS_ESTIMATE",
                    "title": "推算的下次财报日期（非官方日历）",
                    "source": "SEC EDGAR 8-K item 2.02 推算",
                    "detail": "",
                }
            )
        if rows:
            db.save_events(conn, rows)


def _log(conn: Optional[sqlite3.Connection], result: ScanResult, settings, persist: bool) -> None:
    if not persist or conn is None:
        return
    db.log_run(
        conn,
        result.session,
        status="skipped" if result.skipped_reason else "ok",
        provider=settings.data_source.provider,
        universe_size=result.universe_size,
        candidates=len(result.candidates),
        message=result.skipped_reason or "; ".join(result.notes[:2]),
    )


# --------------------------------------------------------------------------
# 补跑：关机 / 睡眠错过定时任务后，把缺的交易日补齐
# --------------------------------------------------------------------------
def pending_sessions(
    conn: Optional[sqlite3.Connection], settings, now: Optional[datetime] = None
) -> List[date]:
    """列出「已经收盘、但还没出过名单」的交易日。

    用途：Mac 关机时定时任务不会运行（launchd 和 cron 都不会），
    所以下次打开时必须能自动发现并补齐。
    """
    target = calendar_us.last_completed_session(now)
    last_done: Optional[date] = None
    if conn is not None:
        latest = db.list_candidate_sessions(conn, limit=1)
        if latest:
            last_done = date.fromisoformat(latest[0])
    if last_done is None:
        # 从来没有跑过：只补最近 max_days 天，避免第一次就抓十年数据
        window = settings.general.catch_up_max_days
        days = calendar_us.trading_days(
            calendar_us.shift_trading_days(target, -window - 1), target
        )
        return days[-window:]
    if last_done >= target:
        return []
    days = calendar_us.trading_days(last_done + timedelta(days=1), target)
    return days[: settings.general.catch_up_max_days]


def catch_up(
    settings,
    conn: Optional[sqlite3.Connection] = None,
    provider=None,
    max_days: Optional[int] = None,
    progress=None,
) -> List[ScanResult]:
    """把错过的交易日按顺序补跑一遍（只取一次行情，逐日回放，避免重复请求）。"""
    days = pending_sessions(conn, settings)
    if max_days is not None:
        days = days[:max_days]
    if not days:
        return []

    provider = provider or get_provider(settings.data_source.provider, settings)
    selection = pick_universe(settings, provider)
    benchmark = settings.general.benchmark
    fetch = sorted({s.ticker for s in selection.kept} | {benchmark})
    bars_map = provider.get_bars_multi(fetch)
    if progress:
        progress(0, len(days), None)

    from .backtest import HistoricalView

    results: List[ScanResult] = []
    for index, day in enumerate(days, 1):
        if progress and index > 1:
            progress(index, len(days), day)
        view = HistoricalView(provider, bars_map, day, selection.kept)
        result = run_scan(settings, session=day, conn=conn, provider=view)
        results.append(result)
    return results
