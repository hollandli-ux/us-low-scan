"""历史回放（backtest）：验证规则本身，而不是只看某天的名单"顺不顺眼"。

无未来数据泄漏的做法
--------------------
* 每个交易日只把"该日及之前"的 K 线交给筛选逻辑（``HistoricalView``）。
* 前瞻收益单独用完整数据计算，与筛选过程完全隔离。
* 同一份筛选代码（``pipeline.run_scan``）同时用于实盘扫描与回放，
  避免出现"回测一套、实盘一套"的隐性偏差。

仍需注意的偏差（会在报告里明确写出来）
--------------------------------------
* 幸存者偏差：股票池来自"今天仍然上市"的名单，已退市股票可能缺失。
* 数据源历史口径：拆股/分红调整、退市处理方式不同，结果会有差异。
* 交易成本：本回测只算价格变化，没有自动扣手续费与滑点。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

from . import calendar_us, outcomes as outcome_module
from .config import Settings
from .models import Bar
from .pipeline import run_scan
from .providers import get_provider

HORIZONS = outcome_module.HORIZONS


class HistoricalView:
    """把某个 provider 的行情截断到指定交易日，模拟"当天能看到的全部数据"。"""

    def __init__(self, provider, bars_map: Dict[str, List[Bar]], session: date, symbols):
        self._provider = provider
        self._bars = bars_map
        self._session = session
        self._symbols = symbols
        self.name = provider.name
        self.coverage_note = provider.coverage_note

    def list_symbols(self):
        return list(self._symbols)

    def get_bars_multi(self, symbols: Sequence[str], **kwargs) -> Dict[str, List[Bar]]:
        return {
            symbol: [b for b in self._bars.get(symbol, []) if b.day <= self._session]
            for symbol in symbols
        }

    def get_daily_bars(self, symbol: str, **kwargs) -> List[Bar]:
        return [b for b in self._bars.get(symbol, []) if b.day <= self._session]

    def get_benchmark_bars(self, symbol: str, **kwargs) -> List[Bar]:
        return self.get_daily_bars(symbol)

    def get_fundamentals(self, symbol: str):
        return None


def run_backtest(
    settings: Settings,
    start: Optional[date] = None,
    end: Optional[date] = None,
    step: int = 1,
    provider=None,
    min_history_days: Optional[int] = None,
    progress=None,
) -> Dict[str, object]:
    """在 [start, end] 区间内逐日回放。返回统计结果（含明细）。"""
    provider = provider or get_provider(settings.data_source.provider, settings)
    lookback = settings.filters.lookback_days
    min_history = min_history_days or max(lookback, 200) + 5

    symbols = provider.list_symbols()
    tickers = [s.ticker for s in symbols]
    benchmark = settings.general.benchmark
    fetch = sorted(set(tickers) | {benchmark})
    bars_map = provider.get_bars_multi(fetch)

    all_days = sorted({b.day for bars in bars_map.values() for b in bars})
    if not all_days:
        raise RuntimeError("回测失败：没有取到任何行情数据")
    earliest = all_days[0]
    latest = all_days[-1]

    end = end or latest
    default_start = all_days[min(len(all_days) - 1, min_history)]
    start = start or default_start
    if start < earliest + timedelta(days=1):
        start = default_start

    import bisect

    sessions = []
    for day in calendar_us.trading_days(start, end):
        # 只回放"至少已经有 min_history 根 K 线"的日期，避免早期样本数据不足
        if bisect.bisect_right(all_days, day) >= min_history:
            sessions.append(day)
    sessions = sessions[:: max(1, step)]

    # 回测需要看到"所有低位候选"，因此放开输出数量上限，分组后再统计
    loose_output = replace(
        settings.output, max_main_candidates=10 ** 6, max_watch_candidates=10 ** 6
    )
    base_settings = replace(settings, output=loose_output)

    picks: List[Dict[str, object]] = []
    sessions_used = 0
    for index, session in enumerate(sessions):
        view = HistoricalView(provider, bars_map, session, symbols)
        result = run_scan(base_settings, session=session, provider=view, persist=False)
        if result.skipped_reason:
            continue
        sessions_used += 1
        for candidate in result.candidates:
            forward = [b for b in bars_map.get(candidate.symbol, []) if b.day > session]
            record = {
                "session": session,
                "symbol": candidate.symbol,
                "group": candidate.group,
                "score": candidate.score.total,
                "stabilization": candidate.score.stabilization,
                "entry_close": candidate.features.close,
                "low_52w": candidate.features.low_52w,
                "forward": forward,
            }
            picks.append(record)
        if progress and index % 20 == 0:
            progress(index + 1, len(sessions))

    spy_closes = {b.day: b.close for b in bars_map.get(benchmark, [])}
    spy_days = sorted(spy_closes)
    rows = [
        _measure_pick(record, spy_closes, spy_days) for record in picks
    ]

    by_group = {
        "低位企稳组": outcome_module.summarize_rows([r for r in rows if r["group"] == "stabilized"]),
        "低位但尚未企稳组": outcome_module.summarize_rows([r for r in rows if r["group"] == "watch"]),
        "全部低位候选（不分组）": outcome_module.summarize_rows(rows),
    }

    return {
        "sessions": [d.isoformat() for d in sessions],
        "sessions_used": sessions_used,
        "start": sessions[0].isoformat() if sessions else None,
        "end": sessions[-1].isoformat() if sessions else None,
        "provider": provider.name,
        "benchmark": benchmark,
        "picks": len(rows),
        "by_group": by_group,
        "rows": rows,
        "notes": [
            "回放只使用当日及之前的数据，前瞻收益单独计算，不存在未来数据泄漏。",
            "股票池来自当前可用的代码列表，无法完全消除幸存者偏差"
            "（已退市股票可能缺失），若数据源提供退市名单请一并纳入。",
            "未扣除交易成本、价差与滑点，因此结果偏乐观，"
            "真实落地需要再打一个折扣。",
        ],
    }


def _measure_pick(record: Dict[str, object], spy_closes, spy_days) -> Dict[str, object]:
    session = record["session"]
    entry = record["entry_close"]
    forward = record["forward"]
    out = {
        "session": session,
        "symbol": record["symbol"],
        "group": record["group"],
        "score": record["score"],
        "stabilization": record["stabilization"],
        "entry_close": entry,
    }
    for horizon in HORIZONS:
        out[f"return_{horizon}d"] = None
        out[f"spy_return_{horizon}d"] = None
        out[f"max_adverse_{horizon}d"] = None
    out["new_low_10d"] = None
    if entry:
        for horizon in HORIZONS:
            if len(forward) >= horizon:
                window = forward[:horizon]
                out[f"return_{horizon}d"] = window[-1].close / entry - 1.0
                out[f"max_adverse_{horizon}d"] = min(b.low for b in window) / entry - 1.0
                out[f"spy_return_{horizon}d"] = _spy_return(
                    spy_closes, session, window[-1].day
                )
    low_52w = record.get("low_52w")
    if low_52w and len(forward) >= 1:
        out["new_low_10d"] = 1 if min(b.low for b in forward[:10]) < low_52w else 0
    return out


def _spy_return(spy_closes, start: date, end: date) -> Optional[float]:
    if start not in spy_closes or end not in spy_closes:
        return None
    base = spy_closes[start]
    return spy_closes[end] / base - 1.0 if base else None


def render_backtest_markdown(report: Dict[str, object], settings: Settings) -> str:
    lines: List[str] = []
    lines.append("# 历史回放报告（低位筛选规则验证）")
    lines.append("")
    lines.append(f"- 回放区间：{report['start']} ~ {report['end']}（共 {report['sessions_used']} 个交易日）")
    lines.append(f"- 数据源：`{report['provider']}`；基准：{report['benchmark']}")
    lines.append(f"- 入选记录数：{report['picks']} 条")
    lines.append("")
    for group, summary in report["by_group"].items():
        lines.append(f"## {group}")
        lines.append("")
        lines.append(outcome_module.render_summary_markdown(summary, title=group).split("\n", 1)[1].strip())
        lines.append("")
    lines.append("## 口径与偏差说明")
    lines.append("")
    for note in report["notes"]:
        lines.append(f"- {note}")
    lines.append("")
    lines.append(
        "> 验收重点不是「收益率超过大盘」，而是：数据是否完整、结果能否复现、"
        "每条理由能否追溯到指标、重大风险是否被标记。"
    )
    return "\n".join(lines)
