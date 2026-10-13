"""SQLite 存储层。

表结构对应产品构想第 8 节的数据表建议：

* symbols            证券静态信息（交易所、类型、市值、上市状态）
* daily_bars         日线 OHLCV + 美元成交额 + 数据源 + 调整口径
* daily_features     52 周高低点距离、回撤、RSI、均线、相对强弱
* daily_candidates   评分、分组、入选理由、风险标签、失效条件
* corporate_events   财报日期、SEC 申报、公司行动（每条都带来源与时间戳）
* candidate_outcomes 入选后 5/10/20 个交易日的表现及最大不利波动
* run_log            每次运行的元信息，便于排查"今天为什么没出名单"
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .models import Bar, Candidate, Features, RiskTag, Symbol

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    ticker          TEXT PRIMARY KEY,
    name            TEXT,
    exchange        TEXT,
    security_type   TEXT,
    is_otc          INTEGER DEFAULT 0,
    market_cap      REAL,
    sector          TEXT,
    sector_etf      TEXT,
    listing_status  TEXT DEFAULT 'active',
    source          TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS daily_bars (
    symbol          TEXT NOT NULL,
    day             TEXT NOT NULL,
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,
    volume          REAL,
    dollar_volume   REAL,
    source          TEXT,
    adjustment      TEXT,
    PRIMARY KEY (symbol, day)
);
CREATE INDEX IF NOT EXISTS idx_daily_bars_day ON daily_bars(day);

CREATE TABLE IF NOT EXISTS daily_features (
    symbol                  TEXT NOT NULL,
    session                 TEXT NOT NULL,
    payload                 TEXT NOT NULL,
    PRIMARY KEY (symbol, session)
);

CREATE TABLE IF NOT EXISTS daily_candidates (
    session         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    name            TEXT,
    exchange        TEXT,
    sector          TEXT,
    group_name      TEXT,
    total_score     REAL,
    max_score       REAL,
    score_liquidity REAL,
    score_low       REAL,
    score_stab      REAL,
    score_quality   REAL,
    close           REAL,
    avg_dollar_volume_20d REAL,
    distance_from_low_pct REAL,
    drawdown_from_high_pct REAL,
    reasons         TEXT,
    risk_tags       TEXT,
    invalidation    TEXT,
    payload         TEXT,
    created_at      TEXT,
    PRIMARY KEY (session, symbol)
);

CREATE TABLE IF NOT EXISTS corporate_events (
    symbol          TEXT NOT NULL,
    event_date      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    title           TEXT,
    source          TEXT,
    url             TEXT,
    fetched_at      TEXT,
    detail          TEXT,
    PRIMARY KEY (symbol, event_date, event_type, title)
);
CREATE INDEX IF NOT EXISTS idx_events_symbol ON corporate_events(symbol, event_date);

CREATE TABLE IF NOT EXISTS candidate_outcomes (
    session         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    group_name      TEXT,
    entry_close     REAL,
    return_5d       REAL,
    return_10d      REAL,
    return_20d      REAL,
    spy_return_5d   REAL,
    spy_return_10d  REAL,
    spy_return_20d  REAL,
    max_adverse_5d  REAL,
    max_adverse_10d REAL,
    max_adverse_20d REAL,
    new_low_10d     INTEGER,
    horizon_used    INTEGER,
    measured_at     TEXT,
    PRIMARY KEY (session, symbol)
);

CREATE TABLE IF NOT EXISTS run_log (
    run_at          TEXT NOT NULL,
    session         TEXT,
    status          TEXT,
    provider        TEXT,
    universe_size   INTEGER,
    candidates      INTEGER,
    message         TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    """打开（并初始化）数据库。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------
def upsert_symbols(conn: sqlite3.Connection, symbols: Iterable[Symbol]) -> int:
    rows = [
        (
            s.ticker,
            s.name,
            s.exchange,
            s.security_type,
            1 if s.is_otc else 0,
            s.market_cap,
            s.sector,
            s.sector_etf,
            s.listing_status,
            s.source,
            s.updated_at or datetime.now().isoformat(timespec="seconds"),
        )
        for s in symbols
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO symbols (ticker, name, exchange, security_type, is_otc, market_cap,
                             sector, sector_etf, listing_status, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            name=excluded.name,
            exchange=excluded.exchange,
            security_type=excluded.security_type,
            is_otc=excluded.is_otc,
            market_cap=excluded.market_cap,
            sector=excluded.sector,
            sector_etf=excluded.sector_etf,
            listing_status=excluded.listing_status,
            source=excluded.source,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_bars(conn: sqlite3.Connection, bars: Iterable[Bar]) -> int:
    rows = [
        (
            b.symbol,
            b.day.isoformat(),
            b.open,
            b.high,
            b.low,
            b.close,
            b.volume,
            b.dollar_volume,
            b.source,
            b.adjustment,
        )
        for b in bars
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO daily_bars (symbol, day, open, high, low, close, volume,
                                dollar_volume, source, adjustment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, day) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            dollar_volume=excluded.dollar_volume, source=excluded.source,
            adjustment=excluded.adjustment
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def save_features(conn: sqlite3.Connection, features: Iterable[Features]) -> int:
    rows = [
        (f.symbol, f.session.isoformat(), json.dumps(f.as_row(), ensure_ascii=False))
        for f in features
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO daily_features (symbol, session, payload) VALUES (?, ?, ?)
        ON CONFLICT(symbol, session) DO UPDATE SET payload=excluded.payload
        """,
        rows,
    )
    conn.commit()
    return len(rows)


GROUP_LABELS = {"stabilized": "低位企稳组", "watch": "低位未企稳观察组"}


def save_candidates(
    conn: sqlite3.Connection, session: date, candidates: Sequence[Candidate]
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for c in candidates:
        rows.append(
            (
                session.isoformat(),
                c.symbol,
                c.name,
                c.exchange,
                c.sector,
                c.group,
                c.score.total,
                c.score.max_available,
                c.score.liquidity,
                c.score.low_position,
                c.score.stabilization,
                c.score.quality,
                c.features.close,
                c.features.avg_dollar_volume_20d,
                c.features.distance_from_low_pct,
                c.features.drawdown_from_high_pct,
                json.dumps(c.reasons, ensure_ascii=False),
                json.dumps([t.as_dict() for t in c.risk_tags], ensure_ascii=False),
                json.dumps(c.invalidation, ensure_ascii=False),
                json.dumps(c.as_dict(), ensure_ascii=False),
                now,
            )
        )
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO daily_candidates (session, symbol, name, exchange, sector, group_name,
            total_score, max_score, score_liquidity, score_low, score_stab, score_quality,
            close, avg_dollar_volume_20d, distance_from_low_pct, drawdown_from_high_pct,
            reasons, risk_tags, invalidation, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session, symbol) DO UPDATE SET
            name=excluded.name, exchange=excluded.exchange, sector=excluded.sector,
            group_name=excluded.group_name, total_score=excluded.total_score,
            max_score=excluded.max_score, score_liquidity=excluded.score_liquidity,
            score_low=excluded.score_low, score_stab=excluded.score_stab,
            score_quality=excluded.score_quality, close=excluded.close,
            avg_dollar_volume_20d=excluded.avg_dollar_volume_20d,
            distance_from_low_pct=excluded.distance_from_low_pct,
            drawdown_from_high_pct=excluded.drawdown_from_high_pct,
            reasons=excluded.reasons, risk_tags=excluded.risk_tags,
            invalidation=excluded.invalidation, payload=excluded.payload,
            created_at=excluded.created_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def save_events(conn: sqlite3.Connection, events: Iterable[Dict[str, object]]) -> int:
    rows = []
    for e in events:
        rows.append(
            (
                e.get("symbol"),
                str(e.get("event_date")),
                e.get("event_type"),
                e.get("title", ""),
                e.get("source", ""),
                e.get("url", ""),
                e.get("fetched_at") or datetime.now().isoformat(timespec="seconds"),
                e.get("detail", ""),
            )
        )
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT OR REPLACE INTO corporate_events
            (symbol, event_date, event_type, title, source, url, fetched_at, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def save_outcome(conn: sqlite3.Connection, outcome: Dict[str, object]) -> None:
    conn.execute(
        """
        INSERT INTO candidate_outcomes (session, symbol, group_name, entry_close,
            return_5d, return_10d, return_20d, spy_return_5d, spy_return_10d, spy_return_20d,
            max_adverse_5d, max_adverse_10d, max_adverse_20d, new_low_10d, horizon_used,
            measured_at)
        VALUES (:session, :symbol, :group_name, :entry_close, :return_5d, :return_10d,
            :return_20d, :spy_return_5d, :spy_return_10d, :spy_return_20d, :max_adverse_5d,
            :max_adverse_10d, :max_adverse_20d, :new_low_10d, :horizon_used, :measured_at)
        ON CONFLICT(session, symbol) DO UPDATE SET
            group_name=excluded.group_name, entry_close=excluded.entry_close,
            return_5d=excluded.return_5d, return_10d=excluded.return_10d,
            return_20d=excluded.return_20d, spy_return_5d=excluded.spy_return_5d,
            spy_return_10d=excluded.spy_return_10d, spy_return_20d=excluded.spy_return_20d,
            max_adverse_5d=excluded.max_adverse_5d, max_adverse_10d=excluded.max_adverse_10d,
            max_adverse_20d=excluded.max_adverse_20d, new_low_10d=excluded.new_low_10d,
            horizon_used=excluded.horizon_used, measured_at=excluded.measured_at
        """,
        outcome,
    )
    conn.commit()


def log_run(
    conn: sqlite3.Connection,
    session: Optional[date],
    status: str,
    provider: str,
    universe_size: int,
    candidates: int,
    message: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO run_log (run_at, session, status, provider, universe_size, candidates, message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            session.isoformat() if session else None,
            status,
            provider,
            universe_size,
            candidates,
            message,
        ),
    )
    conn.commit()


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
def load_symbols(conn: sqlite3.Connection, tickers: Optional[Iterable[str]] = None) -> List[Symbol]:
    sql = "SELECT * FROM symbols"
    params: List[object] = []
    if tickers is not None:
        tickers = list(tickers)
        if not tickers:
            return []
        placeholders = ",".join("?" for _ in tickers)
        sql += f" WHERE ticker IN ({placeholders})"
        params.extend(tickers)
    sql += " ORDER BY ticker"
    out = []
    for row in conn.execute(sql, params):
        out.append(
            Symbol(
                ticker=row["ticker"],
                name=row["name"] or "",
                exchange=row["exchange"] or "",
                security_type=row["security_type"] or "common_stock",
                is_otc=bool(row["is_otc"]),
                market_cap=row["market_cap"],
                sector=row["sector"] or "",
                sector_etf=row["sector_etf"],
                listing_status=row["listing_status"] or "active",
                source=row["source"] or "",
                updated_at=row["updated_at"],
            )
        )
    return out


def load_bars(
    conn: sqlite3.Connection,
    symbol: str,
    end: Optional[date] = None,
    limit: Optional[int] = None,
) -> List[Bar]:
    """按日期升序返回 K 线，可截断到 ``end`` 之前、只取最近 ``limit`` 根。"""
    sql = "SELECT * FROM daily_bars WHERE symbol = ?"
    params: List[object] = [symbol]
    if end is not None:
        sql += " AND day <= ?"
        params.append(end.isoformat())
    sql += " ORDER BY day DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = list(conn.execute(sql, params))
    rows.reverse()
    return [
        Bar(
            symbol=r["symbol"],
            day=date.fromisoformat(r["day"]),
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            source=r["source"] or "",
            adjustment=r["adjustment"] or "",
            dollar_volume=r["dollar_volume"],
        )
        for r in rows
    ]


def load_bars_map(
    conn: sqlite3.Connection,
    symbols: Sequence[str],
    end: Optional[date] = None,
    limit: Optional[int] = None,
) -> Dict[str, List[Bar]]:
    """一次性把多只股票的 K 线读进内存（按需截断），避免 N+1 查询。"""
    out: Dict[str, List[Bar]] = {s: [] for s in symbols}
    if not symbols:
        return out
    chunk = 500
    for start in range(0, len(symbols), chunk):
        batch = symbols[start:start + chunk]
        placeholders = ",".join("?" for _ in batch)
        sql = f"SELECT * FROM daily_bars WHERE symbol IN ({placeholders})"
        params: List[object] = list(batch)
        if end is not None:
            sql += " AND day <= ?"
            params.append(end.isoformat())
        sql += " ORDER BY symbol, day"
        for r in conn.execute(sql, params):
            out[r["symbol"]].append(
                Bar(
                    symbol=r["symbol"],
                    day=date.fromisoformat(r["day"]),
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=r["volume"],
                    source=r["source"] or "",
                    adjustment=r["adjustment"] or "",
                    dollar_volume=r["dollar_volume"],
                )
            )
    if limit:
        out = {k: v[-limit:] for k, v in out.items()}
    return out


def latest_bar_date(conn: sqlite3.Connection, symbols: Optional[Sequence[str]] = None) -> Optional[date]:
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        row = conn.execute(
            f"SELECT MAX(day) AS d FROM daily_bars WHERE symbol IN ({placeholders})", list(symbols)
        ).fetchone()
    else:
        row = conn.execute("SELECT MAX(day) AS d FROM daily_bars").fetchone()
    if not row or not row["d"]:
        return None
    return date.fromisoformat(row["d"])


def load_events(
    conn: sqlite3.Connection, symbol: str, start: Optional[date] = None, end: Optional[date] = None
) -> List[sqlite3.Row]:
    sql = "SELECT * FROM corporate_events WHERE symbol = ?"
    params: List[object] = [symbol]
    if start:
        sql += " AND event_date >= ?"
        params.append(start.isoformat())
    if end:
        sql += " AND event_date <= ?"
        params.append(end.isoformat())
    sql += " ORDER BY event_date"
    return list(conn.execute(sql, params))


def load_candidates(conn: sqlite3.Connection, session: date) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM daily_candidates WHERE session = ? ORDER BY total_score DESC",
            (session.isoformat(),),
        )
    )


def list_candidate_sessions(conn: sqlite3.Connection, limit: int = 60) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT session FROM daily_candidates ORDER BY session DESC LIMIT ?", (limit,)
    )
    return [r["session"] for r in rows]


def bar_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM daily_bars").fetchone()
    return int(row["c"]) if row else 0
