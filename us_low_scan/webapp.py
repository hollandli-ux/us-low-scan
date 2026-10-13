"""网页仪表板（阶段 4）：只用标准库 ``http.server``，无需前端构建。

页面
----
``/``                   最新一期的每日名单
``/session/<YYYY-MM-DD>``  指定交易日
``/history``            历史名单检索（代码 / 分组 / 日期区间 / 最低分）
``/symbol/<TICKER>``    单只股票：什么时候被选过、后来表现如何
``/outcomes``           候选股入选后 5/10/20 个交易日的表现统计
``/lab``                参数实验台：调门槛与权重，立刻看名单变化
``/api/*``              JSON 接口，方便自己接脚本
``/file/<日期>/<文件名>``  下载 Markdown / CSV / JSON 原始报告

启动：``python3 -m us_low_scan.cli serve --port 8765``
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import db, history, lab, outcomes, pipeline, report

# --------------------------------------------------------------------------
# 样式：一套轻量、偏数据工具的视觉，浅色/深色跟随系统
# --------------------------------------------------------------------------
CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --line:#e3e6ea; --ink:#1b1f24; --muted:#6b7280;
  --accent:#1f6feb; --up:#0f7b3f; --down:#c0392b; --warn:#b7791f; --chip:#eef2f7;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#0f1216; --panel:#161a20; --line:#252b33; --ink:#e6e9ee; --muted:#9aa4b2;
         --accent:#6ea8fe; --up:#4ade80; --down:#f87171; --warn:#fbbf24; --chip:#1f2630; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC","PingFang SC","Hiragino Sans GB",sans-serif;
  font-variant-numeric:tabular-nums}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:9;background:var(--panel);border-bottom:1px solid var(--line);
  padding:10px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header .brand{font-weight:650;letter-spacing:.2px}
header nav{display:flex;gap:14px;font-size:13px}
header .spacer{flex:1}
header .stamp{color:var(--muted);font-size:12px}
main{max-width:1180px;margin:0 auto;padding:20px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:26px 0 10px;letter-spacing:.3px;color:var(--muted);
  text-transform:uppercase;font-weight:600}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat .k{color:var(--muted);font-size:12px}
.stat .v{font-size:19px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px;background:var(--panel);
  border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child,th.l,td.l{text-align:left}
thead th{background:var(--chip);color:var(--muted);font-weight:600;font-size:12px}
tbody tr:hover{background:var(--chip)}
tbody tr:last-child td{border-bottom:none}
.wrap{overflow-x:auto;border-radius:10px}
.num{font-variant-numeric:tabular-nums}
.up{color:var(--up)} .down{color:var(--down)} .muted{color:var(--muted)}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--chip);
  font-size:12px;color:var(--muted);margin-right:6px}
.chip.high{background:rgba(192,57,43,.14);color:var(--down)}
.chip.warn{background:rgba(183,121,31,.16);color:var(--warn)}
.chip.ok{background:rgba(15,123,63,.14);color:var(--up)}
.bar{height:6px;border-radius:4px;background:var(--chip);position:relative;min-width:70px}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:4px;background:var(--accent)}
form.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:4px}
input,select{padding:6px 8px;border:1px solid var(--line);border-radius:7px;background:var(--panel);
  color:var(--ink);font-size:13px;min-width:110px}
button{padding:7px 14px;border:0;border-radius:7px;background:var(--accent);color:#fff;
  font-size:13px;cursor:pointer;font-weight:600}
.detail{margin:10px 0 22px}
.detail h3{margin:0 0 8px;font-size:15px}
.kv{display:grid;grid-template-columns:110px 1fr;gap:2px 14px;font-size:13px}
.kv .k{color:var(--muted)}
ol.reasons{margin:8px 0 0 18px;padding:0}
ol.reasons li{margin:3px 0}
footer{max-width:1180px;margin:30px auto 50px;padding:0 20px;color:var(--muted);font-size:12px}
.empty{padding:28px;text-align:center;color:var(--muted)}
"""


def _fmt(value, digits: int = 1, percent: bool = False, signed: bool = False) -> str:
    if value is None:
        return '<span class="muted">未核实</span>'
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if percent:
        return f"{number:+.{digits}f}%" if signed else f"{number:.{digits}f}%"
    return f"{number:,.{digits}f}"


def _money(value) -> str:
    if value is None:
        return '<span class="muted">未核实</span>'
    value = float(value)
    if abs(value) >= 1e9:
        return f"US${value/1e9:.2f}B"
    if abs(value) >= 1e6:
        return f"US${value/1e6:.0f}M"
    return f"US${value/1e3:.0f}K"


def _pct_class(value) -> str:
    if value is None:
        return ""
    return "up" if float(value) > 0 else ("down" if float(value) < 0 else "")


def _chip_class(severity: str) -> str:
    return {"high": "chip high", "warn": "chip warn"}.get(severity, "chip")


def _safe_date(text: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat((text or "").strip())
    except (TypeError, ValueError):
        return None


def _escape(text) -> str:
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------------
# 链接：同一套渲染代码要同时服务两种场景
#   Links       —— 本地 http 服务（绝对路径）
#   StaticLinks —— 导出的静态站点（同目录相对文件名，双击 index.html 就能看）
# --------------------------------------------------------------------------
class Links:
    static = False
    link_symbols = True

    def home(self, session: Optional[str] = None) -> str:
        return "/" + (f"?session={urllib.parse.quote(session)}" if session else "")

    def history(self, query: str = "") -> str:
        return "/history" + (f"?{query}" if query else "")

    def lab(self) -> str:
        return "/lab"

    def outcomes(self) -> str:
        return "/outcomes"

    def symbol(self, ticker: str) -> str:
        return f"/symbol/{urllib.parse.quote(str(ticker))}"

    def catchup_action(self) -> str:
        return "/catchup"

    def report_file(self, session: str, name: str) -> str:
        return f"/file/{session}/{name}"


class PlainLinks(Links):
    """不需要跳转的场合：股票代码只显示文字，不做成链接。"""

    link_symbols = False


class StaticLinks(Links):
    """静态导出：所有页面平铺在同一个目录里，链接全是相对文件名。"""

    static = True

    def home(self, session: Optional[str] = None) -> str:
        return "index.html" if not session else f"session-{session}.html"

    def history(self, query: str = "") -> str:
        return "history.html"

    def lab(self) -> str:
        return "lab.html"

    def outcomes(self) -> str:
        return "outcomes.html"

    def symbol(self, ticker: str) -> str:
        return f"symbol-{urllib.parse.quote(str(ticker))}.html"

    def catchup_action(self) -> str:
        return ""

    def report_file(self, session: str, name: str) -> str:
        return f"{session}_{name}"


# --------------------------------------------------------------------------
# 页面片段
# --------------------------------------------------------------------------
def _demo_banner(settings) -> str:
    """用合成数据时，页面上必须有拦不住的一条横幅 —— 那些代码在现实里不存在。"""
    if settings.data_source.provider != "sample":
        return ""
    return (
        "<div style=\"background:#c0392b;color:#fff;padding:10px 20px;"
        "font-weight:600;text-align:center;font-size:13px\">"
        "⚠️ 演示数据（离线合成）—— 下面的股票代码与公司名都是程序生成的，"
        "**现实中不存在**，不能用于任何投资判断。要看真实股票："
        "<code style=\"background:rgba(0,0,0,.25);padding:1px 6px;border-radius:4px\">"
        "python3 -m us_low_scan.cli --provider nasdaq --top 150 scan</code>"
        "</div>"
    )


def _page(title: str, body: str, settings, active: str = "", links=None) -> str:
    links = links or Links()

    def nav(label: str, href: str, key: str) -> str:
        mark = " style='font-weight:700'" if key == active else ""
        return f"<a href='{href}'{mark}>{label}</a>"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(title)}</title><style>{CSS}</style></head>
<body>
<header>
  <span class="brand">美股低位观察工具</span>
  <nav>
    {nav("今日名单", links.home(), "today")}
    {nav("历史检索", links.history(), "history")}
    {nav("参数实验台", links.lab(), "lab")}
  </nav>
  <span class="spacer"></span>
  <span class="stamp">数据源 {_escape(settings.data_source.provider)}</span>
</header>
{_demo_banner(settings)}<main>{body}</main>
<footer>观察名单，不是投资建议，也不代表上涨概率。
「低位」与「企稳证据」都可能失败，请自行判断并承担风险。</footer>
</body></html>"""


def _candidate_table(rows: List[Dict[str, object]], show_reason: bool = False, links=None) -> str:
    links = links or Links()
    if not rows:
        return '<div class="panel empty">暂无记录。</div>'
    head = ["代码", "名称", "收盘", "距 52 周低点", "较高点回撤", "近 1 月", "vs 50 日线",
            "20 日均额", "RSI", "总分"]
    if show_reason:
        head.append("未企稳原因")
    html = ['<div class="wrap"><table><thead><tr>']
    html.append("<th class='l'>" + "</th><th class='l'>".join(head) + "</th></tr></thead><tbody>")
    for row in rows:
        features = row.get("features") or {}

        def value(key: str):
            """DB 行把指标放在顶层，实验台的结果放在 features 里，两边都要能读。"""
            if row.get(key) is not None:
                return row.get(key)
            return features.get(key)

        close = row.get("close")
        rsi = value("rsi14")
        score = row.get("total_score") or (row.get("score") or {}).get("total") or 0
        max_score = row.get("max_score") or (row.get("score") or {}).get("max_available") or 80
        width = max(2, min(100, float(score) / float(max_score or 80) * 100))
        symbol = _escape(row.get("symbol"))
        html.append(
            "<tr>"
            f"<td class='l'>"
            + (f"<a href='{links.symbol(row.get('symbol'))}'>{symbol}</a>"
               if links.link_symbols else symbol)
            + "</td>"
            f"<td class='l'>{_escape(row.get('name'))}</td>"
            f"<td>{_fmt(close, 2)}</td>"
            f"<td>{_fmt(value('distance_from_low_pct'), 1, percent=True, signed=True)}</td>"
            f"<td class='down'>"
            f"{_fmt(-abs(value('drawdown_from_high_pct')), 1, percent=True, signed=True) if value('drawdown_from_high_pct') is not None else '未核实'}"
            f"</td>"
            f"<td class='{_pct_class(value('change_1m_pct'))}'>"
            f"{_fmt(value('change_1m_pct'), 1, percent=True, signed=True)}</td>"
            f"<td>{_fmt(value('vs_ma50_pct'), 1, percent=True, signed=True)}</td>"
            f"<td>{_money(value('avg_dollar_volume_20d'))}</td>"
            f"<td>{_fmt(rsi, 1)}</td>"
            f"<td><div class='bar' title='{float(score):.1f}/{float(max_score):.0f}'>"
            f"<i style='width:{width:.0f}%'></i></div>"
            f"<span class='muted'>{float(score):.1f}/{float(max_score):.0f}</span></td>"
        )
        if show_reason:
            html.append(f"<td class='l muted'>{_escape(row.get('group_reason'))}</td>")
        html.append("</tr>")
    html.append("</tbody></table></div>")
    return "".join(html)


def _candidate_detail(row: Dict[str, object], settings, links=None) -> str:
    links = links or Links()
    features = row.get("features") or {}
    score = row.get("score") or {}
    reasons = row.get("reasons") or []
    tags = row.get("risk_tags") or []
    invalidation = row.get("invalidation") or []

    weights = score.get("weights") or {}
    parts = [
        ("流动性", score.get("liquidity"), weights.get("liquidity", settings.scoring.weight_liquidity)),
        ("低位程度", score.get("low_position"), weights.get("low_position", settings.scoring.weight_low_position)),
        ("企稳证据", score.get("stabilization"), weights.get("stabilization", settings.scoring.weight_stabilization)),
        ("公司质量", score.get("quality"), weights.get("quality", settings.scoring.weight_quality)),
    ]
    score_html = " ".join(
        f"<span class='chip'>{label} {value:.1f}/{weight:.0f}</span>"
        if isinstance(value, (int, float))
        else f"<span class='chip'>质量 未覆盖（0/{weight:.0f}，不补分）</span>"
        for label, value, weight in parts
    )
    tag_html = "".join(
        f"<span class='{_chip_class(t.get('severity', 'info'))}' title='{_escape(t.get('detail'))}'>"
        f"{_escape(t.get('label'))}</span>"
        for t in tags
    )
    reasons_html = "".join(f"<li>{_escape(r)}</li>" for r in reasons)
    invalidation_html = "".join(f"<li>{_escape(i)}</li>" for i in invalidation)
    return f"""<div class="panel detail">
  <h3>{f"<a href='{links.symbol(row.get('symbol'))}'>{_escape(row.get('symbol'))}</a>" if links.link_symbols else _escape(row.get('symbol'))}
    　{_escape(row.get('name'))}　<span class="muted">{_escape(row.get('exchange'))}
    {'· ' + _escape(row.get('sector')) if row.get('sector') else ''}</span></h3>
  <div>{score_html}</div>
  <div class="kv" style="margin-top:10px">
    <span class="k">最新行情</span><span>收盘 {_fmt(row.get('close'), 2)}
      · 交易日 {_escape(row.get('session'))} · 来源 {_escape(', '.join(row.get('data_sources') or []))}</span>
    <span class="k">52 周区间</span><span>{_fmt(features.get('low_52w'), 2)} – {_fmt(features.get('high_52w'), 2)}</span>
    <span class="k">动能</span><span>RSI(14) {_fmt(features.get('rsi14'), 1)}
      · 5 日 {_fmt(features.get('rsi14_slope_5d'), 1, signed=True)}
      · 20 日均线 {_fmt(features.get('vs_ma20_pct'), 1, percent=True, signed=True)}
      · 50 日均线 {_fmt(features.get('vs_ma50_pct'), 1, percent=True, signed=True)}</span>
    <span class="k">价格变化</span><span class="{_pct_class(features.get('change_6m_pct'))}">
      1 月 {_fmt(features.get('change_1m_pct'), 1, percent=True, signed=True)}
      · 3 月 {_fmt(features.get('change_3m_pct'), 1, percent=True, signed=True)}
      · 6 月 {_fmt(features.get('change_6m_pct'), 1, percent=True, signed=True)}</span>
    <span class="k">相对强弱</span><span>vs SPY {_fmt(features.get('rel_strength_spy_20d'), 1, percent=True, signed=True)}</span>
  </div>
  <div style="margin-top:10px">{tag_html}</div>
  <ol class="reasons">{reasons_html}</ol>
  <details style="margin-top:8px"><summary class="muted">退出观察条件（{len(invalidation)} 条）</summary>
    <ul class="reasons">{invalidation_html}</ul></details>
</div>"""


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
class Dashboard:
    """把 HTTP 请求翻译成页面/JSON。与 HTTP 层分离，便于直接单元测试。"""

    def __init__(self, settings, links=None):
        self.settings = settings
        self.links = links or Links()

    def _connect(self) -> sqlite3.Connection:
        conn = db.connect(self.settings.general.database)
        db.init_db(conn)
        return conn

    def _saved_scan(self, session: str) -> Dict[str, object]:
        """读当天保存的 low_scan.json（拿偏好与"已排除"名单，DB 里没存这些）。"""
        path = Path(self.settings.general.output_dir) / session / "low_scan.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def freshness(self) -> Dict[str, object]:
        """数据新到哪一天、还差几天没跑。"""
        conn = self._connect()
        try:
            sessions = db.list_candidate_sessions(conn, limit=1)
            pending = pipeline.pending_sessions(conn, self.settings)
        finally:
            conn.close()
        return {
            "last_session": sessions[0] if sessions else None,
            "pending": [d.isoformat() for d in pending],
        }

    def run_catch_up(self) -> Dict[str, object]:
        """在网页上点一下就把错过的交易日补齐。"""
        conn = self._connect()
        try:
            results = pipeline.catch_up(
                self.settings, conn=conn,
                provider=None,
            )
            for item in results:
                report.write_outputs(item, self.settings)
        finally:
            conn.close()
        return {
            "ran": [r.session.isoformat() for r in results],
            "count": len(results),
        }

    # -- 页面 ------------------------------------------------------------
    def page_today(self, session: Optional[str] = None) -> Tuple[int, str, str]:
        conn = self._connect()
        try:
            session = session or history.latest_session(conn)
            if not session:
                body = ('<h1>还没有任何名单</h1><div class="panel">先运行 '
                        '<code>python3 -m us_low_scan.cli demo</code> 或 <code>scan</code> '
                        '生成第一份名单。</div>')
                return 200, "美股低位观察", _page("美股低位观察", body, self.settings, "today", self.links)
            rows = history.session_candidates_detail(conn, session)
            sessions = history.sessions_summary(conn, limit=40)
            pending = pipeline.pending_sessions(conn, self.settings)
        finally:
            conn.close()

        saved = self._saved_scan(session)
        preference = saved.get("preference") or self.settings.filters.dip_preference
        primary_group = saved.get("primary_group") or (
            "watch" if preference == "dip" else "stabilized"
        )
        by_group = {"stabilized": [], "watch": []}
        for row in rows:
            by_group.get(row.get("group"), by_group["watch"]).append(row)
        primary = by_group["stabilized"] if primary_group == "stabilized" else by_group["watch"]
        secondary = by_group["watch"] if primary_group == "stabilized" else by_group["stabilized"]
        flagged = saved.get("flagged") or []

        options = "".join(
            f"<option value='{s['session']}'{' selected' if s['session'] == session else ''}>"
            f"{s['session']}（主名单 {s['stabilized']} 只）</option>"
            for s in sessions
        )
        # 静态导出时 <form> / <select> 不起作用，改成一组链接
        switcher = (
            "<p class='sub'>历史交易日："
            + "　".join(
                f"<a href='{self.links.home(s['session'])}'>{s['session']}</a>"
                for s in sessions[:12]
            )
            + "</p>"
            if self.links.static
            else f"""<form class="filters" method="get" action="{self.links.home()}">
  <label>切换交易日<select name="session" onchange="this.form.submit()">{options}</select></label>
  <noscript><button type="submit">查看</button></noscript>
</form>"""
        )

        if pending:
            banner = (
                '<div class="panel" style="border-color:var(--warn)">'
                f"⚠️ 数据最新到 <b>{session}</b>，还有 <b>{len(pending)}</b> 个交易日没出名单"
                f"（{pending[0].isoformat()} ~ {pending[-1].isoformat()}）。"
                + (
                    f"<form method='post' action='{self.links.catchup_action()}'"
                    " style='display:inline;margin-left:10px'>"
                    "<button type='submit'>立即补跑</button></form>"
                    if not self.links.static else ""
                )
                + "</div>"
            )
        else:
            banner = f"<div class='sub'>数据已是最新（{session}）。</div>"

        if preference == "dip":
            pref_note = (
                "当前偏好：<b>买跌</b>（dip_preference = dip）。主名单是"
                "「位置够低、<b>且还没涨起来</b>」的股票；已经站上均线、RSI 回到 50 以上的"
                "放在 B 组仅供参考。"
            )
        else:
            pref_note = (
                "当前偏好：<b>企稳</b>（dip_preference = stabilized）。主名单是已出现止跌信号的"
                "股票 —— 这类股票其实<b>已经在涨</b>。想买还在跌的，把配置改成 dip。"
            )

        primary_title = "A 组 · 低位观察（还没涨起来）" if primary_group == "watch" \
            else "A 组 · 低位企稳组"
        secondary_title = "B 组 · 已出现企稳迹象（价格已在回升）" if primary_group == "watch" \
            else "B 组 · 低位但尚未企稳"

        flagged_html = ""
        if flagged:
            items = "".join(
                f"<li><b>{_escape(c.get('symbol'))}</b>　"
                f"{_escape(c.get('name'))}　近 1 月 "
                f"{_fmt((c.get('features') or {}).get('change_1m_pct'), 1, percent=True, signed=True)}"
                f"　→　{_escape(c.get('group_reason'))}</li>"
                for c in flagged
            )
            flagged_html = (
                f"<h2>已排除（{len(flagged)} 只）</h2><div class='panel'>"
                f"<ul class='reasons'>{items}</ul>"
                "<p class='muted'>一票否决：已反弹 / 持续创新低 / 波动异常 / 退市融资类披露。</p>"
                "</div>"
            )

        avg_score = (
            sum(float(r.get("total_score") or 0) for r in primary) / len(primary)
            if primary else 0
        )
        body = f"""
<h1>低位观察名单 · {_escape(session)}</h1>
{banner}
<div class="grid" style="margin-bottom:16px">
  <div class="stat"><div class="k">主名单</div><div class="v">{len(primary)} 只</div></div>
  <div class="stat"><div class="k">主名单平均分</div><div class="v">{avg_score:.1f} / 80</div></div>
  <div class="stat"><div class="k">次级名单</div><div class="v">{len(secondary)} 只</div></div>
  <div class="stat"><div class="k">一票否决</div><div class="v">{len(flagged)} 只</div></div>
</div>
<p class="sub">{pref_note}</p>
{switcher}
<h2>{primary_title}（{len(primary)} 只）</h2>
{_candidate_table(primary, links=self.links)}
{''.join(_candidate_detail(r, self.settings, self.links) for r in primary)}
<h2>{secondary_title}（{len(secondary)} 只）</h2>
{_candidate_table(secondary, show_reason=True, links=self.links)}
{flagged_html}
"""
        return 200, f"低位观察名单 {session}", _page(
            "低位观察名单", body, self.settings, "today", self.links
        )

    def page_history(self, query: Dict[str, List[str]]) -> Tuple[int, str, str]:
        def one(key: str, default: str = "") -> str:
            return (query.get(key) or [default])[0].strip()

        ticker = one("ticker")
        group = one("group")
        start_text, end_text = one("start"), one("end")
        min_score_text = one("min_score")

        def to_date(value: str) -> Optional[date]:
            try:
                return date.fromisoformat(value) if value else None
            except ValueError:
                return None

        try:
            min_score = float(min_score_text) if min_score_text else None
        except ValueError:
            min_score = None

        conn = self._connect()
        try:
            rows = history.search_candidates(
                conn, ticker=ticker or None, group=group or None,
                start=to_date(start_text), end=to_date(end_text),
                min_score=min_score, limit=300,
            )
            sessions = history.sessions_summary(conn, limit=120)
            top_symbols = history.top_symbols(conn, limit=12)
        finally:
            conn.close()

        symbol_rows = "".join(
            f"<tr><td class='l'><a href='{self.links.symbol(s['symbol'])}'>"
            f"{_escape(s['symbol'])}</a></td><td class='l'>{_escape(s['name'])}</td>"
            f"<td>{s['appearances']}</td><td>{_fmt(s['avg_score'], 1)}</td>"
            f"<td class='l'>{_escape(s['last_session'])}</td></tr>"
            for s in top_symbols
        )

        total = sum(s["total"] for s in sessions)
        if self.links.static:
            header = (
                "<div class='sub'>静态导出模式下不支持在线筛选；下面是全部历史记录。"
                "要按代码 / 分组 / 日期 / 最低分检索，在本机运行 "
                "<code>python3 -m us_low_scan.cli serve</code> 打开可交互版本。</div>"
            )
        else:
            header = f"""<div class="sub">可以按代码、分组、日期区间、最低总分任意组合筛选。
当前数据库里共 {total} 条历史记录。</div>
<div class="panel">
<form class="filters" method="get" action="{self.links.history()}">
  <label>代码<input name="ticker" value="{_escape(ticker)}" placeholder="如 AAPL"></label>
  <label>分组<select name="group">
    <option value="">全部</option>
    <option value="stabilized"{' selected' if group == 'stabilized' else ''}>低位企稳组</option>
    <option value="watch"{' selected' if group == 'watch' else ''}>低位未企稳观察组</option>
  </select></label>
  <label>开始日期<input name="start" type="date" value="{_escape(start_text)}"></label>
  <label>结束日期<input name="end" type="date" value="{_escape(end_text)}"></label>
  <label>最低总分<input name="min_score" value="{_escape(min_score_text)}" placeholder="如 60"></label>
  <button type="submit">检索</button>
  <a class="chip" href="{self.links.history()}">清除条件</a>
</form>
</div>"""

        body = f"""
<h1>历史名单检索</h1>
{header}
<h2>结果（{len(rows)} 条）</h2>
{_candidate_table(rows, show_reason=True, links=self.links)}
<h2>反复入选的代码</h2>
<div class="wrap"><table><thead><tr><th class="l">代码</th><th class="l">名称</th>
<th>入选次数</th><th>平均分</th><th class="l">最近一次</th></tr></thead>
<tbody>{symbol_rows or "<tr><td colspan=5 class='empty'>暂无数据</td></tr>"}</tbody></table></div>
"""
        return 200, "历史名单检索", _page(
            "历史名单检索", body, self.settings, "history", self.links
        )

    def page_symbol(self, ticker: str) -> Tuple[int, str, str]:
        conn = self._connect()
        try:
            data = history.symbol_timeline(conn, ticker)
        finally:
            conn.close()
        summary = data["summary"]
        history_rows = data["history"]
        if not history_rows:
            body = (f"<h1>{_escape(ticker.upper())}</h1>"
                    "<div class='panel'>数据库里没有这只股票的历史入选记录。</div>")
            return 200, ticker, _page(ticker, body, self.settings, "history", self.links)

        rows_html = "".join(
            "<tr>"
            f"<td class='l'>"
            + (f"<a href='{self.links.home(h['session'])}'>{h['session']}</a>"
               if not self.links.static else h["session"])
            + "</td>"
            f"<td class='l'>{_escape(h.get('group_label') or h['group'])}</td>"
            f"<td>{_fmt(h['total_score'], 1)}</td>"
            f"<td>{_fmt(h['close'], 2)}</td>"
            f"<td>{_fmt(h['distance_from_low_pct'], 1, percent=True, signed=True)}</td>"
            f"<td>{_fmt(h['rsi14'], 1)}</td>"
            f"<td class='{_pct_class(h['return_5d'])}'>{_fmt(h['return_5d'], 1, percent=True, signed=True)}</td>"
            f"<td class='{_pct_class(h['return_10d'])}'>{_fmt(h['return_10d'], 1, percent=True, signed=True)}</td>"
            f"<td class='{_pct_class(h['return_20d'])}'>{_fmt(h['return_20d'], 1, percent=True, signed=True)}</td>"
            f"<td class='{_pct_class(h['spy_return_20d'])}'>{_fmt(h['spy_return_20d'], 1, percent=True, signed=True)}</td>"
            f"<td>{_fmt(h['max_adverse_20d'], 1, percent=True)}</td>"
            "</tr>"
            for h in history_rows
        )
        body = f"""
<h1>{_escape(ticker.upper())}　<span class="muted">{_escape(history_rows[0].get('name'))}</span></h1>
<div class="sub">入选 {summary['appearances']} 次
  {f"· 20 日平均 {summary['mean_return_20d']*100:+.1f}%" if summary.get('mean_return_20d') is not None else ''}
  {f"· 20 日胜率 {summary['hit_rate_20d']*100:.0f}%" if summary.get('hit_rate_20d') is not None else ''}
  {f"· 10 日内再创新低 {summary['new_low_rate_10d']*100:.0f}%" if summary.get('new_low_rate_10d') is not None else ''}
</div>
<div class="wrap"><table><thead><tr>
<th class="l">入选日</th><th class="l">分组</th><th>总分</th><th>收盘</th><th>距低点</th><th>RSI</th>
<th>+5 日</th><th>+10 日</th><th>+20 日</th><th>SPY +20 日</th><th>最大不利</th>
</tr></thead><tbody>{rows_html}</tbody></table></div>
<p class="muted" style="margin-top:10px">收益列是入选后若干交易日的真实价格变化；
空值表示还没有足够的前瞻数据（不会用 0 或平均值填充）。</p>
"""
        return 200, f"{ticker} 历史", _page(ticker, body, self.settings, "history", self.links)

    def page_outcomes(self) -> Tuple[int, str, str]:
        conn = self._connect()
        try:
            overall = outcomes.summarize(conn)
            stabilized = outcomes.summarize(conn, "stabilized")
            watch = outcomes.summarize(conn, "watch")
        finally:
            conn.close()

        def block(title: str, summary: Dict[str, object]) -> str:
            if not summary.get("count"):
                return f"<h2>{title}</h2><div class='panel empty'>暂无已跟踪记录。</div>"
            rows = ""
            for horizon in outcomes.HORIZONS:
                entry = summary.get(f"horizon_{horizon}d") or {}
                rows += (
                    "<tr>"
                    f"<td class='l'>{horizon} 个交易日</td><td>{entry.get('samples', 0)}</td>"
                    f"<td class='{_pct_class(entry.get('mean'))}'>"
                    f"{_fmt((entry.get('mean') or 0) * 100, 2, signed=True) if entry.get('mean') is not None else '未核实'}%</td>"
                    f"<td>{(entry.get('hit_rate') or 0) * 100:.1f}%</td>"
                    f"<td class='{_pct_class(entry.get('mean_excess_vs_spy'))}'>"
                    f"{_fmt((entry.get('mean_excess_vs_spy') or 0) * 100, 2, signed=True) if entry.get('mean_excess_vs_spy') is not None else '未核实'}%</td>"
                    f"<td>{(entry.get('beat_spy_rate') or 0) * 100:.1f}%</td>"
                    f"<td class='down'>{_fmt((entry.get('mean_max_adverse') or 0) * 100, 2)}%</td>"
                    "</tr>"
                )
            extra = (
                f"<p class='muted'>入选后 10 个交易日内跌破入选时 52 周低点的比例："
                f"{summary['new_low_rate_10d']*100:.1f}%</p>"
                if summary.get("new_low_rate_10d") is not None else ""
            )
            return f"""<h2>{title}（{summary['count']} 条记录）</h2>
<div class="wrap"><table><thead><tr><th class="l">窗口</th><th>样本</th><th>平均收益</th>
<th>胜率</th><th>平均超额 vs SPY</th><th>跑赢 SPY 比例</th><th>平均最大不利波动</th>
</tr></thead><tbody>{rows}</tbody></table></div>{extra}"""

        body = ("<h1>候选股表现跟踪</h1>"
                "<div class='sub'>入选之后到底发生了什么 —— 这是校正规则的唯一依据。"
                "先跑 scan，再跑 outcomes 才有数据。</div>"
                + block("全部候选", overall)
                + block("低位企稳组", stabilized)
                + block("低位未企稳观察组", watch))
        return 200, "表现跟踪", _page(
            "表现跟踪", body, self.settings, "outcomes", self.links
        )

    def page_lab(self, query: Dict[str, List[str]]) -> Tuple[int, str, str]:
        conn = self._connect()
        try:
            sessions = lab.available_sessions(conn, limit=60)
            session_text = (query.get("session") or [""])[0].strip() or (sessions[0] if sessions else "")
            preset = (query.get("preset") or [""])[0].strip()
            base = self.settings
            if preset:
                from .config import apply_preset

                try:
                    base = apply_preset(self.settings, preset)
                except Exception:  # 预设填错时不影响页面本身
                    base = self.settings
            params = lab.params_from_settings(base)
            overrides = {
                key: (query.get(key) or [""])[0]
                for key in params.as_dict()
                if query.get(key)
            }
            params = lab.coerce_params(params, overrides)
            session_date = _safe_date(session_text)
            if session_date is None:
                result = {
                    "error": (
                        f"日期 {session_text!r} 无法识别，或数据库里还没有特征快照。"
                        "请从下拉框里选一个交易日，或先运行 scan。"
                    )
                }
            else:
                result = lab.run_lab(conn, base, session_date, params)
            preset_names = sorted(self.settings.presets)
        finally:
            conn.close()

        def number_field(key: str, label: str, step: str = "1") -> str:
            value = (result.get("params") or {}).get(key, "")
            return (f"<label>{label}<input name='{key}' type='number' step='{step}' "
                    f"value='{value}'></label>")

        options = "".join(
            f"<option value='{s}'{' selected' if s == session_text else ''}>{s}</option>"
            for s in sessions
        )
        preset_options = "<option value=''>默认配置</option>" + "".join(
            f"<option value='{p}'{' selected' if p == preset else ''}>{p}</option>"
            for p in preset_names
        )

        if result.get("error"):
            result_html = f"<div class='panel empty'>{_escape(result['error'])}</div>"
        else:
            diff = result["diff"]
            def diff_chips(items, cls, label):
                if not items:
                    return f"<span class='chip'>{label} 0</span>"
                inner = "、".join(_escape(x) for x in items[:12])
                return f"<span class='chip {cls}'>{label} {len(items)}：{inner}</span>"

            result_html = f"""
<div class="grid" style="margin-bottom:14px">
  <div class="stat"><div class="k">特征快照</div><div class="v">{result['universe']} 只</div></div>
  <div class="stat"><div class="k">价格/流动性通过</div><div class="v">{result['after_price_liquidity']} 只</div></div>
  <div class="stat"><div class="k">低位区间通过</div><div class="v">{result['after_low_position']} 只</div></div>
  <div class="stat"><div class="k">主名单</div><div class="v">{len(result.get('primary') or [])} 只</div></div>
  <div class="stat"><div class="k">被一票否决</div><div class="v">{len(result.get('flagged') or [])} 只</div></div>
</div>
<div style="margin-bottom:14px">
  {diff_chips(diff['added'], 'ok', '新增')}
  {diff_chips(diff['removed'], 'high', '消失')}
  {diff_chips(diff['kept'], '', '保持')}
</div>
<h2>A 组 · 主名单（{len(result.get('primary') or [])} 只）</h2>
{_candidate_table(result.get('primary') or [], links=self.links)}
<h2>B 组 · 次级（{len(result.get('secondary') or [])} 只）</h2>
{_candidate_table(result.get('secondary') or [], show_reason=True, links=self.links)}
<p class="muted">{_escape(result['caveat'])}</p>
"""

        body = f"""
<h1>参数实验台</h1>
<div class="sub">用数据库里已保存的特征快照重算名单，不需要重新抓行情。
改了门槛立刻就能看到「多出哪几只、少掉哪几只」。</div>
<div class="panel">
<form class="filters" method="get" action="/lab">
  <label>交易日<select name="session">{options}</select></label>
  <label>参数方案<select name="preset">{preset_options}</select></label>
  <label>距 52 周低点上限 %<input name="near_low_pct" type="number" step="1" value="{params.near_low_pct}"></label>
  <label>较高点最小回撤 %<input name="min_drawdown_from_high_pct" type="number" step="1" value="{params.min_drawdown_from_high_pct}"></label>
  <label>20 日均额门槛<input name="min_avg_dollar_volume_20d" type="number" step="1000000" value="{params.min_avg_dollar_volume_20d}"></label>
  <label>企稳分门槛<input name="min_stabilization_score" type="number" step="1" value="{params.min_stabilization_score}"></label>
  <label>流动性权重<input name="weight_liquidity" type="number" step="1" value="{params.weight_liquidity}"></label>
  <label>低位权重<input name="weight_low_position" type="number" step="1" value="{params.weight_low_position}"></label>
  <label>企稳权重<input name="weight_stabilization" type="number" step="1" value="{params.weight_stabilization}"></label>
  <button type="submit">试算</button>
</form>
</div>
{result_html}
"""
        return 200, "参数实验台", _page(
            "参数实验台", body, self.settings, "lab", self.links
        )

    # -- API -------------------------------------------------------------
    def api(self, path: str, query: Dict[str, List[str]]) -> Tuple[int, object]:
        conn = self._connect()
        try:
            if path == "/api/sessions":
                return 200, history.sessions_summary(conn, limit=120)
            if path == "/api/latest":
                session = history.latest_session(conn)
                if not session:
                    return 200, {"session": None, "candidates": []}
                return 200, {
                    "session": session,
                    "candidates": history.session_candidates_detail(conn, session),
                }
            if path.startswith("/api/session/"):
                session = path.rsplit("/", 1)[-1]
                return 200, {
                    "session": session,
                    "candidates": history.session_candidates_detail(conn, session),
                }
            if path == "/api/history":
                def one(key):
                    return (query.get(key) or [""])[0].strip() or None
                start = one("start")
                end = one("end")
                return 200, history.search_candidates(
                    conn, ticker=one("ticker"), group=one("group"),
                    start=date.fromisoformat(start) if start else None,
                    end=date.fromisoformat(end) if end else None,
                    min_score=float(one("min_score")) if one("min_score") else None,
                    limit=int(one("limit") or 200),
                )
            if path.startswith("/api/symbol/"):
                return 200, history.symbol_timeline(conn, path.rsplit("/", 1)[-1])
            if path == "/api/outcomes":
                return 200, {
                    "all": outcomes.summarize(conn),
                    "stabilized": outcomes.summarize(conn, "stabilized"),
                    "watch": outcomes.summarize(conn, "watch"),
                }
            if path == "/api/lab":
                session = (query.get("session") or [""])[0]
                preset = (query.get("preset") or [""])[0]
                base = self.settings
                if preset:
                    from .config import apply_preset

                    base = apply_preset(self.settings, preset)
                params = lab.coerce_params(
                    lab.params_from_settings(base),
                    {k: v[0] for k, v in query.items() if k in lab.params_from_settings(base).as_dict()},
                )
                if not session:
                    sessions = lab.available_sessions(conn, limit=1)
                    session = sessions[0] if sessions else ""
                if not session:
                    return 200, {"error": "没有可用的特征快照"}
                session_date = _safe_date(session)
                if session_date is None:
                    return 400, {"error": f"日期 {session!r} 无法识别"}
                return 200, lab.run_lab(conn, base, session_date, params)
        finally:
            conn.close()
        return 404, {"error": f"未知接口 {path}"}


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------
def _make_handler(dashboard: Dashboard, settings):
    class Handler(BaseHTTPRequestHandler):
        server_version = "us-low-scan"

        def log_message(self, fmt, *args):  # 静音，避免刷屏
            pass

        # -- 工具 ---------------------------------------------------------
        def _send(self, status: int, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _send_html(self, status: int, text: str) -> None:
            self._send(status, text.encode("utf-8"), "text/html; charset=utf-8")

        def _send_json(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            self._send(status, body.encode("utf-8"), "application/json; charset=utf-8")

        def _error_page(self, status: int, message: str) -> None:
            self._send_html(
                status,
                _page(f"错误 {status}",
                      f"<h1>错误 {status}</h1><div class='panel'>{_escape(message)}</div>",
                      settings, links=Links()),
            )

        def _serve_file(self, session: str, name: str) -> None:
            base = Path(settings.general.output_dir).resolve()
            target = (base / session / name).resolve()
            if not str(target).startswith(str(base)) or not target.is_file():
                self._error_page(404, "文件不存在")
                return
            types = {".md": "text/markdown; charset=utf-8", ".csv": "text/csv; charset=utf-8",
                     ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
            self._send(200, target.read_bytes(),
                       types.get(target.suffix, "application/octet-stream"))

        # -- 路由 ---------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/catchup":
                self._error_page(404, "页面不存在")
                return
            try:
                result = dashboard.run_catch_up()
            except Exception as exc:  # noqa: BLE001
                self._error_page(500, f"补跑失败：{type(exc).__name__}: {exc}")
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if path.startswith("/api/"):
                    status, payload = dashboard.api(path, query)
                    self._send_json(status, payload)
                    return
                if path == "/":
                    session = (query.get("session") or [""])[0].strip() or None
                    status, title, html = dashboard.page_today(session)
                elif path == "/history":
                    status, title, html = dashboard.page_history(query)
                elif path.startswith("/symbol/"):
                    status, title, html = dashboard.page_symbol(path.rsplit("/", 1)[-1])
                elif path == "/outcomes":
                    status, title, html = dashboard.page_outcomes()
                elif path == "/lab":
                    status, title, html = dashboard.page_lab(query)
                elif path.startswith("/file/"):
                    parts = path.split("/")
                    if len(parts) != 4:
                        self._error_page(404, "路径格式应为 /file/<日期>/<文件名>")
                        return
                    self._serve_file(parts[2], parts[3])
                    return
                elif path == "/healthz":
                    self._send_json(200, {"ok": True})
                    return
                else:
                    self._error_page(404, "页面不存在")
                    return
                if status == 200:
                    self._send_html(status, html)
                else:
                    self._error_page(status, title)
            except Exception as exc:  # noqa: BLE001 - 单个请求出错不该拖垮服务
                self._error_page(500, f"{type(exc).__name__}: {exc}")

    return Handler


def create_server(settings, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    dashboard = Dashboard(settings, links=Links())
    handler = _make_handler(dashboard, settings)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(settings, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(settings, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"仪表板已启动：{url}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
