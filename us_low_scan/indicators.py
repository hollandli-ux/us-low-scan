"""技术指标与形态特征。

全部为自实现公式（便于单元测试与逐条追溯），零第三方依赖。
约定：所有函数以"按日期升序排列"的列表为输入，最后一元素 = 最新交易日。

口径说明
--------
* 价格序列必须使用**拆股调整**后的前后一致序列，否则拆股会被误判为暴跌。
* 美元成交额 = close × volume，同样需要与价格序列保持相同调整口径。
* 缺失值一律用 ``None`` 表示，不要用 0 填充（0 会在均线里制造虚假信号）。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

Number = float
MaybeNumber = Optional[float]


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def tail(values: Sequence[float], n: int) -> List[float]:
    """末尾 n 个元素（不足则返回全部）。"""
    if n <= 0:
        return []
    return list(values[-n:])


def sma(values: Sequence[float], window: int) -> MaybeNumber:
    """简单移动平均；数据不足返回 None。"""
    if window <= 0 or len(values) < window:
        return None
    chunk = values[-window:]
    return sum(chunk) / window


def sma_series(values: Sequence[float], window: int) -> List[MaybeNumber]:
    """滚动均线序列，长度与输入一致，前 window-1 个为 None（O(n) 实现）。"""
    out: List[MaybeNumber] = [None] * len(values)
    if window <= 0 or len(values) < window:
        return out
    running = sum(values[:window])
    out[window - 1] = running / window
    for i in range(window, len(values)):
        running += values[i] - values[i - window]
        out[i] = running / window
    return out


def stdev(values: Sequence[float]) -> MaybeNumber:
    """样本标准差（n-1 分母）。"""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def mean(values: Sequence[float]) -> MaybeNumber:
    if not values:
        return None
    return sum(values) / len(values)


def coefficient_of_variation(values: Sequence[float]) -> MaybeNumber:
    """变异系数 = 标准差 / 均值，用来衡量"成交额是否稳定"。"""
    if len(values) < 2:
        return None
    m = mean(values)
    if not m:
        return None
    sd = stdev(values)
    if sd is None:
        return None
    return sd / abs(m)


def percentile_rank(values: Sequence[float], value: float) -> MaybeNumber:
    """``value`` 在 ``values`` 中的分位（0-1）。"""
    if not values:
        return None
    below = sum(1 for v in values if v <= value)
    return below / len(values)


# --------------------------------------------------------------------------
# 价格变化与位置
# --------------------------------------------------------------------------
def percent_change(closes: Sequence[float], lookback: int) -> MaybeNumber:
    """近 ``lookback`` 个交易日的价格变化（小数，-0.34 表示跌 34%）。"""
    if lookback <= 0 or len(closes) < lookback + 1:
        return None
    past = closes[-lookback - 1]
    if past <= 0:
        return None
    return closes[-1] / past - 1.0


def distance_above(low_or_current: float, reference: float) -> MaybeNumber:
    """``(当前价 / 参照价 - 1)``：距 52 周低点的距离。"""
    if reference <= 0:
        return None
    return low_or_current / reference - 1.0


def drawdown_from_high(current: float, high: float) -> MaybeNumber:
    """较最高点的回撤，返回正的幅度（0.35 表示回撤 35%）。"""
    if high <= 0:
        return None
    return 1.0 - current / high


def realized_volatility(closes: Sequence[float], window: int, periods_per_year: int = 252) -> MaybeNumber:
    """年化已实现波动率（对数收益标准差 × sqrt(252)）。"""
    if window <= 1 or len(closes) < window + 1:
        return None
    rets = []
    for i in range(len(closes) - window, len(closes)):
        prev = closes[i - 1]
        if prev <= 0 or closes[i] <= 0:
            return None
        rets.append(math.log(closes[i] / prev))
    sd = stdev(rets)
    if sd is None:
        return None
    return sd * math.sqrt(periods_per_year)


def max_abs_single_day_move(closes: Sequence[float], window: int) -> MaybeNumber:
    """近 ``window`` 个交易日内最大的单日涨跌幅绝对值。"""
    if len(closes) < 2:
        return None
    start = max(1, len(closes) - window)
    worst = 0.0
    found = False
    for i in range(start, len(closes)):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        worst = max(worst, abs(closes[i] / prev - 1.0))
        found = True
    return worst if found else None


def new_low_streak(closes: Sequence[float], window: int) -> int:
    """从最新一天往回数，连续"创 window 日新低"的天数。

    例如返回 3 表示最近 3 个交易日每天的最低价都刷新了之前的 window 日低点。
    """
    streak = 0
    for i in range(len(closes) - 1, window - 1, -1):
        prior_low = min(closes[i - window:i])
        if closes[i] < prior_low:
            streak += 1
        else:
            break
    return streak


def made_new_low_within(closes: Sequence[float], window: int, days: int) -> bool:
    """最近 ``days`` 天内是否曾触及更长窗口的新低。"""
    if len(closes) < window + 1:
        return False
    for i in range(max(window, len(closes) - days), len(closes)):
        if closes[i] <= min(closes[i - window:i]):
            return True
    return False


# --------------------------------------------------------------------------
# RSI（Wilder 平滑）
# --------------------------------------------------------------------------
def rsi_series(closes: Sequence[float], period: int = 14) -> List[MaybeNumber]:
    """Wilder RSI 序列，前 ``period`` 个值为 None。"""
    n = len(closes)
    out: List[MaybeNumber] = [None] * n
    if n < period + 1:
        return out

    gains = []
    losses = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(closes: Sequence[float], period: int = 14) -> MaybeNumber:
    series = rsi_series(closes, period)
    return series[-1] if series else None


def rsi_slope(closes: Sequence[float], period: int = 14, lookback: int = 5) -> MaybeNumber:
    """当前 RSI 减去 ``lookback`` 个交易日前的 RSI（>0 表示动能回升）。"""
    series = rsi_series(closes, period)
    if len(series) < lookback + 1 or series[-1] is None:
        return None
    past = series[-1 - lookback]
    if past is None:
        return None
    return series[-1] - past


# --------------------------------------------------------------------------
# 企稳证据
# --------------------------------------------------------------------------
def higher_low(lows: Sequence[float], recent: int = 5, prior: int = 10) -> MaybeNumber:
    """短期低点是否抬高。

    返回 ``recent`` 日最低价相对"再往前 ``prior`` 日最低价"的变化率。
    大于 0 表示低点抬高（企稳证据之一），小于 0 表示仍在创新低。
    """
    if len(lows) < recent + prior:
        return None
    recent_low = min(lows[-recent:])
    prior_low = min(lows[-recent - prior:-recent])
    if prior_low <= 0:
        return None
    return recent_low / prior_low - 1.0


def up_down_volume_ratio(
    closes: Sequence[float], dollar_volume: Sequence[float], window: int = 10
) -> MaybeNumber:
    """上涨日成交额 / 下跌日成交额（>1 表示买盘更活跃）。

    只统计最近 ``window`` 个交易日，忽略平盘日。
    """
    if len(closes) < 2:
        return None
    start = max(1, len(closes) - window)
    up = down = 0.0
    for i in range(start, len(closes)):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        if closes[i] > prev:
            up += dollar_volume[i]
        elif closes[i] < prev:
            down += dollar_volume[i]
    if down <= 0:
        return None
    return up / down


def close_above_ma(closes: Sequence[float], window: int) -> MaybeNumber:
    """收盘价相对均线的位置：>0 表示站上均线，返回偏离比例。"""
    ma = sma(closes, window)
    if ma is None or ma <= 0:
        return None
    return closes[-1] / ma - 1.0
