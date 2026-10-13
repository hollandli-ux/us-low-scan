"""通知（阶段 4）：把每天的名单发出去。

支持三种渠道，全部只用标准库：

* **email**  SMTP + STARTTLS，密码走环境变量 ``SMTP_PASSWORD``
* **webhook** 通用 JSON POST，内置 Slack / 企业微信 / 钉钉 / Telegram 的载荷格式
* **macos**  macOS 本地通知（osascript）

设计上的两个细节：
1. ``--dry-run`` 只打印将要发送的内容，绝不真的发出去，方便先调格式；
2. 任何一个渠道失败都不影响其它渠道，也不会让当天的 scan 失败
   （通知挂了不该让数据流程挂掉）。
"""

from __future__ import annotations

import json
import smtplib
import ssl
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple

from . import report
from .models import ScanResult

WEBHOOK_KINDS = ("generic", "slack", "wecom", "dingtalk", "telegram")


# --------------------------------------------------------------------------
# 消息内容
# --------------------------------------------------------------------------
def build_message(scan: ScanResult, settings) -> Tuple[str, str]:
    """返回 (标题, 正文)。正文是纯文本，邮件与 IM 都能直接用。"""
    count = len(scan.stabilized)
    subject = f"[美股低位观察] {scan.session.isoformat()} · 主要候选 {count} 只"
    if scan.skipped_reason:
        subject = f"[美股低位观察] {scan.session.isoformat()} · 跳过运行"

    body = report.render_brief(scan, settings)
    if settings.notify.include_reasons and scan.stabilized:
        body += "\n\n入选理由\n" + "-" * 32 + "\n"
        for candidate in scan.stabilized:
            body += f"\n{candidate.symbol}（{report.score_line(candidate)}）\n"
            for reason in candidate.reasons:
                body += f"  · {reason}\n"
    body += (
        "\n"
        + "-" * 32
        + "\n观察名单，不是投资建议，也不代表上涨概率。\n"
        + f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )
    return subject, body


def should_notify(scan: ScanResult, settings) -> bool:
    minimum = settings.notify.min_main_candidates
    if scan.skipped_reason:
        return minimum <= 0
    return len(scan.stabilized) >= minimum


# --------------------------------------------------------------------------
# 载荷格式
# --------------------------------------------------------------------------
def format_webhook_payload(
    kind: str, subject: str, body: str, chat_id: Optional[str] = None
) -> Dict[str, object]:
    if kind == "slack":
        return {"text": f"*{subject}*\n```{body}```"}
    if kind in ("wecom", "dingtalk"):
        return {"msgtype": "text", "text": {"content": f"{subject}\n{body}"}}
    if kind == "telegram":
        return {"chat_id": chat_id, "text": f"{subject}\n\n{body}", "disable_web_page_preview": True}
    return {"title": subject, "text": body, "source": "us-low-scan"}


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------
def send_webhook(
    settings,
    subject: str,
    body: str,
    dry_run: bool = False,
    payload_preview: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    cfg = settings.notify
    payload = format_webhook_payload(cfg.webhook_kind, subject, body, cfg.telegram_chat_id)
    if payload_preview is not None:
        payload_preview.update(payload)
    if dry_run:
        return {
            "channel": "webhook",
            "status": "dry-run",
            "kind": cfg.webhook_kind,
            "url": cfg.webhook_url or "(未配置)",
            "payload": payload,
        }

    if not cfg.webhook_url:
        return {"channel": "webhook", "status": "skipped", "detail": "未配置 webhook_url"}

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        cfg.webhook_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return {"channel": "webhook", "status": "sent", "code": response.status}
    except urllib.error.HTTPError as exc:
        return {"channel": "webhook", "status": "failed", "detail": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - 通知失败不能影响主流程
        return {"channel": "webhook", "status": "failed", "detail": str(exc)}


def send_email(
    settings, subject: str, body: str, dry_run: bool = False
) -> Dict[str, object]:
    cfg = settings.notify
    if dry_run:
        return {
            "channel": "email",
            "status": "dry-run",
            "host": cfg.smtp_host or "(未配置)",
            "port": cfg.smtp_port,
            "from": cfg.mail_from or "(未配置)",
            "to": cfg.mail_to,
            "subject": subject,
            "body": body,
        }
    if not (cfg.smtp_host and cfg.mail_to):
        return {"channel": "email", "status": "skipped", "detail": "未配置 smtp_host / mail_to"}

    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = cfg.mail_from or cfg.smtp_username
    message["To"] = ", ".join(cfg.mail_to)

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as server:
            if cfg.smtp_use_tls:
                server.starttls(context=ssl.create_default_context())
            if cfg.smtp_username:
                server.login(cfg.smtp_username, cfg.smtp_password)
            server.sendmail(message["From"], cfg.mail_to, message.as_string())
        return {"channel": "email", "status": "sent", "to": cfg.mail_to}
    except Exception as exc:  # noqa: BLE001
        return {"channel": "email", "status": "failed", "detail": str(exc)}


def send_macos(subject: str, body: str, dry_run: bool = False) -> Dict[str, object]:
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    script = (
        f'display notification {json.dumps(first_line)} '
        f'with title {json.dumps(subject)}'
    )
    if dry_run:
        return {"channel": "macos", "status": "dry-run", "script": script}
    try:
        subprocess.run(["osascript", "-e", script], check=True,
                       capture_output=True, timeout=10)
        return {"channel": "macos", "status": "sent"}
    except Exception as exc:  # noqa: BLE001
        return {"channel": "macos", "status": "failed", "detail": str(exc)}


def notify(
    settings,
    scan: Optional[ScanResult],
    channels: Optional[List[str]] = None,
    dry_run: bool = False,
    force: bool = False,
) -> List[Dict[str, object]]:
    """按配置发送通知，返回每个渠道的结果（便于 CLI 打印与测试断言）。"""
    if scan is None:
        return [{"channel": "-", "status": "skipped", "detail": "没有可发送的扫描结果"}]

    cfg = settings.notify
    selected = [c.lower() for c in (channels or cfg.channel_list())]
    if not selected:
        return [{"channel": "-", "status": "skipped", "detail": "未配置任何通知渠道"}]
    if not force and not should_notify(scan, settings):
        return [
            {
                "channel": "-",
                "status": "skipped",
                "detail": f"主要候选 {len(scan.stabilized)} 只，低于通知门槛 "
                          f"{cfg.min_main_candidates} 只",
            }
        ]

    subject, body = build_message(scan, settings)
    results: List[Dict[str, object]] = []
    for channel in selected:
        if channel == "email":
            results.append(send_email(settings, subject, body, dry_run=dry_run))
        elif channel == "webhook":
            results.append(send_webhook(settings, subject, body, dry_run=dry_run))
        elif channel == "macos":
            results.append(send_macos(subject, body, dry_run=dry_run))
        else:
            results.append({"channel": channel, "status": "unknown"})
    return results
