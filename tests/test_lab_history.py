"""参数实验台与历史检索测试。"""

import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from tests.support import test_settings

from us_low_scan import calendar_us, db, history, lab, pipeline
from us_low_scan.providers import get_provider


class CaseBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        base = test_settings(self.tmp)
        self.settings = replace(
            base, data_source=replace(base.data_source, sample_symbols=60, sample_days=320)
        )
        self.provider = get_provider("sample", self.settings)
        self.conn = db.connect(self.settings.general.database)
        db.init_db(self.conn)
        self.session = calendar_us.last_completed_session()
        self.scan = pipeline.run_scan(
            self.settings, session=self.session, conn=self.conn, provider=self.provider
        )

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


class TestLab(CaseBase):
    def test_default_params_reproduce_the_official_list(self):
        """实验台用同一套参数跑，结果必须与正式名单完全一致。"""
        result = lab.run_lab(self.conn, self.settings, self.session)
        self.assertEqual(
            [c["symbol"] for c in result["stabilized"]],
            [c.symbol for c in self.scan.stabilized],
        )
        self.assertEqual(
            [c["symbol"] for c in result["watch"]],
            [c.symbol for c in self.scan.watch],
        )
        self.assertEqual(result["diff"]["added"], [])
        self.assertEqual(result["diff"]["removed"], [])

    def test_loosening_near_low_admits_more_candidates(self):
        params = lab.params_from_settings(self.settings)
        strict = lab.run_lab(self.conn, self.settings, self.session, params)
        loose_params = lab.coerce_params(params, {"near_low_pct": 40, "min_stabilization_score": 4})
        loose = lab.run_lab(self.conn, self.settings, self.session, loose_params)
        self.assertGreater(loose["after_low_position"], strict["after_low_position"])

    def test_weight_change_keeps_sub_scores_consistent(self):
        params = lab.coerce_params(
            lab.params_from_settings(self.settings),
            {"weight_stabilization": 40, "weight_low_position": 15},
        )
        settings = lab.build_settings(self.settings, params)
        scoring = settings.scoring
        self.assertAlmostEqual(
            scoring.stab_rsi_share + scoring.stab_higher_low_share
            + scoring.stab_ma_share + scoring.stab_volume_share,
            scoring.weight_stabilization, places=3,
        )

    def test_coerce_ignores_garbage(self):
        base = lab.params_from_settings(self.settings)
        coerced = lab.coerce_params(
            base, {"near_low_pct": "abc", "unknown_key": "5", "min_price": "7.5"}
        )
        self.assertEqual(coerced.near_low_pct, base.near_low_pct)
        self.assertEqual(coerced.min_price, 7.5)

    def test_missing_snapshot_is_reported(self):
        result = lab.run_lab(self.conn, self.settings, date(2000, 1, 3))
        self.assertIn("error", result)


class TestHistory(CaseBase):
    def test_sessions_summary_counts_groups(self):
        rows = history.sessions_summary(self.conn)
        self.assertEqual(rows[0]["session"], self.session.isoformat())
        self.assertEqual(
            rows[0]["stabilized"] + rows[0]["watch"], rows[0]["total"]
        )
        self.assertGreater(rows[0]["stabilized"], 0)

    def test_search_by_ticker_and_group(self):
        symbol = self.scan.stabilized[0].symbol
        rows = history.search_candidates(self.conn, ticker=symbol)
        self.assertTrue(rows)
        self.assertTrue(all(r["symbol"] == symbol for r in rows))
        self.assertEqual(rows[0]["group"], "stabilized")

        watch_rows = history.search_candidates(self.conn, group="watch")
        self.assertTrue(watch_rows)
        self.assertTrue(all(r["group"] == "watch" for r in watch_rows))

    def test_search_by_date_range_and_score(self):
        rows = history.search_candidates(
            self.conn, start=self.session, end=self.session, min_score=0
        )
        self.assertEqual(len(rows), len(self.scan.candidates))
        high = history.search_candidates(self.conn, min_score=99)
        self.assertEqual(high, [])

    def test_symbol_timeline(self):
        symbol = self.scan.stabilized[0].symbol
        data = history.symbol_timeline(self.conn, symbol)
        self.assertEqual(data["summary"]["symbol"], symbol)
        self.assertEqual(data["summary"]["appearances"], 1)
        self.assertEqual(data["history"][0]["group_label"], "低位企稳组")

    def test_session_detail_contains_full_payload(self):
        rows = history.session_candidates_detail(self.conn, self.session.isoformat())
        self.assertEqual(len(rows), len(self.scan.candidates))
        first = rows[0]
        self.assertTrue(first["reasons"])
        self.assertTrue(first["invalidation"])
        self.assertIsNotNone(first["features"].get("rsi14"))

    def test_top_symbols(self):
        rows = history.top_symbols(self.conn, limit=5)
        self.assertTrue(rows)
        self.assertGreaterEqual(rows[0]["appearances"], 1)


if __name__ == "__main__":
    unittest.main()
