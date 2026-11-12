"""Email templates: one function per kind, each returning (subject, plain text, simple HTML).

Deterministic and palette-neutral (no images, inline basic styles). Every amount and date comes from the
caller's facts; nothing here decides anything. Money amounts are passed in already formatted.
"""

from __future__ import annotations

import html as _html

from ..crew import draft


def _h(text: str) -> str:
    return _html.escape(str(text), quote=True)


def render_html(text: str, link: tuple[str, str] | None = None) -> str:
    """Plain text -> minimal HTML: paragraphs, preserved line breaks, an optional button-style link."""
    paras = "".join(
        f'<p style="margin:0 0 14px;line-height:1.5">{_h(p).replace(chr(10), "<br>")}</p>'
        for p in text.split("\n\n") if p.strip())
    button = ""
    if link:
        label, url = link
        button = (f'<p style="margin:18px 0"><a href="{_h(url)}" style="background:#1b3a45;color:#ffffff;'
                  f'padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:bold">{_h(label)}</a></p>')
    return ('<!doctype html><html><body style="margin:0;padding:24px;background:#f6f6f4;color:#1a1a1a;'
            'font-family:Arial,Helvetica,sans-serif;font-size:15px"><div style="max-width:560px;margin:0 auto;'
            f'background:#ffffff;padding:24px;border-radius:8px">{paras}{button}</div></body></html>')


def _sign(org: str) -> str:
    return f"Thank you,\n{org}"


def funding_shortfall(plan: dict, org: str, *, overdue: bool = False) -> tuple[str, str, str]:
    subject, text = draft.draft_funding_email("FUNDING_OVERDUE" if overdue else "FUNDING_REMINDER", plan, org)
    return subject, text, render_html(text)


def funding_statement(plan: dict, org: str) -> tuple[str, str, str]:
    subject, text = draft.draft_funding_email("FUNDING_STATEMENT", plan, org)
    return subject, text, render_html(text)


def funds_received(employer: str, amount: str, org: str, *, continuation_started: bool = False) -> tuple[str, str, str]:
    text = (f"Hello,\n\nWe received {amount} for {employer}. Thank you.\n\n"
            + ("The held payments are now queued for approval. Nobody is paid until an approver signs off."
               if continuation_started else "It will be used for the next payroll run.")
            + f"\n\n{_sign(org)}")
    return f"Funds received for {employer}", text, render_html(text)


def approval_requested(employer: str, run_label: str, total: str, url: str, org: str, *,
                       continuation: bool = False) -> tuple[str, str, str]:
    what = "a continuation run for held payments" if continuation else "a payroll run"
    text = (f"Hello,\n\n{employer} has {what} waiting for your approval: {run_label}, {total} in total.\n\n"
            "Nothing is paid until an approver reviews and signs it. Open the run to check the amounts and decide:\n"
            f"{url}\n\n{_sign(org)}")
    return f"Approval needed: {employer} {run_label}", text, render_html(text, ("Review the run", url))


def held_lines_reminder(employer: str, run_label: str, count: int, url: str, org: str, *,
                        funded: bool) -> tuple[str, str, str]:
    line = (f"{employer} has {count} payment{'s' if count != 1 else ''} still on hold from {run_label}. "
            + ("The wallet now covers them: open the run and use Pay held lines." if funded else
               "The wallet does not cover them yet, so funds are needed first."))
    text = f"Hello,\n\n{line}\n\n{url}\n\n{_sign(org)}"
    return f"Held payments waiting: {employer}", text, render_html(text, ("Open the run", url))


def run_settled(employer: str, run_label: str, total: str, url: str, org: str) -> tuple[str, str, str]:
    text = (f"Hello,\n\nThe {employer} payroll ({run_label}) is settled: {total} was paid out and reconciled.\n\n"
            f"Receipts and the audit trail are here:\n{url}\n\n{_sign(org)}")
    return f"Payroll settled: {employer}", text, render_html(text, ("Open the run", url))


def payment_failed(employer: str, run_label: str, reason: str, url: str, org: str) -> tuple[str, str, str]:
    text = (f"Hello,\n\nThe {employer} payroll ({run_label}) did not complete.\n\nWhat happened: {reason}\n\n"
            f"Open the run to see what to do next:\n{url}\n\n{_sign(org)}")
    return f"Payroll problem: {employer}", text, render_html(text, ("Open the run", url))


def invite(org: str, inviter: str, role: str, url: str) -> tuple[str, str, str]:
    text = (f"Hello,\n\n{inviter} invited you to join {org} on PaySilo as {role}.\n\nAccept the invitation "
            f"here (the link works once and expires):\n{url}\n\nThank you,\nPaySilo")
    return f"You are invited to {org} on PaySilo", text, render_html(text, ("Accept invitation", url))
