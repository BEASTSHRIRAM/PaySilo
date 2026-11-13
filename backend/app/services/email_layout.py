"""One shared, email client safe layout for every PaySilo email, plus the small formatters templates use.

Table based with inline CSS (a small dark mode media query is a progressive extra), 560px wide, system fonts,
the PaySilo wordmark as TEXT (no remote images, no tracking pixels), the ink header bar, a coral button with
dark text (AA contrast) and a muted footer that says why the recipient got the mail. The plain text twin is
built from the SAME parts, so the two never drift apart.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .email_style import MAX_SUBJECT

INK, INK_DEEP, CORAL, SAND, PAPER = "#244855", "#0C1D23", "#EC6A52", "#FBE9D0", "#F4EFE6"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


def esc(value: object) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def safe_url(url: str) -> str:
    """Only plain http(s) links are ever put in a button: built by code from APP_BASE_URL, never user text."""
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s\"'<>]+$", url):
        raise ValueError("Email links must be http(s) URLs.")
    return url


def link_ok(url: object) -> bool:
    try:
        safe_url(str(url or ""))
        return True
    except ValueError:
        return False


def money(currency: object, amount: object) -> str:
    """'EUR 3,400.00': always a currency code and two decimals. Unparseable amounts pass through as text."""
    cur = "" if currency is None else str(currency).strip()
    try:
        value = f"{Decimal(str(amount).replace(',', '')):,.2f}"
    except (InvalidOperation, ValueError):
        value = "" if amount is None else str(amount)
    return f"{cur} {value}".strip()


def nice_date(value: object, default: str = "") -> str:
    """'13 Nov 2026' from an ISO date, ISO datetime, date or datetime."""
    if value is None or value == "":
        return default
    try:
        if isinstance(value, datetime):
            d = value.date()
        elif isinstance(value, date):
            d = value
        else:
            d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{d.day} {d.strftime('%b %Y')}"


def clamp_subject(subject: str) -> str:
    subject = " ".join(subject.split())
    if len(subject) <= MAX_SUBJECT:
        return subject
    cut = subject[:MAX_SUBJECT].rsplit(" ", 1)[0] or subject[:MAX_SUBJECT]
    return cut.rstrip(" ,.:;")


@dataclass
class Mail:
    """What a template says. ``compose`` turns it into (subject, text, html)."""

    subject: str
    org: str
    reason: str
    name: str | None = None
    paras: list[str] = field(default_factory=list)
    rows: list[tuple[str, str]] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    code: str | None = None
    button: tuple[str, str] | None = None
    after: list[str] = field(default_factory=list)
    signer: str | None = None
    preheader: str | None = None
    footer_org: bool = True


def _text(m: Mail) -> str:
    parts = [f"Hello {m.name}," if m.name else "Hello,"]
    parts += m.paras
    if m.code:
        parts.append(f"Your code: {m.code}")
    if m.rows:
        parts.append("\n".join(f"  {k}: {v}" for k, v in m.rows))
    if m.steps:
        parts.append("\n".join(f"{i}. {s}" for i, s in enumerate(m.steps, 1)))
    if m.button and link_ok(m.button[1]):
        parts.append(f"{m.button[0]}: {safe_url(m.button[1])}")
    parts += m.after
    parts.append(f"Thank you,\n{m.signer or m.org}")
    footer = m.reason + (f"\n{m.org} uses PaySilo to pay contractors." if m.footer_org else "")
    return "\n\n".join(parts) + "\n\n" + footer + "\n"


def _p(text: str) -> str:
    return (f'<p class="tx" style="margin:0 0 14px;font:15px/1.55 {FONT};color:#1A2A30">'
            f'{esc(text).replace(chr(10), "<br>")}</p>')


def _rows_html(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<tr><td class="mu rl" style="padding:8px 0;border-bottom:1px solid #E7E0D4;font:13px/1.4 {FONT};'
        f'color:#5B6F75">{esc(k)}</td><td class="tx rl" align="right" style="padding:8px 0;border-bottom:1px solid '
        f'#E7E0D4;font:600 14px/1.4 {FONT};color:#1A2A30">{esc(v)}</td></tr>' for k, v in rows)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:4px 0 18px">{body}</table>'


def _button_html(label: str, url: str) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 20px"><tr>'
            f'<td bgcolor="{CORAL}" style="border-radius:8px;background:{CORAL}"><a href="{esc(safe_url(url))}" '
            f'style="display:inline-block;padding:13px 24px;font:700 15px/1 {FONT};color:{INK_DEEP};'
            f'text-decoration:none;border-radius:8px">{esc(label)}</a></td></tr></table>')


_DARK_CSS = (
    "@media (prefers-color-scheme: dark){.bg{background:#0C1D23!important}.card{background:#11262E!important}"
    ".tx{color:#F3EDE3!important}.mu{color:#9DB4B8!important}.rl{border-color:#244855!important}"
    ".codebox{background:#15303A!important;color:#F3EDE3!important}}")


def layout(subject: str, inner: str, *, reason: str, org: str, preheader: str = "", footer_org: bool = True) -> str:
    foot = esc(reason) + (f"<br>{esc(org)} uses PaySilo to pay contractors." if footer_org else "")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
        f"<title>{esc(subject)}</title><style>{_DARK_CSS}</style></head>"
        f'<body class="bg" style="margin:0;padding:0;background:{PAPER}">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{PAPER}">{esc(preheader)}</div>'
        f'<table role="presentation" class="bg" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER}">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">'
        f'<tr><td style="background:{INK};padding:18px 24px;border-radius:10px 10px 0 0;font:700 20px/1 {FONT};'
        'color:#FFFFFF;letter-spacing:.3px">PaySilo</td></tr>'
        f'<tr><td class="card" style="background:#FFFFFF;padding:28px 24px 12px;border-radius:0 0 10px 10px">{inner}</td></tr>'
        f'<tr><td class="mu" style="padding:16px 8px 0;font:12px/1.5 {FONT};color:#5B6F75">{foot}</td></tr>'
        "</table></td></tr></table></body></html>")


def _html_body(m: Mail) -> str:
    out = [_p(f"Hello {m.name}," if m.name else "Hello,")]
    out += [_p(p) for p in m.paras]
    if m.code:
        out.append(f'<div class="codebox" style="margin:6px 0 18px;padding:14px 18px;border-radius:8px;text-align:center;'
                   f'background:{SAND};color:{INK_DEEP};font:700 32px/1.2 {MONO};letter-spacing:8px">{esc(m.code)}</div>')
    if m.rows:
        out.append(_rows_html(m.rows))
    if m.steps:
        items = "".join(f'<li style="margin:0 0 6px">{esc(s)}</li>' for s in m.steps)
        out.append(f'<ol class="tx" style="margin:0 0 16px;padding-left:22px;font:15px/1.55 {FONT};color:#1A2A30">{items}</ol>')
    if m.button and link_ok(m.button[1]):
        out.append(_button_html(*m.button))
    out += [_p(p) for p in m.after]
    out.append(_p(f"Thank you,\n{m.signer or m.org}"))
    return "".join(out)


def compose(m: Mail) -> tuple[str, str, str]:
    subject = clamp_subject(m.subject)
    pre = m.preheader or (m.paras[0] if m.paras else subject)
    html = layout(subject, _html_body(m), reason=m.reason, org=m.org, preheader=pre[:110], footer_org=m.footer_org)
    return subject, _text(m), html


def render_html(text: str, link: tuple[str, str] | None = None, org: str = "PaySilo", *,
                reason: str = "You received this email because of activity on your PaySilo account.",
                subject: str = "PaySilo") -> str:
    """Wrap ready made plain text (for example the funding drafts) in the shared layout."""
    blocks = []
    for para in [p for p in text.strip().split("\n\n") if p.strip()]:
        lines = para.split("\n")
        figures = [ln for ln in lines if ln.startswith("  ")]
        prose = [ln for ln in lines if not ln.startswith("  ")]
        if prose:
            blocks.append(_p("\n".join(prose)))
        if figures:
            rows = []
            for ln in figures:
                k, _, v = ln.strip().partition(": ")
                rows.append((k, v) if v else (ln.strip(), ""))
            blocks.append(_rows_html(rows))
    if link and link_ok(link[1]):
        blocks.append(_button_html(*link))
    pre = next((p.strip() for p in text.split("\n\n")[1:2]), "")
    return layout(subject, "".join(blocks), reason=reason, org=org, preheader=pre[:110])


_DASHY = re.compile(r"\s+[\-–—]+\s+|[–—]")
_WORD_HYPHEN = re.compile(r"(?<=[A-Za-z])-(?=[A-Za-z])")


def tidy(text: object, limit: int = 300) -> str:
    """House style for free text that comes from elsewhere (error reasons, workflow details): no dashes, no
    hyphenated words, no exclamation marks, capped. Links and addresses are left alone."""
    out = []
    for token in ("" if text is None else str(text)).split(" "):
        if "://" in token or "@" in token:
            out.append(token)
        else:
            out.append(_WORD_HYPHEN.sub(" ", token.replace("!", ".")))
    joined = _DASHY.sub(", ", " ".join(out))
    return " ".join(joined.split())[:limit]
