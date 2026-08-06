"""Email delivery for briefings.

Plain-stdlib SMTP so any provider works: Gmail (app password), Resend,
SendGrid, Mailgun, or a local relay. Credentials live in .env
(SERIN_SMTP_* / SERIN_EMAIL_*), never in the database.
"""

from __future__ import annotations

import html
import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from backend.config import settings
from backend.models import Briefing

STYLES = {
    "h1": "font-size:21px;font-weight:700;margin:0 0 14px;padding-bottom:12px;border-bottom:1px solid #d9e4df;color:#17231f",
    "h2": "font-size:13px;font-weight:600;letter-spacing:0.8px;text-transform:uppercase;color:#2f6f9f;margin:24px 0 10px",
    "h3": "font-size:14.5px;font-weight:600;margin:18px 0 8px;color:#17231f",
    "p": "margin:0 0 11px;color:#17231f",
    "list": "margin:0 0 14px;padding-left:22px;color:#17231f",
    "li": "margin-bottom:7px",
    "hr": "border:0;border-top:1px solid #d9e4df;margin:18px 0",
}


def _inline(text: str) -> str:
    escaped = html.escape(text)
    # Non-greedy so bold spans survive a literal '*' inside (e.g. "E*Trade").
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*([^*\s][^*]*)\*", r"<em>\1</em>", escaped)
    return escaped


def markdown_to_html(markdown: str) -> str:
    """Convert briefing markdown (headers, lists, hr, bold/italic) to HTML.

    Mirrors the frontend renderer in components/Markdown.jsx; keep in sync.
    """
    blocks: list[str] = []
    bullets: list[str] = []
    numbers: list[str] = []

    def flush() -> None:
        nonlocal bullets, numbers
        if bullets:
            items = "".join(f'<li style="{STYLES["li"]}">{_inline(item)}</li>' for item in bullets)
            blocks.append(f'<ul style="{STYLES["list"]}">{items}</ul>')
            bullets = []
        if numbers:
            items = "".join(f'<li style="{STYLES["li"]}">{_inline(item)}</li>' for item in numbers)
            blocks.append(f'<ol style="{STYLES["list"]}">{items}</ol>')
            numbers = []

    for raw in str(markdown or "").split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("- ") or line.startswith("* "):
            bullets.append(line[2:])
            continue
        ordered = re.match(r"^\d+\.\s+(.*)$", line)
        if ordered:
            numbers.append(ordered.group(1))
            continue
        flush()
        if re.fullmatch(r"-{3,}|\*{3,}", line):
            blocks.append(f'<hr style="{STYLES["hr"]}" />')
        elif line.startswith("### "):
            blocks.append(f'<h3 style="{STYLES["h3"]}">{_inline(line[4:])}</h3>')
        elif line.startswith("## "):
            blocks.append(f'<h2 style="{STYLES["h2"]}">{_inline(line[3:])}</h2>')
        elif line.startswith("# "):
            blocks.append(f'<h1 style="{STYLES["h1"]}">{_inline(line[2:])}</h1>')
        else:
            blocks.append(f'<p style="{STYLES["p"]}">{_inline(line)}</p>')
    flush()
    return "".join(blocks)


def briefing_subject(briefing: Briefing) -> str:
    try:
        day = datetime.fromisoformat(briefing.created_at.replace("Z", "+00:00"))
        label = day.strftime("%a, %b %-d %Y")
    except Exception:
        label = briefing.created_at[:10]
    return f"Serin Daily Briefing — {label}"


def render_briefing_email_html(briefing: Briefing) -> str:
    body = markdown_to_html(briefing.output_markdown)
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        "Roboto,Helvetica,Arial,sans-serif;max-width:680px;margin:0 auto;"
        'padding:8px 16px;line-height:1.6;font-size:14px">'
        f"{body}"
        f'<hr style="{STYLES["hr"]}" />'
        '<p style="color:#7b8c84;font-size:12px;margin:0">'
        "Sent by Serin · context &amp; organization — not investment advice</p>"
        "</div>"
    )


def _connect() -> smtplib.SMTP:
    mode = settings.smtp_tls.strip().lower()
    if mode == "auto":
        mode = "ssl" if settings.smtp_port == 465 else "starttls"
    if mode == "ssl":
        return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
    server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    if mode == "starttls":
        server.starttls()
    return server


def send_briefing_email(briefing: Briefing) -> str:
    """Send a completed briefing to SERIN_EMAIL_TO. Returns the recipient."""
    if not settings.email_configured:
        raise RuntimeError(
            "Email is not configured. Set SERIN_SMTP_HOST, SERIN_SMTP_USERNAME, "
            "SERIN_SMTP_PASSWORD, and SERIN_EMAIL_TO in .env, then restart Serin."
        )
    if briefing.status != "done" or not briefing.output_markdown:
        raise RuntimeError("Only completed briefings can be emailed.")

    message = MIMEMultipart("alternative")
    message["Subject"] = briefing_subject(briefing)
    message["From"] = settings.resolved_email_from
    message["To"] = settings.email_to
    message.attach(MIMEText(briefing.output_markdown, "plain", "utf-8"))
    message.attach(MIMEText(render_briefing_email_html(briefing), "html", "utf-8"))

    with _connect() as server:
        if settings.smtp_username and settings.smtp_password:
            server.login(settings.smtp_username, settings.smtp_password)
        server.sendmail(settings.resolved_email_from, [settings.email_to], message.as_string())
    return settings.email_to
