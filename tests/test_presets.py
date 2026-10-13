"""参数方案（presets）测试。"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.support import CONFIG_PATH, test_settings

from us_low_scan.config import ConfigError, apply_preset, load_settings, preset_names


class TestPresets(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(CONFIG_PATH)

    def test_presets_are_discovered(self):
        self.assertEqual(preset_names(self.settings), ["aggressive", "conservative"])

    def test_no_preset_returns_same_settings(self):
        self.assertIs(apply_preset(self.settings, ""), self.settings)
        self.assertIs(apply_preset(self.settings, "default"), self.settings)

    def test_conservative_tightens_thresholds(self):
        strict = apply_preset(self.settings, "conservative")
        self.assertLess(strict.filters.near_low_pct, self.settings.filters.near_low_pct)
        self.assertGreater(
            strict.filters.min_stabilization_score,
            self.settings.filters.min_stabilization_score,
        )
        self.assertGreater(
            strict.universe.min_avg_dollar_volume_20d,
            self.settings.universe.min_avg_dollar_volume_20d,
        )
        self.assertLess(strict.output.max_main_candidates,
                        self.settings.output.max_main_candidates)

    def test_aggressive_loosens_thresholds(self):
        loose = apply_preset(self.settings, "aggressive")
        self.assertGreater(loose.filters.near_low_pct, self.settings.filters.near_low_pct)
        self.assertLess(
            loose.filters.min_stabilization_score,
            self.settings.filters.min_stabilization_score,
        )

    def test_weights_stay_consistent_after_scaling(self):
        """改维度权重后，分项之和必须仍然等于维度权重（否则配置校验会失败）。"""
        for name in preset_names(self.settings):
            preset = apply_preset(self.settings, name)
            scoring = preset.scoring
            self.assertAlmostEqual(
                scoring.liquidity_volume_share + scoring.liquidity_stability_share,
                scoring.weight_liquidity, places=3,
            )
            self.assertAlmostEqual(
                scoring.low_near_low_share + scoring.low_drawdown_share
                + scoring.low_decline_share,
                scoring.weight_low_position, places=3,
            )
            self.assertAlmostEqual(
                scoring.stab_rsi_share + scoring.stab_higher_low_share
                + scoring.stab_ma_share + scoring.stab_volume_share,
                scoring.weight_stabilization, places=3,
            )
            self.assertEqual(preset.scoring.total_weight, 100.0)

    def test_unknown_preset_raises(self):
        with self.assertRaises(ConfigError):
            apply_preset(self.settings, "does-not-exist")

    def test_typo_key_is_rejected_at_load_time(self):
        text = Path(CONFIG_PATH).read_text(encoding="utf-8") + (
            "\n[preset.typo]\nnear_low_pctt = 10\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.ini"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
