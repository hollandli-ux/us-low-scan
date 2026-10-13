"""命令行入口。

常用命令
--------
    python -m us_low_scan.cli init-db              # 建库
    python -m us_low_scan.cli demo                 # 用离线合成数据跑通全流程
    python -m us_low_scan.cli scan                 # 按配置扫描最近一个已收盘交易日
    python -m us_low_scan.cli scan --session 2026-10-09
    python -m us_low_scan.cli outcomes             # 跟踪已入选股票的前瞻表现
    python -m us_low_scan.cli backtest --step 3    # 历史回放
    python -m us_low_scan.cli report --session 2026-10-09   # 重渲染报告
    python -m us_low_scan.cli doctor               # 自检（配置 + 数据源）
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from . import backtest as backtest_module
from . import calendar_us, db, history as history_module, lab as lab_module
from . import notify as notify_module, outcomes as outcomes_module, pipeline, report, site
from .config import ConfigError, apply_preset, load_settings, preset_names
from .providers import DataProviderError, get_provider

# 退出码：0 正常、1 运行错误、2 配置错误、3 数据源错误
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_PROVIDER = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="us_low_scan",
        description="美股低位高流动性股票观察工具（观察名单，不构成投资建议）",
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/config.ini）")
    parser.add_argument("--db", default=None, help="覆盖数据库路径")
    parser.add_argument("--preset", default=None,
                        help="参数方案（见 [presets]，如 conservative / aggressive）")
    parser.add_argument("--provider", default=None,
                        help="临时覆盖数据源：sample / csv / nasdaq / alpaca / alphavantage")
    parser.add_argument("--top", type=int, default=None, metavar="N",
                        help="临时覆盖 [universe] prefilter_top_market_cap（只拉市值前 N 只）")
    parser.add_argument("--watchlist", default=None,
                        help="临时指定只扫描的代码清单（CSV/纯文本，每行一个 ticker）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化 SQLite 数据库")
    sub.add_parser("doctor", help="自检：配置、数据库、数据源连通性")

    scan = sub.add_parser("scan", help="按配置执行一次扫描并输出报告")
    scan.add_argument("--session", default=None, help="交易日 YYYY-MM-DD（默认最近一个已收盘交易日）")
    scan.add_argument("--catch-up", action="store_true",
                      help="先把错过的交易日按顺序补齐，再出最新一期（关机/睡眠后用它）")
    scan.add_argument("--no-persist", action="store_true", help="只输出报告，不写入数据库")
    scan.add_argument("--quiet", action="store_true", help="不打印摘要")

    demo = sub.add_parser("demo", help="用离线合成数据跑通全流程（无需联网）")
    demo.add_argument("--symbols", type=int, default=None, help="合成市场股票数量")
    demo.add_argument("--backtest-sessions", type=int, default=60, help="回放多少个交易日")
    demo.add_argument("--with-outcomes", action="store_true", help="同时演示前瞻表现跟踪")

    rep = sub.add_parser("report", help="用已保存的 JSON 重新渲染报告")
    rep.add_argument("--session", required=True, help="交易日 YYYY-MM-DD")

    out = sub.add_parser("outcomes", help="更新并汇总候选股的前瞻表现")
    out.add_argument("--as-of", default=None, help="截止日期 YYYY-MM-DD")
    out.add_argument("--no-update", action="store_true", help="只汇总，不更新")

    his = sub.add_parser("history", help="历史名单检索（不带参数时列出每个交易日）")
    his.add_argument("--ticker", default=None, help="按代码过滤（支持模糊匹配）")
    his.add_argument("--group", default=None, choices=["stabilized", "watch"], help="按分组过滤")
    his.add_argument("--from", dest="start", default=None, help="开始日期 YYYY-MM-DD")
    his.add_argument("--to", dest="end", default=None, help="结束日期 YYYY-MM-DD")
    his.add_argument("--min-score", type=float, default=None, help="最低总分")
    his.add_argument("--limit", type=int, default=100, help="最多显示多少条")

    st = sub.add_parser("site", help="导出可以直接双击打开的静态网站")
    st.add_argument("--out", default=None, help="输出目录（默认取 [output] html_dir）")
    st.add_argument("--print-path", action="store_true", help="只打印 index.html 的路径")

    sv = sub.add_parser("serve", help="启动网页仪表板（可交互，需要本机跑着）")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)

    nt = sub.add_parser("notify", help="把名单发到邮件 / Webhook / macOS 通知")
    nt.add_argument("--session", default=None, help="交易日 YYYY-MM-DD（默认最新一期）")
    nt.add_argument("--channel", default=None, help="逗号分隔：email,webhook,macos")
    nt.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不真的发送")
    nt.add_argument("--force", action="store_true", help="忽略 min_main_candidates 门槛")

    lb = sub.add_parser("lab", help="参数实验台：改门槛与权重，看名单怎么变")
    lb.add_argument("--session", default=None, help="交易日 YYYY-MM-DD（默认最新有特征快照的一天）")
    lb.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="覆盖某个参数，可重复，例如 --set near_low_pct=20")

    bt = sub.add_parser("backtest", help="历史回放，验证规则")
    bt.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD")
    bt.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    bt.add_argument("--step", type=int, default=1, help="每隔几个交易日回放一次")
    return parser


def _load_settings(args):
    settings = load_settings(args.config)
    if getattr(args, "preset", None):
        settings = apply_preset(settings, args.preset)
    if getattr(args, "provider", None):
        settings = replace(
            settings, data_source=replace(settings.data_source, provider=args.provider)
        )
    if getattr(args, "top", None) is not None:
        settings = replace(
            settings,
            universe=replace(settings.universe, prefilter_top_market_cap=args.top),
        )
    if getattr(args, "watchlist", None):
        settings = replace(
            settings,
            universe=replace(settings.universe, watchlist_file=Path(args.watchlist)),
        )
    # 用真实数据源时，全美有 7000 只股票、每只一次请求 —— 不设上限会跑几个小时。
    # 所以这里兜一个默认值，并把话说清楚。
    if (
        settings.data_source.provider in ("nasdaq", "alpaca", "alphavantage")
        and settings.universe.prefilter_top_market_cap <= 0
    ):
        default_top = 250
        settings = replace(
            settings,
            universe=replace(
                settings.universe, prefilter_top_market_cap=default_top
            ),
        )
        print(
            f"[提示] 数据源 {settings.data_source.provider} 需要逐只请求行情，"
            f"本次自动限制为「市值前 {default_top} 只」（约 1-3 分钟）。\n"
            f"       想扫更多：--top 500；想扫指定清单：--watchlist your.txt；"
            f"想扫全市场：--top 0（会很慢）"
        )
    from .config import validate_settings

    validate_settings(settings)
    if args.db:
        settings = replace(settings, general=replace(settings.general, database=Path(args.db)))
    return settings


def _open_db(settings):
    conn = db.connect(settings.general.database)
    db.init_db(conn)
    return conn


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(value)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_init_db(args) -> int:
    settings = _load_settings(args)
    conn = _open_db(settings)
    conn.close()
    print(f"数据库已就绪：{settings.general.database}")
    print(f"报告目录：{settings.general.output_dir}")
    print(f"配置文件：{settings.path}")
    return EXIT_OK


def cmd_scan(args) -> int:
    settings = _load_settings(args)
    session = _parse_date(args.session)
    conn = None if args.no_persist else _open_db(settings)

    if args.catch_up and conn is not None:
        missing = pipeline.pending_sessions(conn, settings)
        if missing:
            print(f"发现 {len(missing)} 个交易日还没出过名单，先补跑：")
            def _progress(index, total, day=None):
                if day is None:
                    print(f"  正在抓取行情（{total} 个交易日待补）…", flush=True)
                else:
                    print(f"  [{index}/{total}] {day.isoformat()}", flush=True)

            results = pipeline.catch_up(settings, conn=conn, progress=_progress)
            for item in results:
                report.write_outputs(item, settings)
                mark = f"{len(item.primary)} 只主名单" if not item.skipped_reason else "跳过"
                print(f"      {item.session.isoformat()}：{mark}")
        else:
            print("没有需要补跑的交易日。")

    result = pipeline.run_scan(settings, session=session, conn=conn)
    if result.skipped_reason:
        print(f"[跳过] {result.skipped_reason}")
        if conn:
            conn.close()
        return EXIT_OK
    paths = report.write_outputs(result, settings)
    _maybe_export_site(settings, conn)
    if not args.quiet:
        _print_summary(result, paths)
    if conn:
        conn.close()
    _maybe_notify(settings, result)
    return EXIT_OK


def cmd_demo(args) -> int:
    settings = _load_settings(args)
    if args.symbols:
        settings = replace(
            settings, data_source=replace(settings.data_source, sample_symbols=args.symbols)
        )
    # demo 一律使用离线合成数据，保证无网络也能跑通。
    # 并且写到独立的库/目录，绝不和真实数据混在一起（否则历史名单里会真假混杂）。
    demo_db = settings.general.project_root / "data" / "demo.db"
    demo_out = settings.general.project_root / "reports" / "demo"
    settings = replace(
        settings,
        data_source=replace(settings.data_source, provider="sample"),
        general=replace(
            settings.general, database=demo_db, output_dir=demo_out
        ),
    )
    print(f"（演示模式：合成数据 → {demo_db.name} + reports/demo/，不影响真实数据）")
    conn = _open_db(settings)

    print("=" * 72)
    print("1/3 每日扫描（离线合成数据）")
    print("=" * 72)
    result = pipeline.run_scan(settings, conn=conn)
    paths = report.write_outputs(result, settings)
    _print_summary(result, paths)

    print()
    print("=" * 72)
    print(f"2/3 历史回放（最近 {args.backtest_sessions} 个交易日，验证规则是否有效）")
    print("=" * 72)
    bt = run_backtest_command(settings, None, None, 1, limit_sessions=args.backtest_sessions)
    _print_backtest_summary(bt)
    bt_path = _write_backtest_report(settings, bt)
    print(f"回放报告：{bt_path}")

    _maybe_export_site(settings, conn)

    if args.with_outcomes:
        print()
        print("=" * 72)
        print("3/3 前瞻表现跟踪（用 30 个交易日前的那次扫描做样本）")
        print("=" * 72)
        past_session = calendar_us.shift_trading_days(
            calendar_us.last_completed_session(), -30
        )
        past = pipeline.run_scan(settings, session=past_session, conn=conn)
        print(f"在 {past_session} 出过 {len(past.candidates)} 只候选（已写入数据库）")
        updated = outcomes_module.update_outcomes(conn, settings)
        print(f"已补齐 {updated} 条前瞻表现记录")
        summary = outcomes_module.summarize(conn)
        markdown = outcomes_module.render_summary_markdown(summary)
        out_path = Path(settings.general.output_dir) / "outcomes.md"
        out_path.write_text(markdown, encoding="utf-8")
        print(markdown)
        print(f"跟踪报告：{out_path}")

    conn.close()
    print()
    print("完成。以上全部使用离线合成数据，不是真实行情。")
    return EXIT_OK


def run_backtest_command(settings, start, end, step, limit_sessions=None):
    """``limit_sessions`` 表示"只回放最后 N 个交易日"（默认回放全部可用区间）。"""
    provider = get_provider(settings.data_source.provider, settings)
    if limit_sessions:
        symbols = provider.list_symbols()
        benchmark = settings.general.benchmark
        bars_map = provider.get_bars_multi(sorted({s.ticker for s in symbols} | {benchmark}))
        days = sorted({b.day for bars in bars_map.values() for b in bars})
        if days:
            start = days[max(0, len(days) - limit_sessions)]
    return backtest_module.run_backtest(
        settings,
        start=start,
        end=end,
        step=step,
        provider=provider,
        progress=lambda i, n: print(f"  回放进度 {i}/{n}", flush=True),
    )


def _write_backtest_report(settings, bt) -> Path:
    out_dir = Path(settings.general.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest_{bt['start']}_{bt['end']}.md"
    path.write_text(backtest_module.render_backtest_markdown(bt, settings), encoding="utf-8")
    return path


def cmd_backtest(args) -> int:
    settings = _load_settings(args)
    bt = run_backtest_command(
        settings, _parse_date(args.start), _parse_date(args.end), args.step
    )
    _print_backtest_summary(bt)
    print(f"回放报告：{_write_backtest_report(settings, bt)}")
    return EXIT_OK


def cmd_outcomes(args) -> int:
    settings = _load_settings(args)
    conn = _open_db(settings)
    if not args.no_update:
        updated = outcomes_module.update_outcomes(conn, settings, as_of=_parse_date(args.as_of))
        print(f"已更新 {updated} 条前瞻表现记录")
    summary = outcomes_module.summarize(conn)
    markdown = outcomes_module.render_summary_markdown(summary)
    path = Path(settings.general.output_dir) / "outcomes.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"\n已写入 {path}")
    conn.close()
    return EXIT_OK


def cmd_history(args) -> int:
    settings = _load_settings(args)
    conn = _open_db(settings)
    if args.ticker or args.group or args.start or args.end or args.min_score:
        rows = history_module.search_candidates(
            conn,
            ticker=args.ticker,
            group=args.group,
            start=_parse_date(args.start),
            end=_parse_date(args.end),
            min_score=args.min_score,
            limit=args.limit,
        )
        if not rows:
            print("没有匹配的历史记录。")
        else:
            print(f"{'交易日':<12}{'代码':<9}{'分组':<12}{'总分':>7}{'收盘':>10}{'距低点':>9}")
            for row in rows:
                distance = row.get("distance_from_low_pct")
                print(
                    f"{row['session']:<12}{row['symbol']:<9}"
                    f"{'企稳' if row['group'] == 'stabilized' else '观察':<12}"
                    f"{row['total_score']:>7.1f}{row['close']:>10.2f}"
                    f"{(f'{distance:>8.1f}%' if distance is not None else '    未核实')}"
                )
            print(f"\n共 {len(rows)} 条（上限 {args.limit}）。")
        conn.close()
        return EXIT_OK

    sessions = db.list_candidate_sessions(conn, limit=120)
    if not sessions:
        print("数据库中还没有任何候选记录。先运行 scan 或 demo。")
        conn.close()
        return EXIT_OK
    print(f"{'交易日':<12}{'候选数':>8}{'低位企稳':>10}{'观察':>8}{'平均分':>9}")
    for session_text in sessions:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN group_name IN ('stabilized','低位企稳组') THEN 1 ELSE 0 END)"
            "   AS stabilized,"
            " SUM(CASE WHEN group_name IN ('watch','低位未企稳观察组') THEN 1 ELSE 0 END)"
            "   AS watch,"
            " ROUND(AVG(total_score), 1) AS avg_score"
            " FROM daily_candidates WHERE session = ?",
            (session_text,),
        ).fetchone()
        print(
            f"{session_text:<12}{row['total']:>8}{row['stabilized'] or 0:>10}"
            f"{row['watch'] or 0:>8}{row['avg_score']:>9}"
        )
    print("\n提示：加 --ticker AAPL / --group stabilized / --from 2026-08-01 可以精确检索。")
    conn.close()
    return EXIT_OK


def cmd_site(args) -> int:
    settings = _load_settings(args)
    conn = _open_db(settings)
    try:
        files = site.export_site(settings, conn=conn, out_dir=args.out)
    finally:
        conn.close()
    index = files.get("index.html")
    if args.print_path and index:
        print(index)
        return EXIT_OK
    print(f"静态网站已导出：{index}")
    print(f"共 {len(files)} 个页面。用浏览器打开上面那个文件即可（不需要启动服务）。")
    print("也可以： open \"" + str(index) + "\"")
    return EXIT_OK


def cmd_serve(args) -> int:
    settings = _load_settings(args)
    from . import webapp

    webapp.serve(settings, host=args.host, port=args.port)
    return EXIT_OK


def _describe_notify(item) -> str:
    """把通知结果压缩成一句人话，避免只打印一个空冒号。"""
    channel = item.get("channel")
    status = item.get("status")
    if status in ("failed", "skipped", "unknown"):
        return str(item.get("detail") or "")
    if channel == "email":
        to = item.get("to") or []
        host = item.get("host") or ""
        return f"{host}:{item.get('port')} → {', '.join(to) if to else '（未配置收件人）'}"
    if channel == "webhook":
        return f"{item.get('kind') or ''} → {item.get('url') or '（未配置）'}"
    if channel == "macos":
        return "osascript display notification"
    return str(item.get("detail") or "")


def cmd_notify(args) -> int:
    settings = _load_settings(args)
    scan = _load_scan(settings, args.session)
    if scan is None:
        print("[跳过] 找不到可发送的名单，先运行 scan 或 demo。")
        return EXIT_OK
    channels = [c.strip() for c in args.channel.split(",")] if args.channel else None
    results = notify_module.notify(
        settings, scan, channels=channels, dry_run=args.dry_run, force=args.force
    )
    for item in results:
        status = item.get("status")
        tag = {"sent": "✅ 已发送", "dry-run": "🧪 试运行", "skipped": "⏭️ 跳过",
               "failed": "❌ 失败", "unknown": "❓ 未知"}.get(status, status)
        print(f"[{tag}] {item.get('channel')}　{_describe_notify(item)}")

        if not args.dry_run:
            continue
        if item.get("kind"):
            print(f"  webhook_kind = {item['kind']}")
        if item.get("url"):
            print(f"  webhook_url  = {item['url']}")
        if item.get("payload") is not None:
            print("  将要发送的 JSON：")
            print("    " + json.dumps(item["payload"], ensure_ascii=False))
        if item.get("subject"):
            print(f"  邮件主题：{item['subject']}")
    if not args.dry_run:
        print("\n提示：先用 --dry-run 确认格式，再去掉它真正发送。")
    return EXIT_OK


def cmd_lab(args) -> int:
    settings = _load_settings(args)
    conn = _open_db(settings)
    session_text = args.session
    if not session_text:
        sessions = lab_module.available_sessions(conn, limit=1)
        if not sessions:
            print("[跳过] 数据库里还没有特征快照，先运行 scan 或 demo。")
            conn.close()
            return EXIT_OK
        session_text = sessions[0]
    session = _parse_date(session_text)

    base_params = lab_module.params_from_settings(settings)
    overrides = {}
    for item in args.set:
        if "=" not in item:
            print(f"[忽略] --set {item} 格式应为 KEY=VALUE")
            continue
        key, value = item.split("=", 1)
        overrides[key.strip()] = value.strip()
    params = lab_module.coerce_params(base_params, overrides)

    result = lab_module.run_lab(conn, settings, session, params)
    conn.close()
    if result.get("error"):
        print(f"[跳过] {result['error']}")
        return EXIT_OK

    print(f"参数实验台 · {result['session']}")
    print(f"  特征快照 {result['universe']} 只 -> 价格/流动性 {result['after_price_liquidity']} 只"
          f" -> 低位区间 {result['after_low_position']} 只")
    diff = result["diff"]
    print(f"  与正式名单相比：新增 {len(diff['added'])}，消失 {len(diff['removed'])}，"
          f"保持 {len(diff['kept'])}")
    if diff["added"]:
        print(f"    新增：{', '.join(diff['added'])}")
    if diff["removed"]:
        print(f"    消失：{', '.join(diff['removed'])}")
    print()
    primary_label = "低位观察（还没涨起来）" if result.get("primary_group") == "watch" else "低位企稳"
    print(f"  A 组 {primary_label}（{len(result.get('primary') or [])} 只）")
    for candidate in result.get("primary") or []:
        f = candidate["features"]
        print(f"    {candidate['symbol']:<8} {candidate['score']['total']:>6.1f}"
              f"/{candidate['score']['max_available']:.0f}"
              f"  距低点 {f['distance_from_low_pct']:>5.1f}%"
              f"  近1月 {f['change_1m_pct']:>6.1f}%"
              f"  RSI {f['rsi14']:>5.1f}")
    if not result.get("primary"):
        print("    （这套参数下没有符合条件的候选）")
    print()
    print(f"  B 组 次级（{len(result.get('secondary') or [])} 只）："
          + ("、".join(c["symbol"] for c in result.get("secondary") or []) or "无"))
    if result.get("flagged"):
        print(f"  已排除（{len(result['flagged'])} 只）："
              + "、".join(c["symbol"] for c in result["flagged"]))
    print()
    print(f"  说明：{result['caveat']}")
    print("  满意的话，把这些值写进 config/config.ini 的 [preset.<名字>]，")
    print("  以后用 --preset <名字> 一键复用。")
    return EXIT_OK


def _maybe_export_site(settings, conn) -> None:
    """每次扫描后顺手导出静态站点 —— 这样用户直接双击就能看，不用起服务。"""
    if not settings.output.write_html:
        return
    try:
        files = site.export_site(settings, conn=conn)
        index = files.get("index.html")
        if index:
            print(f"静态网站：{index}（浏览器打开即可，不需要启动服务）")
    except Exception as exc:  # 导出失败不该让扫描失败
        print(f"[提示] 静态网站导出失败：{type(exc).__name__}: {exc}")


def _load_scan(settings, session_text=None):
    """从保存的 JSON 里读回一次扫描结果（用于重渲染/通知）。"""
    base = Path(settings.general.output_dir)
    if session_text:
        path = base / session_text / "low_scan.json"
    else:
        candidates = sorted(
            (p for p in base.glob("*/low_scan.json")), key=lambda p: p.parent.name
        ) if base.exists() else []
        if not candidates:
            return None
        path = candidates[-1]
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return report.scan_result_from_dict(data)


def _maybe_notify(settings, scan) -> None:
    """scan 结束后按配置自动发通知；通知失败不影响主流程。"""
    if not settings.notify.enabled:
        return
    results = notify_module.notify(settings, scan)
    for item in results:
        if item.get("status") == "skipped":
            continue
        mark = "✅" if item.get("status") == "sent" else "❌"
        print(f"{mark} 通知[{item.get('channel')}] {item.get('status')} "
              f"{item.get('detail') or ''}".rstrip())


def cmd_report(args) -> int:
    settings = _load_settings(args)
    try:
        markdown = report.render_saved_report(settings, args.session)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return EXIT_ERROR
    print(markdown)
    return EXIT_OK


def cmd_doctor(args) -> int:
    settings = _load_settings(args)
    print(f"配置文件：{settings.path}")
    print(f"项目根目录：{settings.general.project_root}")
    print(f"数据库：{settings.general.database}")
    print(f"报告目录：{settings.general.output_dir}")
    print(f"数据源：{settings.data_source.provider}")
    print(f"市场时区：{settings.general.market_timezone}；展示时区：{settings.general.display_timezone}")
    print(f"最近已收盘交易日：{calendar_us.last_completed_session().isoformat()}")
    print(f"下一交易日：{calendar_us.next_trading_day(calendar_us.last_completed_session()).isoformat()}")
    _report_freshness(settings, prefix="数据状态：")
    print(
        "评分维度：流动性 {:.0f} / 低位 {:.0f} / 企稳 {:.0f} / 质量 {:.0f}".format(
            settings.scoring.weight_liquidity,
            settings.scoring.weight_low_position,
            settings.scoring.weight_stabilization,
            settings.scoring.weight_quality,
        )
    )
    if not settings.scoring.quality_enabled:
        print("提示：quality_enabled=false，总分将以 80 分制展示（基本面维度不补分）")

    if settings.data_source.provider == "sample":
        print(
            "提示：当前是离线合成数据源（股票代码是虚构的），只用于验证流程。\n"
            "      看真实股票： python3 -m us_low_scan.cli --provider nasdaq --top 150 scan"
        )
        return EXIT_OK

    try:
        provider = get_provider(settings.data_source.provider, settings)
        symbols = provider.list_symbols()
        print(f"数据源连通：OK，返回 {len(symbols)} 个标的")
        if symbols:
            bars = provider.get_daily_bars(symbols[0].ticker)
            latest = bars[-1].day.isoformat() if bars else "无"
            print(f"样本 {symbols[0].ticker}：{len(bars)} 根 K 线，最新 {latest}")
        print(f"覆盖说明：{provider.coverage_note}")
    except DataProviderError as exc:
        print(f"[数据源错误] {exc}")
        return EXIT_PROVIDER
    return EXIT_OK


# --------------------------------------------------------------------------
# 打印
# --------------------------------------------------------------------------
def _report_freshness(settings, prefix: str = "") -> None:
    """告诉使用者：数据到底新到哪一天，还差几天没跑。"""
    try:
        conn = _open_db(settings)
    except Exception:
        return
    try:
        sessions = db.list_candidate_sessions(conn, limit=1)
        last = sessions[0] if sessions else "（从未运行过）"
        pending = pipeline.pending_sessions(conn, settings)
        print(f"{prefix}最近出过名单的交易日 {last}")
        if pending:
            print(
                f"{prefix}有 {len(pending)} 个交易日还没出名单："
                f"{pending[0].isoformat()} ~ {pending[-1].isoformat()}；"
                "运行 scan --catch-up 可以补齐"
            )
        else:
            print(f"{prefix}已是最新，没有需要补跑的交易日")
    finally:
        conn.close()


def _print_summary(result, paths) -> None:
    print(f"交易日 {result.session.isoformat()}｜股票池 {result.universe_size} 只")
    regime = result.market_regime or {}
    print(f"市场环境：{regime.get('status', '未核实')}")
    print()
    primary_label = (
        "A. 低位观察组（还没涨起来，符合买跌偏好）"
        if result.primary_group == "watch"
        else "A. 低位企稳组"
    )
    secondary_label = (
        "B. 已出现企稳迹象（价格已经回升）"
        if result.primary_group == "watch"
        else "B. 低位但尚未企稳"
    )

    print(f"{primary_label}（{len(result.primary)} 只）")
    for candidate in result.primary:
        f = candidate.features
        print(f"   {candidate.symbol:<8} {candidate.score.total:>6.1f}/{candidate.score.max_available:.0f}"
              f"  距低点 {f.distance_from_low_pct:>5.1f}%"
              f"  近1月 {f.change_1m_pct:>6.1f}%"
              f"  RSI {f.rsi14:>5.1f}"
              f"  20日均额 {f.avg_dollar_volume_20d/1e6:>7.1f}M")
    if not result.primary:
        print("   （暂无符合条件的候选：宁可不给名单，也不凑数）")
    print()
    print(f"{secondary_label}（{len(result.secondary)} 只）")
    for candidate in result.secondary:
        f = candidate.features
        print(f"   {candidate.symbol:<8} {candidate.score.total:>6.1f}/{candidate.score.max_available:.0f}"
              f"  近1月 {f.change_1m_pct:>6.1f}%"
              f"  RSI {f.rsi14:>5.1f}  {candidate.group_reason}")
    if result.flagged:
        print()
        print(f"已排除（{len(result.flagged)} 只）："
              + "、".join(f"{c.symbol}" for c in result.flagged))
    print()
    for note in result.notes[:3]:
        print(f"· {note}")
    print()
    for kind, path in paths.items():
        print(f"{kind:<12} {path}")


def _print_backtest_summary(bt) -> None:
    print(f"回放区间 {bt['start']} ~ {bt['end']}，共 {bt['sessions_used']} 个交易日，"
          f"入选记录 {bt['picks']} 条")
    for group, summary in bt["by_group"].items():
        entry = summary.get("horizon_20d") or {}
        if not entry.get("samples"):
            print(f"  {group}：样本不足")
            continue
        print(
            f"  {group}：20 日样本 {entry['samples']}，平均 {entry['mean']*100:+.2f}%，"
            f"胜率 {entry['hit_rate']*100:.1f}%，平均超额 {entry['mean_excess_vs_spy']*100:+.2f}%，"
            f"平均最大不利波动 {entry['mean_max_adverse']*100:.2f}%"
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "init-db": cmd_init_db,
        "doctor": cmd_doctor,
        "scan": cmd_scan,
        "demo": cmd_demo,
        "report": cmd_report,
        "outcomes": cmd_outcomes,
        "history": cmd_history,
        "backtest": cmd_backtest,
        "serve": cmd_serve,
        "site": cmd_site,
        "notify": cmd_notify,
        "lab": cmd_lab,
    }
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except DataProviderError as exc:
        print(f"[数据源错误] {exc}", file=sys.stderr)
        return EXIT_PROVIDER
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底
        print(f"[运行失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
