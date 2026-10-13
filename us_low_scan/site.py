"""静态网站导出：生成一堆可以直接双击打开的 HTML 文件。

为什么需要它
------------
``serve`` 起的是一个本地服务，必须先跑命令、再开浏览器；关机、关终端就没了。
对"我就想看看今天出了什么"这个需求来说太重。所以每次扫描顺手导出一个静态站点：

    reports/site/index.html          # 打开这个就行
    reports/site/history.html        # 历史名单
    reports/site/lab.html            # 参数方案对比（静态版）
    reports/site/outcomes.html       # 表现统计
    reports/site/symbol-<代码>.html   # 个股时间线

全部是纯 HTML + 内联 CSS，**不依赖任何服务、不联网、没有 JS 请求**，
所以从 ``file://`` 直接打开也能用（链接全是同目录相对路径）。

需要交互（在线筛选、参数滑块、立即补跑）时再用 ``cli serve``。
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import db, history, lab, webapp
from .config import Settings, apply_preset, preset_names


class StaticDashboard(webapp.Dashboard):
    """静态模式下的页面。只覆盖会依赖服务端交互的那一个页面。"""

    def __init__(self, settings: Settings, links=None):
        super().__init__(settings, links=links or webapp.StaticLinks())

    def page_lab(self, query: Dict[str, List[str]] = None) -> Tuple[int, str, str]:
        conn = self._connect()
        try:
            sessions = lab.available_sessions(conn, limit=1)
            if not sessions:
                body = ("<h1>参数实验台</h1><div class='panel empty'>"
                        "数据库里还没有特征快照，先运行 scan 或 demo。</div>")
                return 200, "参数实验台", webapp._page(
                    "参数实验台", body, self.settings, "lab", self.links
                )
            session = sessions[0]
            session_date = lab._date_from(session)
            variants = self._variants()
            rows = []
            for name, variant in variants:
                result = lab.run_lab(conn, variant, session_date)
                if result.get("error"):
                    continue
                diff = result["diff"]
                primary = result.get("primary") or []
                rows.append(
                    (name, result, primary, diff)
                )
        finally:
            conn.close()

        table = "".join(
            "<tr>"
            f"<td class='l'>{webapp._escape(name)}</td>"
            f"<td>{result['universe']}</td>"
            f"<td>{result['after_price_liquidity']}</td>"
            f"<td>{result['after_low_position']}</td>"
            f"<td><b>{len(primary)}</b></td>"
            f"<td class='l'>{'、'.join(webapp._escape(c['symbol']) for c in primary) or '—'}</td>"
            f"<td class='l muted'>"
            + (
                f"+{len(diff['added'])} / -{len(diff['removed'])}"
                if diff.get("has_saved_list") else "（当天还没有正式名单）"
            )
            + "</td>"
            "</tr>"
            for name, result, primary, diff in rows
        )

        best = max(rows, key=lambda r: len(r[2]), default=None)
        detail = ""
        if best:
            name, _result, primary, _diff = best
            detail = (
                f"<h2>最宽松的方案：{webapp._escape(name)}</h2>"
                # 这里的股票不一定进过正式名单，所以不做成跳转链接（避免死链）
                + webapp._candidate_table(primary, links=webapp.PlainLinks())
            )

        body = f"""
<h1>参数实验台</h1>
<div class="sub">用数据库里保存的特征快照重算名单（不需要重新抓行情）。
这里是几个预设方案的静态对比；想拖滑块实时试算，请在本机运行
<code>python3 -m us_low_scan.cli serve</code>。</div>
<div class="wrap"><table><thead><tr>
<th class="l">参数方案</th><th>特征快照</th><th>价格/流动性通过</th><th>低位区间通过</th>
<th>主名单</th><th class="l">主名单代码</th><th class="l">相比正式名单</th>
</tr></thead><tbody>{table}</tbody></table></div>
{detail}
<p class="muted">实验台不会重算财报 / SEC 等外部事件风险（统一显示为未核实），
正式名单请以 scan 的结果为准。</p>
"""
        return 200, "参数实验台", webapp._page(
            "参数实验台", body, self.settings, "lab", self.links
        )

    def _variants(self):
        """要对比的几套参数：默认配置 + 预设 + 两个门槛档位。"""
        out = [("当前配置", self.settings)]
        for name in preset_names(self.settings):
            try:
                out.append((f"预设 {name}", apply_preset(self.settings, name)))
            except Exception:  # 预设写错时跳过，不影响其它方案
                continue
        base = self.settings
        for label, near_low, stab in (("更严格（距低点 ≤8%、企稳 ≥15）", 8.0, 15.0),
                                      ("更宽松（距低点 ≤25%、企稳 ≥8）", 25.0, 8.0)):
            out.append((
                label,
                replace(
                    base,
                    filters=replace(
                        base.filters, near_low_pct=near_low,
                        min_stabilization_score=stab,
                    ),
                ),
            ))
        return out


def export_site(
    settings: Settings,
    conn: Optional[sqlite3.Connection] = None,
    out_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    """把仪表板导出成静态站点，返回写出的文件。"""
    out = Path(out_dir or (Path(settings.general.output_dir) / "site"))
    out.mkdir(parents=True, exist_ok=True)

    own_conn = conn is None
    if own_conn:
        conn = db.connect(settings.general.database)
        db.init_db(conn)

    written: Dict[str, Path] = {}
    try:
        dashboard = StaticDashboard(settings)
        session = history.latest_session(conn)
        if not session:
            raise RuntimeError("数据库里还没有名单，先运行 scan 或 demo")

        pages = {
            "index.html": dashboard.page_today(session)[2],
            "history.html": dashboard.page_history({})[2],
            "outcomes.html": dashboard.page_outcomes()[2],
            "lab.html": dashboard.page_lab({})[2],
        }
        # 每个交易日一个页面，这样"切换交易日"的链接真的能点开（index.html 是最新一期）
        for row in history.sessions_summary(conn, limit=60):
            day = row["session"]
            pages[f"session-{day}.html"] = dashboard.page_today(day)[2]
        for ticker in _tickers(conn):
            pages[f"symbol-{ticker}.html"] = dashboard.page_symbol(ticker)[2]

        download_links = _copy_reports(settings, session, out)

        for name, html in pages.items():
            if name == "index.html" and download_links:
                html = _inject_downloads(html, download_links)
            path = out / name
            path.write_text(html, encoding="utf-8")
            written[name] = path
    finally:
        if own_conn:
            conn.close()
    return written


def _tickers(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM daily_candidates ORDER BY symbol"
    )
    return [r["symbol"] for r in rows]


def _copy_reports(settings: Settings, session: str, out: Path) -> List[Tuple[str, str]]:
    """把当天的 Markdown / CSV 复制到站点目录里，方便直接下载或查看。"""
    source = Path(settings.general.output_dir) / session
    links: List[Tuple[str, str]] = []
    for name, label in (
        ("low_scan.md", "Markdown 原版报告"),
        ("low_scan.csv", "CSV 全量指标"),
        ("low_scan_rejected.csv", "淘汰明细 CSV"),
    ):
        src = source / name
        if not src.exists():
            continue
        target_name = f"{session}_{name}"
        shutil.copyfile(src, out / target_name)
        links.append((target_name, label))
    return links


def _inject_downloads(html: str, links: List[Tuple[str, str]]) -> str:
    items = "　".join(f"<a href='{name}'>{label}</a>" for name, label in links)
    block = f"<div class='panel'><b>下载</b>　{items}</div>"
    marker = "</main>"
    return html.replace(marker, block + marker, 1)
