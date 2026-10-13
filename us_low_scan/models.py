"""领域模型：证券、K 线、特征、候选股、风险标签、评分明细。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

# 对"未被数据源覆盖"的项目，一律使用该字面量，绝不默认成"安全"
UNVERIFIED = "未核实"


@dataclass(frozen=True)
class Symbol:
    """一只证券的静态信息。"""

    ticker: str
    name: str = ""
    exchange: str = ""
    security_type: str = "common_stock"   # common_stock | adr | etf | ...
    is_otc: bool = False
    market_cap: Optional[float] = None
    sector: str = ""
    sector_etf: Optional[str] = None
    listing_status: str = "active"        # active | delisted | halted
    source: str = ""
    updated_at: Optional[str] = None

    @property
    def exchange_key(self) -> str:
        return (self.exchange or "").strip().upper().replace(" ", "_")


@dataclass(frozen=True)
class Bar:
    """日线 OHLCV。``close`` 与 ``volume`` 必须与所用调整口径一致。"""

    symbol: str
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str = ""
    adjustment: str = ""        # raw | split | all
    dollar_volume: Optional[float] = None

    def __post_init__(self) -> None:
        if self.dollar_volume is None:
            object.__setattr__(self, "dollar_volume", self.close * self.volume)


@dataclass
class RiskTag:
    """风险标签：code 用于程序判断，label 用于报告展示。"""

    code: str
    label: str
    severity: str = "info"      # info | warn | high
    detail: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "label": self.label,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass
class ScoreBreakdown:
    """100 分制的分项得分。缺失维度记 None，不补满分/平均分。"""

    liquidity: Optional[float] = None
    low_position: Optional[float] = None
    stabilization: Optional[float] = None
    quality: Optional[float] = None
    quality_available: bool = False
    # 各维度权重来自配置；缺失维度不计入 max_available
    weights: Dict[str, float] = field(
        default_factory=lambda: {
            "liquidity": 25.0,
            "low_position": 30.0,
            "stabilization": 25.0,
            "quality": 20.0,
        }
    )
    notes: List[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        parts = [self.liquidity, self.low_position, self.stabilization, self.quality]
        return round(sum(p for p in parts if p is not None), 2)

    @property
    def max_available(self) -> float:
        """当前数据覆盖下的理论最高分（基本面缺失时为 80）。"""
        total = 0.0
        for key, value in (
            ("liquidity", self.liquidity),
            ("low_position", self.low_position),
            ("stabilization", self.stabilization),
            ("quality", self.quality),
        ):
            if value is None:
                continue
            total += self.weights.get(key, 0.0)
        return round(total, 2)

    def as_dict(self) -> Dict[str, Optional[float]]:
        return {
            "liquidity": _round(self.liquidity),
            "low_position": _round(self.low_position),
            "stabilization": _round(self.stabilization),
            "quality": _round(self.quality),
            "quality_available": self.quality_available,
            "total": self.total,
            "max_available": self.max_available,
            "notes": list(self.notes),
        }


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


@dataclass
class Features:
    """单只股票在某交易日的全部量化特征（阶段 B/C/D 的输入输出）。"""

    symbol: str
    session: date
    close: float
    dollar_volume: float
    avg_dollar_volume_20d: Optional[float] = None
    dollar_volume_ratio_20d: Optional[float] = None
    volume_cv_20d: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    distance_from_low_pct: Optional[float] = None     # (close/low - 1) * 100
    drawdown_from_high_pct: Optional[float] = None    # (1 - close/high) * 100
    change_1m_pct: Optional[float] = None
    change_3m_pct: Optional[float] = None
    change_6m_pct: Optional[float] = None
    ma20: Optional[float] = None
    ma50: Optional[float] = None
    ma200: Optional[float] = None
    vs_ma20_pct: Optional[float] = None
    vs_ma50_pct: Optional[float] = None
    vs_ma200_pct: Optional[float] = None
    rsi14: Optional[float] = None
    rsi14_slope_5d: Optional[float] = None
    higher_low_pct: Optional[float] = None
    new_low_streak: int = 0
    volume_up_down_ratio: Optional[float] = None
    volatility_20d: Optional[float] = None
    volatility_60d: Optional[float] = None
    volatility_ratio: Optional[float] = None
    max_single_day_move_20d: Optional[float] = None
    rel_strength_spy_20d: Optional[float] = None
    rel_strength_spy_60d: Optional[float] = None
    rel_strength_sector_20d: Optional[float] = None
    bars_used: int = 0
    data_stale_days: Optional[int] = None

    def as_row(self) -> Dict[str, object]:
        data = dict(self.__dict__)
        data["session"] = self.session.isoformat()
        return data


@dataclass
class Candidate:
    """入选报告的一只股票。"""

    symbol: str
    name: str
    exchange: str
    session: date
    group: str                     # stabilized | watch
    score: ScoreBreakdown
    features: Features
    reasons: List[str] = field(default_factory=list)
    risk_tags: List[RiskTag] = field(default_factory=list)
    invalidation: List[str] = field(default_factory=list)
    sector: str = ""
    data_sources: List[str] = field(default_factory=list)
    group_reason: str = ""         # 进入观察组/降级的原因（报告里要写清楚）

    @property
    def total_score(self) -> float:
        return self.score.total

    @property
    def high_severity_tags(self) -> List[RiskTag]:
        return [t for t in self.risk_tags if t.severity == "high"]

    def as_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "sector": self.sector,
            "session": self.session.isoformat(),
            "group": self.group,
            "score": self.score.as_dict(),
            "reasons": list(self.reasons),
            "risk_tags": [t.as_dict() for t in self.risk_tags],
            "invalidation": list(self.invalidation),
            "data_sources": list(self.data_sources),
            "group_reason": self.group_reason,
            "features": self.features.as_row(),
        }


@dataclass
class ScanResult:
    """一次每日扫描的完整结果。"""

    session: date
    generated_at: datetime
    market_regime: Dict[str, object] = field(default_factory=dict)
    stabilized: List[Candidate] = field(default_factory=list)
    watch: List[Candidate] = field(default_factory=list)
    #: 没进 A/B 名单的股票：已反弹的、以及一票降级的严重风险股
    flagged: List[Candidate] = field(default_factory=list)
    rejected: List[Dict[str, object]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    universe_size: int = 0
    skipped_reason: Optional[str] = None
    #: 当前买入偏好下哪一组是主名单：stabilized（企稳）或 watch（低位未企稳）
    primary_group: str = "stabilized"
    preference: str = "stabilized"

    @property
    def candidates(self) -> List[Candidate]:
        return list(self.stabilized) + list(self.watch)

    @property
    def primary(self) -> List[Candidate]:
        """主名单（由买入偏好决定）。"""
        return self.watch if self.primary_group == "watch" else self.stabilized

    @property
    def secondary(self) -> List[Candidate]:
        return self.stabilized if self.primary_group == "watch" else self.watch

    def as_dict(self) -> Dict[str, object]:
        return {
            "session": self.session.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "market_regime": self.market_regime,
            "universe_size": self.universe_size,
            "notes": list(self.notes),
            "skipped_reason": self.skipped_reason,
            "primary_group": self.primary_group,
            "preference": self.preference,
            "stabilized": [c.as_dict() for c in self.stabilized],
            "watch": [c.as_dict() for c in self.watch],
            "flagged": [c.as_dict() for c in self.flagged],
            "rejected": list(self.rejected),
        }
