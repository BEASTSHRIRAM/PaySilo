"""Platform email — a recorded-first outbox with a pluggable transport.

The outbox law:

- :func:`enqueue_email` renders a template from a context snapshot and
  records an :class:`~app.db.models.EmailOutbox` row. It NEVER sends.
  Rendered amounts come from the context snapshot verbatim — templates
  never recompute money.
- Until SMTP is configured (``SMTP_HOST`` + ``EMAIL_FROM``), enqueued
  rows are recorded as ``NOT_CONFIGURED``. That is a final recorded
  state until configuration exists — the row is the honest evidence,
  and nothing is ever fake-sent. With configuration, rows start as
  ``QUEUED``.
- :func:`drain_outbox` is the sender worker. It sends ``QUEUED`` rows
  only, flips each row to ``SENT`` (with ``sent_at``) or ``FAILED``
  (with ``error_detail``), and commits each outcome immediately — the
  status flip is the lock, so a row is sent at most once and a second
  drain sends nothing more. ``FAILED`` is terminal for that row: a
  later drain does NOT retry it (retries only ever touch ``QUEUED``
  rows); re-notification means enqueueing a new row.

The transport is provider-agnostic: :class:`MailTransport` is the
interface, :class:`SmtpTransport` (stdlib ``smtplib``) is the built-in
implementation, and a future API provider only has to implement the
same ``send`` method. Secrets come from settings/environment only and
are never stored on outbox rows or exposed by the API.
"""

from __future__ import annotations

import html
import smtplib
import ssl
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import EMAIL_RELATED_KINDS, EmailOutbox, utcnow

BLOCKED_COMPONENT_WORDING = (
    "Not yet booked — exceeds the current credential limit; "
    "the team will arrange this separately"
)

_PURCHASED_STATUSES = ("PURCHASED", "SUCCEEDED", "BOOKED", "CONFIRMED")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _read(source: Any, *names: str) -> Any:
    """Read the first present attribute/key from a settings-like object."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        for name in names:
            if source.get(name) is not None:
                return source.get(name)
        return None
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


@dataclass(frozen=True)
class EmailSettings:
    """The SMTP slice of the application settings.

    Build one from the full application :class:`~app.config.Settings`
    with :meth:`from_settings`. Field names mirror the environment
    variables: ``SMTP_HOST``, ``SMTP_PORT``, ``SMTP_USER``,
    ``SMTP_PASSWORD`` and ``EMAIL_FROM``.
    """

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_from: str | None = None

    @property
    def from_address(self) -> str | None:
        return self.email_from

    @classmethod
    def from_settings(cls, settings: Any) -> "EmailSettings":
        if isinstance(settings, cls):
            return settings
        port = _read(settings, "smtp_port", "SMTP_PORT")
        return cls(
            smtp_host=_read(settings, "smtp_host", "SMTP_HOST"),
            smtp_port=int(port) if port not in (None, "") else 587,
            smtp_user=_read(settings, "smtp_user", "SMTP_USER"),
            smtp_password=_read(settings, "smtp_password", "SMTP_PASSWORD"),
            email_from=_read(settings, "email_from", "EMAIL_FROM"),
        )


def email_configured(settings: Settings | EmailSettings | Any) -> bool:
    """True when email can actually be sent: SMTP host AND From are set."""
    cfg = EmailSettings.from_settings(settings)
    return bool(cfg.smtp_host and str(cfg.smtp_host).strip()
                and cfg.email_from and str(cfg.email_from).strip())


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class MailTransport(Protocol):
    """Provider-agnostic mail transport. ``send`` raises on failure."""

    def send(self, *, to: str, subject: str, body_text: str, body_html: str) -> None:
        ...


class SmtpTransport:
    """SMTP transport over stdlib ``smtplib`` — no third-party deps.

    Port 465 uses implicit TLS (``SMTP_SSL``); every other port uses
    STARTTLS. Login happens only when an SMTP user is configured.
    """

    def __init__(self, settings: Any = None, *, host: str | None = None,
                 port: int | None = None, user: str | None = None,
                 username: str | None = None, password: str | None = None,
                 from_address: str | None = None, email_from: str | None = None,
                 timeout: float = 30) -> None:
        if isinstance(settings, str):  # a bare host name was passed
            settings = EmailSettings(smtp_host=settings)
        cfg = EmailSettings.from_settings(settings) if settings is not None else EmailSettings()
        self.host = host if host is not None else cfg.smtp_host
        self.port = int(port) if port is not None else cfg.smtp_port
        self.user = user if user is not None else (username if username is not None else cfg.smtp_user)
        self.password = password if password is not None else cfg.smtp_password
        self.from_address = (from_address if from_address is not None
                             else (email_from if email_from is not None else cfg.email_from))
        self.timeout = timeout

    def send(self, *, to: str, subject: str, body_text: str, body_html: str) -> None:
        if not (self.host and self.from_address):
            raise RuntimeError("SMTP is not configured (SMTP_HOST and EMAIL_FROM are required).")
        message = EmailMessage()
        message["From"] = str(self.from_address)
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body_text or "")
        if body_html:
            message.add_alternative(body_html, subtype="html")
        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context) as server:
                if self.user:
                    server.login(self.user, self.password or "")
                server.send_message(message)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                server.starttls(context=context)
                if self.user:
                    server.login(self.user, self.password or "")
                server.send_message(message)


# ---------------------------------------------------------------------------
# Templates
#
# A template is a (subject_fn, render_fn) pair: subject_fn(context) ->
# subject string, render_fn(context) -> (subject, body_text, body_html).
# Every figure in the output is a context value rendered verbatim with
# str() — no formatting, rounding or arithmetic ever happens here.
# ---------------------------------------------------------------------------

def _v(context: Mapping[str, Any], key: str, default: str = "") -> str:
    value = context.get(key)
    return default if value is None else str(value)


def _money(currency: Any, amount: Any) -> str:
    """``<currency> <amount>`` with the amount string used verbatim."""
    amount_str = "" if amount is None else str(amount)
    currency_str = "" if currency is None else str(currency)
    return f"{currency_str} {amount_str}".strip()


def _layout(title: str, content_html: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{html.escape(title)}</title>\n</head>\n"
        "<body style=\"margin:0;padding:0;background-color:#f4f6f7;"
        "font-family:Arial,Helvetica,sans-serif;color:#1c2b31;\">\n"
        "<div style=\"max-width:640px;margin:0 auto;background-color:#ffffff;\">\n"
        "<div style=\"background-color:#244855;padding:20px 28px;\">\n"
        "<div style=\"color:#ffffff;font-size:22px;font-weight:bold;letter-spacing:0.5px;\">PaySilo</div>\n"
        f"<div style=\"color:#cfdde2;font-size:13px;margin-top:4px;\">{html.escape(title)}</div>\n"
        "</div>\n"
        f"<div style=\"padding:24px 28px;font-size:15px;line-height:1.55;\">{content_html}</div>\n"
        "<div style=\"padding:16px 28px;border-top:1px solid #dde5e8;color:#5b6d73;font-size:12px;\">"
        "PaySilo — Multi-Employer Payroll Executor. This message was generated by PaySilo; "
        "the figures above are recorded exactly as approved."
        "</div>\n</div>\n</body>\n</html>"
    )


def _p(text: str) -> str:
    return f"<p style=\"margin:0 0 12px;\">{html.escape(text)}</p>"


def _ul(items: list[str]) -> str:
    lis = "".join(f"<li style=\"margin-bottom:6px;\">{html.escape(item)}</li>" for item in items)
    return f"<ul style=\"margin:0 0 12px;padding-left:20px;\">{lis}</ul>"


def _make_entry(subject_fn: Callable[[Mapping[str, Any]], str],
                builder: Callable[[Mapping[str, Any], str], tuple[str, str]]):
    def render_fn(context: Mapping[str, Any]) -> tuple[str, str, str]:
        subject = subject_fn(context)
        body_text, content_html = builder(context, subject)
        return subject, body_text, _layout(subject, content_html)

    return subject_fn, render_fn


def _approval_subject(ctx: Mapping[str, Any]) -> str:
    entity = _v(ctx, "entity_label", "Approval")
    total = _money(ctx.get("currency"), ctx.get("total"))
    return f"Approval requested: {entity} — {total}" if total else f"Approval requested: {entity}"


def _approval_body(ctx: Mapping[str, Any], _subject: str) -> tuple[str, str]:
    currency = ctx.get("currency")
    lines: list[str] = []
    for line in ctx.get("lines") or []:
        if isinstance(line, Mapping):
            lines.append(f"{_v(line, 'label')} — {_money(line.get('currency', currency), line.get('amount'))}")
        else:
            lines.append(str(line))
    facts = [
        f"Entity: {_v(ctx, 'entity_label')}",
        f"Employer: {_v(ctx, 'employer_name')}",
    ]
    if ctx.get("requested_for"):
        facts.append(f"Requested for: {_v(ctx, 'requested_for')}")
    text_parts = [
        "Approval requested",
        "",
        *facts,
        "",
        "Summary:",
        *[f"- {line}" for line in lines],
        "",
        f"Total: {_money(currency, ctx.get('total'))}",
        "",
        f"Review and decide: {_v(ctx, 'link_path')}",
    ]
    if ctx.get("expires_at"):
        text_parts.append(f"This approval request expires at {_v(ctx, 'expires_at')}. "
                          "After that time a new request is required.")
    text_parts.append("If you were not expecting this request, do not approve it.")
    html_parts = [_p("A purchase needs your decision before it can proceed.")]
    html_parts.append(_ul(facts))
    html_parts.append(_p("Summary:"))
    html_parts.append(_ul(lines))
    html_parts.append(_p(f"Total: {_money(currency, ctx.get('total'))}"))
    html_parts.append(_p(f"Review and decide: {_v(ctx, 'link_path')}"))
    if ctx.get("expires_at"):
        html_parts.append(_p(f"This approval request expires at {_v(ctx, 'expires_at')}. "
                             "After that time a new request is required."))
    html_parts.append(_p("If you were not expecting this request, do not approve it."))
    return "\n".join(text_parts), "".join(html_parts)


def _kit_subject(ctx: Mapping[str, Any]) -> str:
    return f"Onboarding kit complete — {_v(ctx, 'employer_name', 'PaySilo')}"


def _kit_body(ctx: Mapping[str, Any], _subject: str) -> tuple[str, str]:
    items: list[str] = []
    for item in ctx.get("items") or []:
        if not isinstance(item, Mapping):
            items.append(str(item))
            continue
        parts = [f"{_v(item, 'label', _v(item, 'item'))} — {_money(item.get('currency'), item.get('amount'))}"]
        if item.get("provider_ref"):
            parts.append(f"Provider reference: {_v(item, 'provider_ref')}")
        if item.get("mode"):
            parts.append(f"Mode: {_v(item, 'mode')}")
        items.append(" · ".join(parts))
    greeting = f"Hello {_v(ctx, 'contractor_name')},"
    intro = (f"The following onboarding kit items have been purchased for you "
             f"by {_v(ctx, 'employer_name')}:")
    text = "\n".join([
        greeting,
        "",
        intro,
        *[f"- {item}" for item in items],
        "",
        f"Total: {_v(ctx, 'total')}",
        "",
        "Keep this message as your record of the completed kit.",
    ])
    body_html = "".join([
        _p(greeting),
        _p(intro),
        _ul(items),
        _p(f"Total: {_v(ctx, 'total')}"),
        _p("Keep this message as your record of the completed kit."),
    ])
    return text, body_html


def _component_line(component: Mapping[str, Any]) -> str:
    label = _v(component, "label", _v(component, "item"))
    kind = _v(component, "kind")
    prefix = label + (f" ({kind})" if kind else "")
    amount = _money(component.get("currency"), component.get("amount")) if component.get("amount") is not None else ""
    if amount:
        prefix = f"{prefix} — {amount}"
    status_raw = _v(component, "status")
    status = status_raw.strip().upper()
    if status == "BLOCKED":
        return f"{prefix} — {BLOCKED_COMPONENT_WORDING}"
    if status in _PURCHASED_STATUSES:
        line = f"{prefix} — Booked"
        if component.get("provider_ref"):
            line += f" — Provider reference: {_v(component, 'provider_ref')}"
        return line
    line = prefix
    if status_raw:
        line += f" — Status: {status_raw}"
    if component.get("provider_ref"):
        line += f" — Provider reference: {_v(component, 'provider_ref')}"
    return line


def _trip_subject(ctx: Mapping[str, Any]) -> str:
    return f"Trip itinerary: {_v(ctx, 'destination')} ({_v(ctx, 'start_date')} to {_v(ctx, 'end_date')})"


def _trip_body(ctx: Mapping[str, Any], _subject: str) -> tuple[str, str]:
    components = [_component_line(c) for c in (ctx.get("components") or []) if isinstance(c, Mapping)]
    greeting = f"Hello {_v(ctx, 'contractor_name')},"
    intro = (f"Your trip to {_v(ctx, 'destination')} runs from {_v(ctx, 'start_date')} "
             f"to {_v(ctx, 'end_date')}. Component status:")
    text = "\n".join([
        greeting,
        "",
        intro,
        *[f"- {line}" for line in components],
        "",
        "If any detail above looks wrong, contact the team before you travel.",
    ])
    body_html = "".join([
        _p(greeting),
        _p(intro),
        _ul(components),
        _p("If any detail above looks wrong, contact the team before you travel."),
    ])
    return text, body_html


def _receipt_subject(ctx: Mapping[str, Any]) -> str:
    return f"Purchase receipt: {_v(ctx, 'item_label')}"


def _receipt_body(ctx: Mapping[str, Any], _subject: str) -> tuple[str, str]:
    facts = [f"Buyer: {_v(ctx, 'buyer_label')}",
             f"Item: {_v(ctx, 'item_label')}",
             f"Merchant: {_v(ctx, 'merchant')}",
             f"Amount: {_money(ctx.get('currency'), ctx.get('amount'))}"]
    if ctx.get("provider_ref"):
        facts.append(f"Provider reference: {_v(ctx, 'provider_ref')}")
    if ctx.get("mode"):
        facts.append(f"Mode: {_v(ctx, 'mode')}")
    if ctx.get("book_label"):
        facts.append(f"Recorded in book: {_v(ctx, 'book_label')}")
    closing = "This receipt records the purchase figures exactly as approved and booked."
    text = "\n".join(["Purchase receipt", "", *facts, "", closing])
    body_html = "".join([_p("Purchase receipt"), _ul(facts), _p(closing)])
    return text, body_html


def _welcome_subject(ctx: Mapping[str, Any]) -> str:
    return f"Welcome to PaySilo, {_v(ctx, 'employer_name')}"


def _welcome_body(ctx: Mapping[str, Any], _subject: str) -> tuple[str, str]:
    employer = _v(ctx, "employer_name")
    intro = (f"Your employer account for {employer} is ready in PaySilo. "
             "PaySilo runs payroll for many employers on one platform while "
             "keeping every employer's money in its own separate silo — your "
             "funds are never mixed with another employer's, and payroll for "
             f"{employer} can only ever draw on {employer}'s own balance.")
    steps = [
        "Fund your silo before your first payroll date (your balance, and only your balance, pays your contractors).",
        "Add your contractors — an onboarding kit workflow can be drafted for each new joiner.",
        "Approve each payroll plan before it executes: nothing is paid without a human approving the exact figures.",
    ]
    closing = ("If anything above is unexpected, contact the PaySilo team before "
               "funding the account.")
    text = "\n".join([f"Welcome to PaySilo, {employer}", "", intro, "",
                      "Next steps:", *[f"- {s}" for s in steps], "", closing])
    body_html = "".join([_p(f"Welcome to PaySilo, {employer}"), _p(intro),
                         _p("Next steps:"), _ul(steps), _p(closing)])
    return text, body_html


def _payslip_subject(ctx: Mapping[str, Any]) -> str:
    return (f"Your payslip from {_v(ctx, 'employer_name')} — "
            f"{_money(ctx.get('currency'), ctx.get('amount'))}")


def _payslip_body(ctx: Mapping[str, Any], _subject: str) -> tuple[str, str]:
    greeting = f"Hello {_v(ctx, 'contractor_name')},"
    facts = [f"Employer: {_v(ctx, 'employer_name')}",
             f"Amount paid: {_money(ctx.get('currency'), ctx.get('amount'))}"]
    link = ctx.get("receipt_link") or ctx.get("receipt_path")
    closing = ("Your worker receipt shows the full breakdown of this payment"
               + (f" — open it here: {link}" if link else "."))
    text = "\n".join([greeting, "", *facts, "", closing])
    body_html = "".join([_p(greeting), _ul(facts), _p(closing)])
    return text, body_html


def _held_subject(ctx: Mapping[str, Any]) -> str:
    return (f"Payroll held: {_v(ctx, 'run_label')} — "
            f"shortfall {_money(ctx.get('currency'), ctx.get('shortfall'))}")


def _held_body(ctx: Mapping[str, Any], _subject: str) -> tuple[str, str]:
    facts = [
        f"Employer: {_v(ctx, 'employer_name')}",
        f"Run: {_v(ctx, 'run_label')}",
        f"Required total: {_money(ctx.get('currency'), ctx.get('required_total'))}",
        f"Shortfall: {_money(ctx.get('currency'), ctx.get('shortfall'))}",
    ]
    next_steps = ("What happens next: no payroll money has moved for this run. "
                  "Add funds to cover the shortfall, then ask the PaySilo team to re-check "
                  "the run; it stays held until the employer's own balance covers the "
                  "full required total.")
    text = "\n".join(["Payroll run held", "", *facts, "", next_steps])
    body_html = "".join([_p("Payroll run held"), _ul(facts), _p(next_steps)])
    return text, body_html


TEMPLATES: dict[str, tuple[Callable[[Mapping[str, Any]], str],
                           Callable[[Mapping[str, Any]], tuple[str, str, str]]]] = {
    "approval_requested": _make_entry(_approval_subject, _approval_body),
    "kit_completed": _make_entry(_kit_subject, _kit_body),
    "trip_itinerary": _make_entry(_trip_subject, _trip_body),
    "purchase_receipt": _make_entry(_receipt_subject, _receipt_body),
    "payroll_held_alert": _make_entry(_held_subject, _held_body),
    "employer_welcome": _make_entry(_welcome_subject, _welcome_body),
    "payslip_receipt": _make_entry(_payslip_subject, _payslip_body),
}


def render_template(template: str, context: Mapping[str, Any] | None) -> tuple[str, str, str]:
    """Render ``template`` against ``context`` -> (subject, body_text, body_html)."""
    entry = TEMPLATES.get(template)
    if entry is None:
        raise ValueError(f"Unknown email template: {template!r}. Known templates: {sorted(TEMPLATES)}.")
    _subject_fn, render_fn = entry
    return render_fn(dict(context or {}))


# ---------------------------------------------------------------------------
# Outbox operations
# ---------------------------------------------------------------------------

def enqueue_email(s: Session, settings: Settings | EmailSettings | Any, *,
                  organization_id: uuid.UUID | str | None = None,
                  template: str,
                  to: str | None = None,
                  context: Mapping[str, Any] | None = None,
                  related_kind: str = "other",
                  related_id: uuid.UUID | str | int | None = None,
                  related_label: str = "",
                  **kwargs: Any) -> EmailOutbox:
    """Render and record an email. NEVER sends.

    The row is ``QUEUED`` when SMTP is configured and ``NOT_CONFIGURED``
    when it is not. The row is flushed, not committed, so callers can
    enqueue inside their own transaction (e.g. alongside a purchase).
    """
    if to is None:
        to = kwargs.pop("to_address", None) or kwargs.pop("recipient", None)
    if kwargs:
        raise TypeError(f"Unexpected enqueue_email arguments: {sorted(kwargs)}")
    if not to:
        raise ValueError("enqueue_email requires a recipient (to).")
    if related_kind not in EMAIL_RELATED_KINDS:
        raise ValueError(f"related_kind must be one of {EMAIL_RELATED_KINDS}, got {related_kind!r}.")
    subject, body_text, body_html = render_template(template, context)
    org_id: uuid.UUID | None = None
    if organization_id is not None:
        org_id = organization_id if isinstance(organization_id, uuid.UUID) else uuid.UUID(str(organization_id))
    row = EmailOutbox(
        organization_id=org_id,
        to_address=str(to).strip(),
        template=template,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        status="QUEUED" if email_configured(settings) else "NOT_CONFIGURED",
        related_label=str(related_label or ""),
        related_kind=related_kind,
        related_id=None if related_id is None else str(related_id),
    )
    s.add(row)
    s.flush()
    return row


def drain_outbox(s: Session, settings: Settings | EmailSettings | Any,
                 transport: MailTransport | None = None, *,
                 limit: int = 50,
                 mail_transport: MailTransport | None = None) -> dict:
    """Send QUEUED outbox rows through ``transport`` (default: SMTP).

    Idempotency: only ``QUEUED`` rows are touched and each outcome is
    committed as soon as it is known, so a row is sent at most once and
    a second drain sends nothing more. ``FAILED`` is terminal for a
    row — a later drain does NOT retry it; re-notification creates a
    new row via :func:`enqueue_email`.

    When SMTP is not configured this returns
    ``{"skipped": "not_configured"}`` and changes nothing.
    """
    if not email_configured(settings):
        return {"skipped": "not_configured"}
    active_transport: MailTransport = transport or mail_transport or SmtpTransport(settings)
    rows = list(s.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.status == "QUEUED")
        .order_by(EmailOutbox.created_at.asc(), EmailOutbox.id.asc())
        .limit(limit)))
    sent = 0
    failed = 0
    for row in rows:
        if row.status != "QUEUED":  # defensive: the status flip is the lock
            continue
        try:
            active_transport.send(to=row.to_address, subject=row.subject,
                                  body_text=row.body_text, body_html=row.body_html)
        except Exception as exc:  # noqa: BLE001 — any transport failure is recorded, not raised
            row.status = "FAILED"
            row.error_detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            row.sent_at = None
            failed += 1
        else:
            row.status = "SENT"
            row.sent_at = utcnow()
            row.error_detail = None
            sent += 1
        row.updated_at = utcnow()
        s.commit()
    return {"sent": sent, "failed": failed, "processed": sent + failed}


__all__ = [
    "BLOCKED_COMPONENT_WORDING",
    "EmailSettings",
    "MailTransport",
    "SmtpTransport",
    "TEMPLATES",
    "drain_outbox",
    "email_configured",
    "enqueue_email",
    "render_template",
]
