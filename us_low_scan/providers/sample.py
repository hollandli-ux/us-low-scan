"""离线合成数据源：不联网也能把整条链路跑通。

用途
----
1. 演示与验收：`python -m us_low_scan.cli demo` 立刻得到一份有内容的报告；
2. 测试：形态可复现（固定随机种子），用来验证每条筛选规则真的起作用。

合成市场里刻意包含多种"形态原型"，让每条规则都有对应的样本：

===================  ==========================================================
原型                  期望被哪一步处理
===================  ==========================================================
低位企稳             通过全部筛选，进入"低位企稳组"
低位继续下跌         进入"低位但尚未企稳"观察组，并带"持续创新低"标签
高位强势             距 52 周高点回撤不足，被阶段 B 过滤
流动性不足           20 日均额低于门槛，被阶段 A 过滤
低价股 / 微市值      被股价、市值门槛过滤
中位震荡             距 52 周低点过远，被阶段 B 过滤
异常波动             低位但波动率突增，带"异常波动"高风险标签
OTC / ETF / ADR      被证券类型过滤
===================  ==========================================================

注意：合成数据 **不是真实行情**，只用于验证工具本身。报告里会明确标注数据来源。
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import Dict, List, Optional

from .. import calendar_us
from ..models import Bar, Symbol
from .base import MarketDataProvider

# 形态原型 -> 权重（合计 1.0）
ARCHETYPES: Dict[str, float] = {
    "stabilized_low": 0.20,
    "falling_low": 0.18,
    "high_flyer": 0.18,
    "illiquid_low": 0.10,
    "penny_low": 0.06,
    "micro_cap_low": 0.06,
    "mid_range": 0.14,
    "volatile_low": 0.08,
}

# 各原型的目标市值区间（美元），用来验证市值门槛是否真的生效
MARKET_CAP_RANGES = {
    "stabilized_low": (2.0e9, 8.0e10),
    "falling_low": (1.5e9, 5.0e10),
    "high_flyer": (5.0e9, 2.0e11),
    "illiquid_low": (1.2e9, 9.0e9),      # 流动性不足，但市值达标
    "penny_low": (2.0e8, 2.0e9),         # 会被低价股门槛先拦下
    "micro_cap_low": (1.5e8, 9.0e8),     # 会被市值门槛拦下
    "mid_range": (3.0e9, 6.0e10),
    "volatile_low": (2.0e9, 4.0e10),
    "benchmark": (1.0e11, 1.0e11),
    "sector": (1.0e10, 1.0e10),
}

SECTORS = [
    ("Technology", "XLK"),
    ("Health Care", "XLV"),
    ("Industrials", "XLI"),
    ("Consumer Discretionary", "XLY"),
    ("Financials", "XLF"),
    ("Energy", "XLE"),
    ("Materials", "XLB"),
    ("Utilities", "XLU"),
]

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _invent_ticker(rng: random.Random, used: set) -> str:
    while True:
        length = rng.choice([3, 3, 4, 4, 5])
        ticker = "".join(rng.choice(_LETTERS) for _ in range(length))
        # 纯随机字母容易撞上真实代码；加一个不可交易的前缀段以示区隔
        candidate = f"Z{ticker}"
        if candidate not in used:
            used.add(candidate)
            return candidate


class SampleProvider(MarketDataProvider):
    name = "sample"
    coverage_note = (
        "离线合成数据（非真实行情）：仅用于验证筛选逻辑与报告格式，"
        "不能用于任何投资判断。"
    )

    def __init__(self, settings):
        super().__init__(settings)
        cfg = settings.data_source
        self.n_symbols = max(1, cfg.sample_symbols)
        self.n_days = max(120, cfg.sample_days)
        self.seed = cfg.sample_seed
        self._end_session = calendar_us.last_completed_session()
        self._sessions = calendar_us.trading_days(
            self._end_session - timedelta(days=int(self.n_days * 1.6)), self._end_session
        )[-self.n_days:]
        self._symbols: Optional[List[Symbol]] = None
        self._bars: Optional[Dict[str, List[Bar]]] = None
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        rng = random.Random(self.seed)
        symbols: List[Symbol] = []
        bars: Dict[str, List[Bar]] = {}
        used: set = set()

        archetypes = list(ARCHETYPES.keys())
        weights = [ARCHETYPES[a] for a in archetypes]

        for _ in range(self.n_symbols):
            archetype = rng.choices(archetypes, weights=weights, k=1)[0]
            ticker = _invent_ticker(rng, used)
            sector, sector_etf = rng.choice(SECTORS)
            name = f"Sample {archetype.replace('_', ' ').title()} {ticker}"
            exchange = rng.choice(["NYSE", "NASDAQ", "NYSE_AMERICAN"])

            symbol = Symbol(
                ticker=ticker,
                name=name,
                exchange=exchange,
                security_type="common_stock",
                is_otc=False,
                market_cap=None,
                sector=sector,
                sector_etf=sector_etf,
                listing_status="active",
                source="sample",
            )
            closes, dollar_volumes, _adv_target = self._simulate(rng, archetype)
            cap_low, cap_high = MARKET_CAP_RANGES.get(archetype, (2.0e9, 1.0e10))
            symbol = Symbol(
                ticker=symbol.ticker,
                name=symbol.name,
                exchange=symbol.exchange,
                security_type=symbol.security_type,
                is_otc=symbol.is_otc,
                market_cap=round(rng.uniform(cap_low, cap_high)),
                sector=symbol.sector,
                sector_etf=symbol.sector_etf,
                listing_status="active",
                source="sample",
                updated_at=date.today().isoformat(),
            )
            symbols.append(symbol)
            bars[ticker] = self._to_bars(ticker, closes, dollar_volumes, rng)

        # 基准与行业 ETF（用于相对强弱、市场环境判断）
        for etf, archetype in [("SPY", "benchmark")] + [(s[1], "sector") for s in SECTORS]:
            if etf in bars:
                continue
            closes, dollar_volumes, adv = self._simulate(rng, archetype)
            symbols.append(
                Symbol(
                    ticker=etf,
                    name=f"Sample ETF {etf}",
                    exchange="ARCA",
                    security_type="etf",
                    is_otc=False,
                    market_cap=None,
                    sector="",
                    sector_etf=None,
                    listing_status="active",
                    source="sample",
                    updated_at=date.today().isoformat(),
                )
            )
            bars[etf] = self._to_bars(etf, closes, dollar_volumes, rng)

        # 少量非普通股样本，用来验证阶段 A 的证券类型过滤
        for extra, kind, otc in [
            ("ZOTC1", "common_stock", True),
            ("ZADR1", "adr", False),
            ("ZETF1", "etf", False),
        ]:
            closes, dollar_volumes, _ = self._simulate(rng, "falling_low")
            symbols.append(
                Symbol(
                    ticker=extra,
                    name=f"Sample {kind} {extra}",
                    exchange="OTC" if otc else "NASDAQ",
                    security_type=kind,
                    is_otc=otc,
                    market_cap=5e9,
                    sector="Technology",
                    sector_etf="XLK",
                    source="sample",
                    updated_at=date.today().isoformat(),
                )
            )
            bars[extra] = self._to_bars(extra, closes, dollar_volumes, rng)

        self._symbols = symbols
        self._bars = bars

    # ------------------------------------------------------------------
    def _simulate(self, rng: random.Random, archetype: str):
        """返回 (收盘价序列, 美元成交额序列, 目标日均额)。"""
        n = len(self._sessions)
        p0 = rng.uniform(20, 260)

        if archetype == "benchmark":
            # 先温和上行，最后 45 个交易日回调 -> 演示"市场环境风险"标签
            drift = [0.0006] * (n - 45) + [-0.0035] * 45
            sigma = [0.008] * n
            adv = 2.0e10
        elif archetype == "sector":
            drift = [0.0004] * (n - 45) + [-0.0030] * 45
            sigma = [0.010] * n
            adv = 4.0e8
        elif archetype == "stabilized_low":
            decline = n - 45
            drift = [-0.0035] * decline + [0.0018] * 45
            sigma = [0.019] * decline + [0.012] * 45
            adv = rng.uniform(2.0e7, 6.0e8)
        elif archetype == "falling_low":
            drift = [-0.0026] * n
            sigma = [0.022] * n
            adv = rng.uniform(1.5e7, 2.0e8)
        elif archetype == "high_flyer":
            drift = [0.0016] * n
            sigma = [0.016] * n
            adv = rng.uniform(2.0e7, 5.0e8)
        elif archetype == "illiquid_low":
            decline = n - 40
            drift = [-0.0030] * decline + [0.0015] * 40
            sigma = [0.024] * n
            adv = rng.uniform(1.0e6, 6.0e6)
        elif archetype == "penny_low":
            decline = n - 30
            drift = [-0.0032] * decline + [0.0012] * 30
            sigma = [0.030] * n
            adv = rng.uniform(5.0e6, 4.0e7)
        elif archetype == "micro_cap_low":
            decline = n - 35
            drift = [-0.0028] * decline + [0.0014] * 35
            sigma = [0.026] * n
            adv = rng.uniform(4.0e6, 2.0e7)
        elif archetype == "mid_range":
            drift = [-0.0002] * n
            sigma = [0.013] * n
            adv = rng.uniform(3.0e7, 4.0e8)
        elif archetype == "volatile_low":
            decline = n - 25
            drift = [-0.0034] * decline + [0.0005] * 25
            sigma = [0.020] * decline + [0.075] * 25
            adv = rng.uniform(2.0e7, 1.5e8)
        else:
            drift = [0.0] * n
            sigma = [0.015] * n
            adv = 1.0e7

        # 原型级别的抖动，避免所有样本长得一模一样
        jitter = rng.uniform(0.85, 1.15)
        sigma = [s * jitter for s in sigma]

        closes = [p0]
        for i in range(1, n):
            shock = rng.gauss(0.0, 1.0) * sigma[i]
            price = closes[-1] * math.exp(drift[i] + shock)
            closes.append(max(price, 0.35))

        if archetype == "penny_low":
            # 强行压到 5 美元以下，确保触发低价股门槛
            scale = rng.uniform(2.5, 4.8) / closes[-1]
            closes = [c * scale for c in closes]

        # 美元成交额：对数正态波动 + 少量放量日
        dollar_volumes = []
        for i in range(n):
            noise = math.exp(rng.gauss(0.0, 0.35))
            value = adv * noise
            if rng.random() < 0.04:
                value *= rng.uniform(1.8, 4.0)
            dollar_volumes.append(value)
        # 让最近 20 日的均额围绕目标值稳定下来，避免随机性导致门槛判断漂移
        recent = dollar_volumes[-20:]
        scale = adv / (sum(recent) / len(recent))
        dollar_volumes[-20:] = [v * scale for v in recent]

        if archetype == "volatile_low":
            # 制造一次异常放量下挫，触发"异常波动"标签
            spike_index = n - rng.randint(3, 12)
            closes[spike_index] = closes[spike_index - 1] * rng.uniform(0.78, 0.86)
            for j in range(spike_index, n):
                closes[j] = min(closes[j], closes[spike_index] * rng.uniform(1.0, 1.05))
            dollar_volumes[spike_index] *= rng.uniform(3.0, 6.0)

        return closes, dollar_volumes, adv

    def _to_bars(self, ticker, closes, dollar_volumes, rng) -> List[Bar]:
        bars: List[Bar] = []
        for i, day in enumerate(self._sessions):
            close = closes[i]
            prev = closes[i - 1] if i else close
            open_ = prev * (1 + rng.gauss(0.0, 0.004))
            hi = max(open_, close) * (1 + abs(rng.gauss(0.0, 0.006)))
            lo = min(open_, close) * (1 - abs(rng.gauss(0.0, 0.006)))
            volume = dollar_volumes[i] / max(close, 0.01)
            bars.append(
                Bar(
                    symbol=ticker,
                    day=day,
                    open=round(open_, 4),
                    high=round(hi, 4),
                    low=round(max(lo, 0.01), 4),
                    close=round(close, 4),
                    volume=round(volume, 2),
                    source="sample",
                    adjustment="split",
                )
            )
        return bars

    # ------------------------------------------------------------------
    def list_symbols(self) -> List[Symbol]:
        return list(self._symbols or [])

    def get_daily_bars(
        self, symbol: str, start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Bar]:
        bars = (self._bars or {}).get(symbol.upper(), [])
        if start:
            bars = [b for b in bars if b.day >= start]
        if end:
            bars = [b for b in bars if b.day <= end]
        return list(bars)

    def get_benchmark_bars(self, symbol: str, start=None, end=None) -> List[Bar]:
        return self.get_daily_bars(symbol, start=start, end=end)

    def get_fundamentals(self, symbol: str):
        # 合成数据不提供基本面 -> 报告会显示"基本面覆盖不足"，总分按 80 分制
        return None
