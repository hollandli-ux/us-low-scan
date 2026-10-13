"""技术指标测试：公式正确性与边界条件。"""

import math
import unittest

from tests.support import ROOT  # noqa: F401  （确保 sys.path 已注入）

from us_low_scan import indicators as ind


class TestBasics(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(ind.sma([1, 2, 3, 4, 5], 5), 3)
        self.assertEqual(ind.sma([1, 2, 3, 4, 5], 2), 4.5)
        self.assertIsNone(ind.sma([1, 2], 3))

    def test_sma_series_matches_sma(self):
        values = [float(x) for x in range(1, 21)]
        series = ind.sma_series(values, 5)
        self.assertIsNone(series[3])
        self.assertAlmostEqual(series[4], 3.0)
        self.assertAlmostEqual(series[-1], ind.sma(values, 5))

    def test_coefficient_of_variation(self):
        self.assertIsNone(ind.coefficient_of_variation([1.0]))
        self.assertAlmostEqual(ind.coefficient_of_variation([2, 2, 2, 2]), 0.0)
        self.assertGreater(ind.coefficient_of_variation([1, 2, 3, 4]), 0.4)

    def test_stdev_uses_sample_denominator(self):
        self.assertAlmostEqual(ind.stdev([2, 4, 4, 4, 5, 5, 7, 9]), 2.13809, places=4)


class TestRsi(unittest.TestCase):
    def test_all_gains_is_100(self):
        closes = [float(x) for x in range(1, 30)]
        self.assertAlmostEqual(ind.rsi(closes, 14), 100.0)

    def test_all_losses_is_zero(self):
        closes = [float(x) for x in range(30, 1, -1)]
        self.assertAlmostEqual(ind.rsi(closes, 14), 0.0)

    def test_wilder_reference_series(self):
        # Wilder 的经典示例，第 15 个交易日的 RSI(14) 约为 70.5
        closes = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
        ]
        value = ind.rsi(closes, 14)
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, 70.5, delta=0.6)

    def test_rsi_series_padding(self):
        closes = [float(x) for x in range(1, 40)]
        series = ind.rsi_series(closes, 14)
        self.assertEqual(len(series), len(closes))
        self.assertIsNone(series[13])
        self.assertIsNotNone(series[14])

    def test_rsi_slope_positive_when_recovering(self):
        closes = [100 - x for x in range(30)] + [71, 72, 73, 74, 75, 76]
        self.assertGreater(ind.rsi_slope(closes, 14, 5), 0)

    def test_flat_series_is_neutral(self):
        self.assertAlmostEqual(ind.rsi([50.0] * 30, 14), 50.0)


class TestPosition(unittest.TestCase):
    def test_percent_change(self):
        closes = [100, 110, 121]
        self.assertAlmostEqual(ind.percent_change(closes, 2), 0.21)
        self.assertIsNone(ind.percent_change([100], 5))

    def test_distance_and_drawdown(self):
        self.assertAlmostEqual(ind.distance_above(110, 100), 0.10)
        self.assertAlmostEqual(ind.drawdown_from_high(75, 100), 0.25)

    def test_realized_volatility(self):
        flat = [100.0] * 30
        self.assertAlmostEqual(ind.realized_volatility(flat, 20), 0.0)
        wild = [100 * math.exp(0.05 * ((-1) ** i)) for i in range(40)]
        self.assertGreater(ind.realized_volatility(wild, 20), 0.5)

    def test_new_low_streak(self):
        # 横盘 20 天后连续 3 天刷新 20 日新低
        closes = [100.0] * 20 + [99.0, 98.0, 97.0]
        self.assertEqual(ind.new_low_streak(closes, 20), 3)
        # 中间有一天没创新低 -> 连续计数在此中断
        closes = [100.0] * 20 + [99.0, 99.5, 98.0, 97.0]
        self.assertEqual(ind.new_low_streak(closes, 20), 2)
        # 反弹后不再创新低
        closes = [100.0] * 20 + [99.0, 98.0, 99.0, 99.5]
        self.assertEqual(ind.new_low_streak(closes, 20), 0)

    def test_higher_low_detects_rising_lows(self):
        lows = [100 - i for i in range(20)] + [85, 86, 87, 88, 89]
        self.assertGreater(ind.higher_low(lows, recent=5, prior=10), 0)
        lows = [100 - i for i in range(20)] + [80, 79, 78, 77, 76]
        self.assertLess(ind.higher_low(lows, recent=5, prior=10), 0)

    def test_close_above_ma(self):
        # 均线 = (19×10 + 11) / 20 = 10.05，收盘价高出 0.0945
        closes = [10.0] * 19 + [11.0]
        self.assertAlmostEqual(ind.close_above_ma(closes, 20), 0.09453, places=4)
        # 低于均线时返回负值
        closes = [10.0] * 19 + [9.0]
        self.assertLess(ind.close_above_ma(closes, 20), 0)


class TestVolumeBehaviour(unittest.TestCase):
    def test_up_down_volume_ratio(self):
        # 6 天里共 5 次比较：上涨 3 次各 5000、下跌 2 次各 1000 -> 15000 / 2000
        closes = [100, 101, 100, 101, 100, 101]
        volumes = [1000, 5000, 1000, 5000, 1000, 5000]
        self.assertAlmostEqual(ind.up_down_volume_ratio(closes, volumes, window=5), 7.5)
        # 涨跌天数相同且成交额相同时为 1.0
        self.assertAlmostEqual(
            ind.up_down_volume_ratio([100, 101, 100], [1000, 1000, 1000], window=2), 1.0
        )
        # 下跌放量 -> 小于 1
        self.assertLess(
            ind.up_down_volume_ratio(closes, [5000, 1000, 5000, 1000, 5000, 1000], window=5),
            1.0,
        )

    def test_up_down_volume_ratio_none_without_down_days(self):
        closes = [1, 2, 3, 4, 5]
        self.assertIsNone(ind.up_down_volume_ratio(closes, [1, 1, 1, 1, 1], window=5))

    def test_max_single_day_move(self):
        closes = [100, 108, 108]
        self.assertAlmostEqual(ind.max_abs_single_day_move(closes, 20), 0.08, places=6)


if __name__ == "__main__":
    unittest.main()
