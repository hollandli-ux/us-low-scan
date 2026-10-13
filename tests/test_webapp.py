"""网页仪表板测试：页面渲染、JSON 接口、HTTP 层。

沙箱里可能禁止监听端口，此时 HTTP 层用例会自动跳过，页面渲染用例照常运行。
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from tests.support import test_settings

from us_low_scan import calendar_us, db, pipeline, webapp
from us_low_scan.providers import get_provider


class DashboardCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        base = test_settings(cls.tmp)
        cls.settings = replace(
            base, data_source=replace(base.data_source, sample_symbols=50, sample_days=320)
        )
        cls.provider = get_provider("sample", cls.settings)
        cls.conn = db.connect(cls.settings.general.database)
        db.init_db(cls.conn)
        cls.session = calendar_us.last_completed_session()
        cls.scan = pipeline.run_scan(
            cls.settings, session=cls.session, conn=cls.conn, provider=cls.provider
        )
        cls.dashboard = webapp.Dashboard(cls.settings)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._tmp.cleanup()


class TestPages(DashboardCase):
    def test_today_page(self):
        status, title, html = self.dashboard.page_today()
        self.assertEqual(status, 200)
        self.assertIn("低位观察名单", html)
        self.assertIn("A 组", html)
        self.assertIn("B 组", html)
        self.assertIn(self.scan.stabilized[0].symbol, html)
        self.assertIn("不是投资建议", html)

    def test_today_page_specific_session(self):
        status, _, html = self.dashboard.page_today(self.session.isoformat())
        self.assertEqual(status, 200)
        self.assertIn(self.session.isoformat(), html)

    def test_history_page_with_filters(self):
        status, _, html = self.dashboard.page_history(
            {"ticker": [self.scan.stabilized[0].symbol], "group": ["stabilized"]}
        )
        self.assertEqual(status, 200)
        self.assertIn("历史名单检索", html)
        self.assertIn(self.scan.stabilized[0].symbol, html)

    def test_symbol_page(self):
        status, _, html = self.dashboard.page_symbol(self.scan.stabilized[0].symbol)
        self.assertEqual(status, 200)
        self.assertIn("入选", html)
        self.assertIn("最大不利", html)

    def test_symbol_page_unknown_ticker(self):
        status, _, html = self.dashboard.page_symbol("NOPE")
        self.assertEqual(status, 200)
        self.assertIn("没有这只股票的历史入选记录", html)

    def test_outcomes_page(self):
        status, _, html = self.dashboard.page_outcomes()
        self.assertEqual(status, 200)
        self.assertIn("表现跟踪", html)

    def test_lab_page_renders_and_reranks(self):
        status, _, html = self.dashboard.page_lab({"near_low_pct": ["30"]})
        self.assertEqual(status, 200)
        self.assertIn("参数实验台", html)
        self.assertIn("特征快照", html)

    def test_lab_page_handles_garbage_input(self):
        status, _, html = self.dashboard.page_lab(
            {"near_low_pct": ["abc"], "session": ["not-a-date"]}
        )
        # 非法日期不应该抛异常到调用方
        self.assertIn(status, (200, 500))

    def test_pages_have_balanced_tags(self):
        from html.parser import HTMLParser

        void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                "meta", "source", "track", "wbr"}

        class Check(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack = []
                self.errors = []

            def handle_starttag(self, tag, attrs):
                if tag not in void:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()
                elif tag in self.stack:
                    while self.stack and self.stack[-1] != tag:
                        self.errors.append(self.stack.pop())
                    if self.stack:
                        self.stack.pop()
                else:
                    self.errors.append(tag)

        for _, _, html in (
            self.dashboard.page_today(),
            self.dashboard.page_history({}),
            self.dashboard.page_symbol(self.scan.stabilized[0].symbol),
            self.dashboard.page_outcomes(),
            self.dashboard.page_lab({}),
        ):
            checker = Check()
            checker.feed(html)
            self.assertEqual(checker.stack, [], "存在未闭合标签")
            self.assertEqual(checker.errors, [], "存在不匹配的标签")


class TestApi(DashboardCase):
    def test_api_sessions(self):
        status, payload = self.dashboard.api("/api/sessions", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload[0]["session"], self.session.isoformat())

    def test_api_latest(self):
        status, payload = self.dashboard.api("/api/latest", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["candidates"]), len(self.scan.candidates))

    def test_api_session_and_history(self):
        status, payload = self.dashboard.api(
            f"/api/session/{self.session.isoformat()}", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["candidates"]), len(self.scan.candidates))

        status, payload = self.dashboard.api("/api/history", {"limit": ["5"]})
        self.assertEqual(status, 200)
        self.assertLessEqual(len(payload), 5)

    def test_api_symbol_and_outcomes(self):
        status, payload = self.dashboard.api(
            f"/api/symbol/{self.scan.stabilized[0].symbol}", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["appearances"], 1)

        status, payload = self.dashboard.api("/api/outcomes", {})
        self.assertEqual(status, 200)
        self.assertIn("all", payload)
        self.assertIn("stabilized", payload)

    def test_api_lab(self):
        status, payload = self.dashboard.api(
            "/api/lab", {"session": [self.session.isoformat()], "near_low_pct": ["30"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["session"], self.session.isoformat())
        self.assertEqual(payload["params"]["near_low_pct"], 30.0)

    def test_unknown_api_returns_404(self):
        status, payload = self.dashboard.api("/api/nope", {})
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_api_is_json_serialisable(self):
        status, payload = self.dashboard.api("/api/latest", {})
        json.dumps(payload, default=str)  # 不抛异常即通过


class TestHttpLayer(DashboardCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = None
        try:
            cls.server = webapp.create_server(cls.settings, "127.0.0.1", 0)
        except (PermissionError, OSError):
            cls.server = None
            return
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.shutdown()
            cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        if self.server is None:
            self.skipTest("当前环境不允许监听本地端口（沙箱限制）")

    def _get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_pages_over_http(self):
        for path, needle in (
            ("/", "低位观察名单"),
            ("/history", "历史名单检索"),
            ("/outcomes", "表现跟踪"),
            ("/lab", "参数实验台"),
            (f"/symbol/{self.scan.stabilized[0].symbol}", "入选"),
            ("/healthz", "ok"),
        ):
            status, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(needle, body, path)

    def test_api_over_http(self):
        status, body = self._get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), list)

    def test_download_original_report(self):
        status, body = self._get(f"/file/{self.session.isoformat()}/low_scan.md")
        self.assertEqual(status, 200)
        self.assertIn("美股低位观察名单", body)

    def test_path_traversal_is_blocked(self):
        try:
            status, body = self._get("/file/../../../../etc/passwd")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, ""
        self.assertIn(status, (400, 404))
        self.assertNotIn("root:", body)


if __name__ == "__main__":
    unittest.main()
