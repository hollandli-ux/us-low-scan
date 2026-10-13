"""静态网站导出测试。

用户要的是"能直接双击打开的网站"，所以这里盯三件事：
1. 文件确实生成了；
2. **所有链接都是相对路径且真的存在**（file:// 下没有服务端兜底，死链就是空白页）；
3. 首页内容正确。
"""

import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.support import test_settings

from us_low_scan import db, pipeline, report, site, webapp
from us_low_scan.providers import get_provider


class SiteCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        base = test_settings(cls.tmp)
        cls.settings = replace(
            base,
            data_source=replace(base.data_source, sample_symbols=50, sample_days=320),
        )
        cls.provider = get_provider("sample", cls.settings)
        cls.conn = db.connect(cls.settings.general.database)
        db.init_db(cls.conn)
        # 跑两天的扫描，确保历史页 / 多交易日页面都有内容
        cls.scan = None
        for offset in (5, 0):
            session = pipeline.calendar_us.shift_trading_days(
                pipeline.calendar_us.last_completed_session(), -offset
            )
            result = pipeline.run_scan(cls.settings, session=session, conn=cls.conn,
                                       provider=cls.provider)
            report.write_outputs(result, cls.settings)   # 下载区依赖这些文件
            if offset == 0:
                cls.scan = result
        cls.out_dir = cls.tmp / "site"
        cls.files = site.export_site(cls.settings, conn=cls.conn, out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._tmp.cleanup()

    def _html(self, name: str) -> str:
        return (self.out_dir / name).read_text(encoding="utf-8")


class TestExport(SiteCase):
    def test_core_pages_exist(self):
        for name in ("index.html", "history.html", "lab.html", "outcomes.html"):
            self.assertIn(name, self.files)
            self.assertTrue((self.out_dir / name).exists())

    def test_per_session_pages_exist(self):
        pages = [p.name for p in self.out_dir.glob("session-*.html")]
        self.assertGreaterEqual(len(pages), 2)
        for page in pages:
            self.assertIn(page, self.files)

    def test_symbol_pages_exist(self):
        pages = list(self.out_dir.glob("symbol-*.html"))
        self.assertTrue(pages, "至少应该导出几只股票的个股页面")

    def test_no_broken_or_absolute_links(self):
        """file:// 下没有服务端兜底：绝对路径或死链都会变成空白页。"""
        broken = []
        for page in sorted(self.out_dir.glob("*.html")):
            html = page.read_text(encoding="utf-8")
            for match in re.finditer(r"(?:href|action)='([^']*)'", html):
                url = match.group(1)
                if not url or url.startswith(("http", "#")):
                    continue
                if url.startswith("/"):
                    broken.append((page.name, url, "绝对路径"))
                    continue
                target = url.split("?")[0].split("#")[0]
                if target and not (self.out_dir / target).exists():
                    broken.append((page.name, url, "目标不存在"))
        self.assertEqual(broken, [], f"存在坏链接：{broken[:5]}")

    def test_index_shows_the_primary_list(self):
        html = self._html("index.html")
        self.assertIn("低位观察名单", html)
        self.assertIn("A 组 · 低位观察", html)
        self.assertIn("主名单", html)
        for candidate in self.scan.primary:
            self.assertIn(candidate.symbol, html)

    def test_index_has_no_server_only_controls(self):
        """静态站点里不该出现需要服务端的东西（表单、补跑按钮）。"""
        html = self._html("index.html")
        self.assertNotIn("catchup", html)
        self.assertNotIn("<form class=\"filters\"", html)

    def test_index_links_to_downloadable_reports(self):
        html = self._html("index.html")
        self.assertIn("Markdown 原版报告", html)
        self.assertTrue(list(self.out_dir.glob("*_low_scan.md")))

    def test_history_and_lab_render(self):
        history_html = self._html("history.html")
        self.assertIn("历史名单检索", history_html)
        self.assertIn("反复入选的代码", history_html)

        lab_html = self._html("lab.html")
        self.assertIn("参数实验台", lab_html)
        self.assertIn("当前配置", lab_html)

    def test_outcomes_page_renders(self):
        self.assertIn("表现跟踪", self._html("outcomes.html"))

    def test_export_without_data_raises(self):
        from us_low_scan import db as db_module

        empty = db_module.connect(self.tmp / "empty.db")
        db_module.init_db(empty)
        try:
            with self.assertRaises(RuntimeError):
                site.export_site(self.settings, conn=empty, out_dir=self.tmp / "empty_site")
        finally:
            empty.close()


class TestStaticLinks(unittest.TestCase):
    def test_static_links_are_relative(self):
        links = webapp.StaticLinks()
        self.assertTrue(links.static)
        self.assertEqual(links.home(), "index.html")
        self.assertEqual(links.home("2026-10-09"), "session-2026-10-09.html")
        self.assertEqual(links.symbol("AAPL"), "symbol-AAPL.html")
        self.assertFalse(links.home().startswith("/"))

    def test_plain_links_do_not_link_symbols(self):
        self.assertFalse(webapp.PlainLinks().link_symbols)
        self.assertTrue(webapp.Links().link_symbols)


if __name__ == "__main__":
    unittest.main()
