"""历史名单检索：按日期、代码、分组、分数查历史候选，并回看其后续表现。

命令行与网页仪表板共用这一层，保证两边的口径完全一致。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Dict, List, Optional, Sequence

GROUP_LABELS = {"stabilized": "低位企稳组", "watch": "低位未企稳观察组"}

#: 数据库里存的是英文键；为兼容早期写入的中文值，读写时统一归一化
GROUP_ALIASES = {
    "stabilized": "stabilized",
    "低位企稳组": "stabilized",
    "watch": "watch",
    "低位未企稳观察组": "watch",
}


def normalize_group(value) -> str:
    text = str(value or "").strip()
    return GROUP_ALIASES.get(text, text or "watch")


def group_label(value) -> str:
    key = normalize_group(value)
    return GROUP_LABELS.get(key, key)


def _group_values(group: Optional[str]) -> List[str]:
    key = normalize_group(group) if group else ""
    if key == "stabilized":
        return ["stabilized", "低位企稳组"]
    if key == "watch":
        return ["watch", "低位未企稳观察组"]
    return []


def sessions_summary(conn: sqlite3.Connection, limit: int = 120) -> List[Dict[str, object]]:
    """每个交易日出过多少只、平均分多少，用于历史页与网页侧边栏。"""
    rows = conn.execute(
        """
        SELECT session,
               COUNT(*) AS total,
               SUM(CASE WHEN group_name IN ('stabilized', '低位企稳组')
                        THEN 1 ELSE 0 END) AS stabilized,
               SUM(CASE WHEN group_name IN ('watch', '低位未企稳观察组')
                        THEN 1 ELSE 0 END) AS watch,
               ROUND(AVG(total_score), 2) AS avg_score
        FROM daily_candidates
        GROUP BY session
        ORDER BY session DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in rows]


def latest_session(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT MAX(session) AS s FROM daily_candidates").fetchone()
    return row["s"] if row and row["s"] else None


def search_candidates(
    conn: sqlite3.Connection,
    ticker: Optional[str] = None,
    group: Optional[str] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    min_score: Optional[float] = None,
    limit: int = 300,
) -> List[Dict[str, object]]:
    """历史名单检索。所有条件都是可选的，可以任意组合。"""
    sql = ["SELECT * FROM daily_candidates WHERE 1 = 1"]
    params: List[object] = []
    if ticker:
        sql.append("AND symbol LIKE ?")
        params.append(f"%{ticker.strip().upper()}%")
    values = _group_values(group)
    if values:
        sql.append(f"AND group_name IN ({', '.join('?' for _ in values)})")
        params.extend(values)
    if start:
        sql.append("AND session >= ?")
        params.append(start.isoformat())
    if end:
        sql.append("AND session <= ?")
        params.append(end.isoformat())
    if min_score is not None:
        sql.append("AND total_score >= ?")
        params.append(float(min_score))
    sql.append("ORDER BY session DESC, total_score DESC LIMIT ?")
    params.append(int(limit))

    out = []
    for row in conn.execute(" ".join(sql), params):
        item = dict(row)
        for key in ("reasons", "risk_tags", "invalidation"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                item[key] = []
        item["group"] = normalize_group(item.get("group_name"))
        item["group_label"] = GROUP_LABELS.get(item["group"], item.get("group_name"))
        item.pop("payload", None)
        out.append(item)
    return out


def candidates_for_session(conn: sqlite3.Connection, session: str) -> List[Dict[str, object]]:
    rows = search_candidates(conn, start=date.fromisoformat(session),
                             end=date.fromisoformat(session), limit=500)
    return rows


def parse_payload(row: Dict[str, object]) -> Dict[str, object]:
    try:
        return json.loads(row.get("payload") or "{}")
    except json.JSONDecodeError:
        return {}


def symbol_timeline(conn: sqlite3.Connection, ticker: str, limit: int = 60) -> Dict[str, object]:
    """某只股票：什么时候被选过、当时什么状态、后来表现如何。"""
    ticker = ticker.strip().upper()
    rows = conn.execute(
        """
        SELECT c.*, o.return_5d, o.return_10d, o.return_20d,
               o.spy_return_5d, o.spy_return_10d, o.spy_return_20d,
               o.max_adverse_20d, o.new_low_10d
        FROM daily_candidates c
        LEFT JOIN candidate_outcomes o
          ON o.session = c.session AND o.symbol = c.symbol
        WHERE c.symbol = ?
        ORDER BY c.session DESC
        LIMIT ?
        """,
        (ticker, limit),
    )
    history = []
    for row in rows:
        item = dict(row)
        payload = parse_payload(item)
        features = payload.get("features") or {}
        history.append(
            {
                "session": item["session"],
                "group": normalize_group(item["group_name"]),
                "group_label": group_label(item["group_name"]),
                "total_score": item["total_score"],
                "max_score": item["max_score"],
                "close": item["close"],
                "distance_from_low_pct": item["distance_from_low_pct"],
                "drawdown_from_high_pct": item["drawdown_from_high_pct"],
                "avg_dollar_volume_20d": item["avg_dollar_volume_20d"],
                "rsi14": features.get("rsi14"),
                "name": item["name"],
                "exchange": item["exchange"],
                "return_5d": item["return_5d"],
                "return_10d": item["return_10d"],
                "return_20d": item["return_20d"],
                "spy_return_20d": item["spy_return_20d"],
                "max_adverse_20d": item["max_adverse_20d"],
                "new_low_10d": item["new_low_10d"],
            }
        )

    summary: Dict[str, object] = {"symbol": ticker, "appearances": len(history)}
    returns = [h["return_20d"] for h in history if h["return_20d"] is not None]
    if returns:
        summary["mean_return_20d"] = sum(returns) / len(returns)
        summary["hit_rate_20d"] = sum(1 for r in returns if r > 0) / len(returns)
    new_lows = [h["new_low_10d"] for h in history if h["new_low_10d"] is not None]
    if new_lows:
        summary["new_low_rate_10d"] = sum(new_lows) / len(new_lows)
    return {"summary": summary, "history": history}


def sessions_csv_rows(conn: sqlite3.Connection, limit: int = 120) -> List[Dict[str, object]]:
    return sessions_summary(conn, limit=limit)


def count_candidates(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM daily_candidates").fetchone()
    return int(row["c"]) if row else 0


def top_symbols(conn: sqlite3.Connection, limit: int = 12) -> List[Dict[str, object]]:
    """出现次数最多的股票，用来观察"是不是反复入选同一批"。"""
    rows = conn.execute(
        """
        SELECT symbol, name, COUNT(*) AS appearances, ROUND(AVG(total_score), 1) AS avg_score,
               MAX(session) AS last_session
        FROM daily_candidates
        GROUP BY symbol
        ORDER BY appearances DESC, avg_score DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in rows]


def session_candidates_detail(
    conn: sqlite3.Connection, session: str
) -> List[Dict[str, object]]:
    """某日候选的完整信息（含特征、理由、风险标签、失效条件），给网页用。"""
    rows = conn.execute(
        """
        SELECT * FROM daily_candidates WHERE session = ?
        ORDER BY CASE WHEN group_name IN ('stabilized', '低位企稳组') THEN 0 ELSE 1 END,
             total_score DESC
        """,
        (session,),
    )
    out = []
    for row in rows:
        item = dict(row)
        payload = parse_payload(item)
        for key in ("reasons", "risk_tags", "invalidation"):
            value = item.get(key)
            if isinstance(value, str):
                try:
                    item[key] = json.loads(value)
                except json.JSONDecodeError:
                    item[key] = []
        item["group"] = normalize_group(item.get("group_name"))
        item["group_label"] = GROUP_LABELS.get(item["group"], item.get("group_name"))
        item["features"] = payload.get("features") or {}
        item["group_reason"] = payload.get("group_reason", "")
        item["score"] = payload.get("score") or {}
        item["data_sources"] = payload.get("data_sources") or []
        item.pop("payload", None)
        out.append(item)
    return out
