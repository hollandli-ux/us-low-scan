"""候选股入选后的表现跟踪（产品构想第 8、9 节）。

对每个入选记录计算：

* 入选后 5 / 10 / 20 个交易日的收益（相对入选当日收盘价）
* 同一窗口内 SPY 的收益（用来判断是"选得准"还是"大盘在涨"）
* 最大不利波动（MAE）：窗口内最低价相对入选价的跌幅
* 10 个交易日内是否跌破入选时的 52 周低点（"持续创新低"比例）

这一步是校正规则、判断"企稳过滤到底有没有用"的关键，不能省。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from . import db
from .models import Bar

HORIZONS = (5, 10, 20)


def update_outcomes(
    conn: sqlite3.Connection,
    settings,
    as_of: Optional[date] = None,
    only_incomplete: bool = True,
) -> int:
    """为已保存的候选股补齐前瞻表现。返回更新的记录数。"""
    as_of = as_of or date.today()
    benchmark = settings.general.benchmark
    spy_bars = db.load_bars(conn, benchmark)
    spy_closes = {b.day: b.close for b in spy_bars}
    spy_days = sorted(spy_closes)

    rows = list(
        conn.execute(
            """
            SELECT c.session, c.symbol, c.group_name, c.close, c.payload,
                   o.horizon_used AS measured_horizon
            FROM daily_candidates c
            LEFT JOIN candidate_outcomes o
              ON o.session = c.session AND o.symbol = c.symbol
            ORDER BY c.session, c.symbol
            """
        )
    )

    updated = 0
    for row in rows:
        session = date.fromisoformat(row["session"])
        symbol = row["symbol"]
        if only_incomplete and row["measured_horizon"] is not None:
            continue
        bars = [b for b in db.load_bars(conn, symbol) if b.day > session]
        if not bars:
            continue
        payload = {}
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        low_52w = ((payload.get("features") or {}).get("low_52w"))
        entry_close = row["close"]

        outcome = _measure(
            session, symbol, entry_close, bars, spy_closes, spy_days, low_52w
        )
        outcome["group_name"] = row["group_name"]
        outcome["measured_at"] = datetime.now().isoformat(timespec="seconds")
        db.save_outcome(conn, outcome)
        updated += 1
    return updated


def _measure(
    session: date,
    symbol: str,
    entry_close: Optional[float],
    forward_bars: Sequence[Bar],
    spy_closes: Dict[date, float],
    spy_days: Sequence[date],
    low_52w: Optional[float],
) -> Dict[str, object]:
    horizon_used = min(len(forward_bars), max(HORIZONS))
    outcome: Dict[str, object] = {
        "session": session.isoformat(),
        "symbol": symbol,
        "entry_close": entry_close,
        "return_5d": None,
        "return_10d": None,
        "return_20d": None,
        "spy_return_5d": None,
        "spy_return_10d": None,
        "spy_return_20d": None,
        "max_adverse_5d": None,
        "max_adverse_10d": None,
        "max_adverse_20d": None,
        "new_low_10d": None,
        "horizon_used": horizon_used,
    }
    if entry_close:
        for horizon in HORIZONS:
            if len(forward_bars) >= horizon:
                window = forward_bars[:horizon]
                outcome[f"return_{horizon}d"] = window[-1].close / entry_close - 1.0
                outcome[f"max_adverse_{horizon}d"] = min(b.low for b in window) / entry_close - 1.0
                spy_return = _spy_return(spy_closes, spy_days, session, window[-1].day)
                outcome[f"spy_return_{horizon}d"] = spy_return
    if low_52w is not None and len(forward_bars) >= 1:
        window = forward_bars[:10]
        outcome["new_low_10d"] = 1 if min(b.low for b in window) < low_52w else 0
    return outcome


def _spy_return(
    spy_closes: Dict[date, float], spy_days: Sequence[date], start: date, end: date
) -> Optional[float]:
    if not spy_days or start not in spy_closes or end not in spy_closes:
        return None
    base = spy_closes[start]
    if not base:
        return None
    return spy_closes[end] / base - 1.0


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------
def summarize(conn: sqlite3.Connection, group_filter: Optional[str] = None) -> Dict[str, object]:
    from .history import _group_values

    sql = "SELECT * FROM candidate_outcomes"
    params: List[object] = []
    values = _group_values(group_filter)
    if values:
        sql += f" WHERE group_name IN ({', '.join('?' for _ in values)})"
        params.extend(values)
    rows = [dict(r) for r in conn.execute(sql, params)]
    return summarize_rows(rows)


def summarize_rows(rows: Sequence[Dict[str, object]]) -> Dict[str, object]:
    total = len(rows)
    if total == 0:
        return {"count": 0, "note": "暂无已跟踪的候选记录（先运行 scan，再运行 outcomes）"}

    summary: Dict[str, object] = {"count": total}
    for horizon in HORIZONS:
        returns = [r[f"return_{horizon}d"] for r in rows if r.get(f"return_{horizon}d") is not None]
        spy_returns = [r[f"spy_return_{horizon}d"] for r in rows if r.get(f"spy_return_{horizon}d") is not None]
        excess = [
            r[f"return_{horizon}d"] - r[f"spy_return_{horizon}d"]
            for r in rows
            if r.get(f"return_{horizon}d") is not None and r.get(f"spy_return_{horizon}d") is not None
        ]
        mae = [r[f"max_adverse_{horizon}d"] for r in rows if r.get(f"max_adverse_{horizon}d") is not None]
        entry = {
            "samples": len(returns),
            "mean": _mean(returns),
            "median": _median(returns),
            "hit_rate": (
                sum(1 for x in returns if x > 0) / len(returns) if returns else None
            ),
            "spy_mean": _mean(spy_returns),
            "mean_excess_vs_spy": _mean(excess),
            "beat_spy_rate": (
                sum(1 for x in excess if x > 0) / len(excess) if excess else None
            ),
            "mean_max_adverse": _mean(mae),
        }
        summary[f"horizon_{horizon}d"] = entry

    new_low = [r["new_low_10d"] for r in rows if r.get("new_low_10d") is not None]
    summary["new_low_rate_10d"] = (sum(new_low) / len(new_low)) if new_low else None
    return summary


def _mean(values: Sequence[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _median(values: Sequence[float]) -> Optional[float]:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def render_summary_markdown(summary: Dict[str, object], title: str = "候选股表现跟踪") -> str:
    lines = [f"# {title}", ""]
    if summary.get("count", 0) == 0:
        lines.append(summary.get("note", "暂无数据"))
        return "\n".join(lines)

    lines.append(f"- 已跟踪候选记录：{summary['count']} 条")
    if summary.get("new_low_rate_10d") is not None:
        lines.append(
            f"- 入选后 10 个交易日内跌破入选时 52 周低点的比例："
            f"{summary['new_low_rate_10d']*100:.1f}%（越低说明低位判断越稳）"
        )
    lines.append("")
    lines.append("| 窗口 | 样本 | 平均收益 | 中位收益 | 胜率 | 平均超额(相对SPY) | 跑赢SPY比例 | 平均最大不利波动 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for horizon in HORIZONS:
        entry = summary.get(f"horizon_{horizon}d") or {}
        lines.append(
            "| {h} 个交易日 | {n} | {mean} | {median} | {hit} | {excess} | {beat} | {mae} |".format(
                h=horizon,
                n=entry.get("samples", 0),
                mean=_fmt_pct(entry.get("mean")),
                median=_fmt_pct(entry.get("median")),
                hit=_fmt_pct(entry.get("hit_rate"), digits=1, percent=True),
                excess=_fmt_pct(entry.get("mean_excess_vs_spy")),
                beat=_fmt_pct(entry.get("beat_spy_rate"), digits=1, percent=True),
                mae=_fmt_pct(entry.get("mean_max_adverse")),
            )
        )
    lines.append("")
    lines.append(
        "> 历史表现不保证未来表现；样本量小的时候统计意义有限，"
        "请先确认数据完整、结果可复现、理由可追溯，再讨论收益。"
    )
    return "\n".join(lines)


def _fmt_pct(value, digits: int = 2, percent: bool = False) -> str:
    if value is None:
        return "未核实"
    if percent:
        return f"{value*100:.{digits}f}%"
    return f"{value*100:+.{digits}f}%" if value != 0 else "0.00%"
