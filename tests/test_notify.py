"""通知模块测试：内容组装、载荷格式、干跑与失败不致命。"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.support import test_settings

from us_low_scan import notify
from us_low_scan.config import ConfigError, load_settings
from tests.support import CONFIG_PATH
from us_low_scan.models import Candidate, Features, ScanResult, ScoreBreakdown
from datetime import date, datetime


def make_scan(stabilized: int = 2) -> ScanResult:
    def candidate(symbol: str) -> Candidate:
        features = Features(
            symbol=symbol, session=date(2026, 10, 9), close=10.0, dollar_volume=5e7,
            avg_dollar_volume_20d=6e7, distance_from_low_pct=4.0,
            drawdown_from_high_pct=55.0, rsi14=52.0,
        )
        return Candidate(
            symbol=symbol, name=f"{symbol} Inc", exchange="NASDAQ",
            session=date(2026, 10, 9), group="stabilized",
            score=ScoreBreakdown(liquidity=20, low_position=25, stabilization=18),
            features=features, reasons=["流动性充足", "位置偏低"],
        )
    return ScanResult(
        session=date(2026, 10, 9), generated_at=datetime(2026, 10, 10, 6, 0),
        stabilized=[candidate(f"AAA{i}") for i in range(stabilized)],
        market_regime={"status": "整体下行环境"},
    )


class TestMessage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = test_settings(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_subject_and_body(self):
        subject, body = notify.build_message(make_scan(2), self.settings)
        self.assertIn("2026-10-09", subject)
        self.assertIn("主要候选 2 只", subject)
        self.assertIn("A 组 低位企稳（2 只）", body)
        self.assertIn("AAA0", body)
        self.assertIn("不是投资建议", body)

    def test_skip_line_when_no_candidates(self):
        subject, body = notify.build_message(make_scan(0), self.settings)
        self.assertIn("主要候选 0 只", subject)
        self.assertIn("今日无符合条件的主要候选", body)

    def test_reasons_included_when_configured(self):
        settings = replace(
            self.settings, notify=replace(self.settings.notify, include_reasons=True)
        )
        _, body = notify.build_message(make_scan(1), settings)
        self.assertIn("入选理由", body)
        self.assertIn("流动性充足", body)

    def test_notify_threshold(self):
        settings = replace(
            self.settings, notify=replace(self.settings.notify, min_main_candidates=3)
        )
        self.assertFalse(notify.should_notify(make_scan(2), settings))
        self.assertTrue(notify.should_notify(make_scan(3), settings))

    def test_skipped_when_below_threshold(self):
        settings = replace(
            self.settings, notify=replace(self.settings.notify, min_main_candidates=5)
        )
        results = notify.notify(settings, make_scan(2))
        self.assertEqual(results[0]["status"], "skipped")

    def test_force_bypasses_threshold(self):
        settings = replace(
            self.settings,
            notify=replace(self.settings.notify, min_main_candidates=5, channels=["webhook"]),
        )
        results = notify.notify(settings, make_scan(2), dry_run=True, force=True)
        self.assertEqual(results[0]["status"], "dry-run")


class TestPayloads(unittest.TestCase):
    def test_webhook_payload_shapes(self):
        generic = notify.format_webhook_payload("generic", "T", "B")
        self.assertEqual(generic["title"], "T")
        self.assertEqual(generic["text"], "B")

        slack = notify.format_webhook_payload("slack", "T", "B")
        self.assertIn("*T*", slack["text"])

        for kind in ("wecom", "dingtalk"):
            payload = notify.format_webhook_payload(kind, "T", "B")
            self.assertEqual(payload["msgtype"], "text")
            self.assertIn("T", payload["text"]["content"])

        telegram = notify.format_webhook_payload("telegram", "T", "B", "12345")
        self.assertEqual(telegram["chat_id"], "12345")
        self.assertTrue(telegram["disable_web_page_preview"])

    def test_webhook_dry_run_never_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(Path(tmp))
            settings = replace(
                settings,
                notify=replace(
                    settings.notify, webhook_url="https://example.invalid/hook",
                    webhook_kind="slack",
                ),
            )
            result = notify.send_webhook(settings, "T", "B", dry_run=True)
            self.assertEqual(result["status"], "dry-run")
            self.assertEqual(result["kind"], "slack")
            self.assertIn("text", result["payload"])

    def test_email_dry_run_shows_recipients(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(Path(tmp))
            settings = replace(
                settings,
                notify=replace(settings.notify, smtp_host="smtp.example.com",
                               mail_to=["a@example.com"], mail_from="b@example.com"),
            )
            result = notify.send_email(settings, "T", "B", dry_run=True)
            self.assertEqual(result["status"], "dry-run")
            self.assertEqual(result["to"], ["a@example.com"])

    def test_missing_config_skips_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(Path(tmp))     # 默认没有配置 smtp / webhook
            self.assertEqual(notify.send_email(settings, "T", "B")["status"], "skipped")
            self.assertEqual(notify.send_webhook(settings, "T", "B")["status"], "skipped")

    def test_unknown_channel_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(Path(tmp))
            results = notify.notify(settings, make_scan(1), channels=["carrier-pigeon"])
            self.assertEqual(results[0]["status"], "unknown")


class TestNotifyValidation(unittest.TestCase):
    def test_enabled_email_requires_host_and_recipient(self):
        text = Path(CONFIG_PATH).read_text(encoding="utf-8").replace(
            "enabled = false", "enabled = true", 1
        )
        text = text.replace("channels = email", "channels = email")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.ini"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)

    def test_bad_webhook_kind_is_rejected(self):
        text = Path(CONFIG_PATH).read_text(encoding="utf-8")
        text = text.replace("enabled = false", "enabled = true", 1)
        text = text.replace("channels = email", "channels = webhook")
        text = text.replace("webhook_url = ${WEBHOOK_URL:-}", "webhook_url = https://example.com/hook")
        text = text.replace("webhook_kind = generic", "webhook_kind = myspace")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.ini"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()
