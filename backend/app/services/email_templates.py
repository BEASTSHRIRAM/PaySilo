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



def onboarding_link(contractor: str, employer: str, org: str, url: str, days: int) -> tuple[str, str, str]:
    text = (f"Hello {contractor},\n\n{org} is paying you for {employer} and needs your payout and tax details. "
            f"Use your private link (it works once and expires in {days} days):\n{url}\n\n"
            f"Never share this link. If you did not expect this email, ignore it.\n\n{_sign(org)}")
    return f"Add your payout details for {employer}", text, render_html(text, ("Add my details", url))


def bank_change_callback(contractor: str, employer: str, masked: str, url: str, org: str) -> tuple[str, str, str]:
    text = (f"Hello,\n\nThe bank details of {contractor} ({employer}) were changed or submitted (account {masked}). "
            "They will NOT be paid until a second person verifies them.\n\n"
            "Please call the contractor on a phone number you already know (not one from an email) and confirm the "
            f"change, then verify it here:\n{url}\n\n{_sign(org)}")
    return f"Verify new bank details: {contractor}", text, render_html(text, ("Open the employer", url))


# --------------------------------------------------------------------------------------------------
# Spend categories (onboarding kits, travel), employer welcome, payslips and workflow alerts.
# Each takes ONE context dict of already-formatted facts so workflow nodes and routes share a shape.
# Nothing here decides anything: amounts, dates and statuses come from the caller's recorded rows.
# --------------------------------------------------------------------------------------------------

BLOCKED_COMPONENT_WORDING = ("Not yet booked: exceeds the current credential limit; "
                             "the team will arrange this separately")
_BOOKED_STATUSES = ("PURCHASED", "SUCCEEDED", "BOOKED", "CONFIRMED")


def _v(ctx: dict, key: str, default: str = "") -> str:
    value = ctx.get(key)
    return default if value is None else str(value)


def _money_s(currency: object, amount: object) -> str:
    return f"{'' if currency is None else currency} {'' if amount is None else amount}".strip()


def spend_approval_requested(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    cur = ctx.get("currency")
    lines = []
    for line in ctx.get("lines") or []:
        lines.append(f"- {_v(line, 'label')}: {_money_s(line.get('currency', cur), line.get('amount'))}"
                     if isinstance(line, dict) else f"- {line}")
    total = _money_s(cur, ctx.get("total"))
    entity = _v(ctx, "entity_label", "Approval")
    text = (f"Hello,\n\nA purchase needs your decision before it can proceed.\n\n"
            f"{entity} for {_v(ctx, 'employer_name')}"
            + (f" (requested for {_v(ctx, 'requested_for')})" if ctx.get("requested_for") else "") + ".\n\n"
            + ("\n".join(lines) + "\n\n" if lines else "") + f"Total: {total}\n\n"
            "Nothing is bought until an approver reviews the exact items and approves. If you were not "
            f"expecting this request, do not approve it.\n\n{_sign(org)}")
    subject = f"Approval requested: {entity}" + (f" ({total})" if total else "")
    link = ctx.get("link_url")
    return subject, text, render_html(text, ("Review and decide", link) if link else None)


def kit_completed(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    items = []
    for item in ctx.get("items") or []:
        if not isinstance(item, dict):
            items.append(f"- {item}")
            continue
        extra = f" (provider reference {item['provider_ref']})" if item.get("provider_ref") else ""
        items.append(f"- {_v(item, 'label')}: {_money_s(item.get('currency'), item.get('amount'))}{extra}")
    text = (f"Hello {_v(ctx, 'contractor_name')},\n\nThe following onboarding kit items have been purchased for you "
            f"by {_v(ctx, 'employer_name')}:\n\n" + "\n".join(items) + f"\n\nTotal: {_v(ctx, 'total')}\n\n"
            f"Keep this message as your record of the completed kit.\n\n{_sign(org)}")
    return f"Onboarding kit complete: {_v(ctx, 'employer_name', 'PaySilo')}", text, render_html(text)


def _component_line(c: dict) -> str:
    label = _v(c, "label", _v(c, "item"))
    prefix = label + (f" ({_v(c, 'kind')})" if c.get("kind") else "")
    if c.get("amount") is not None:
        prefix += f": {_money_s(c.get('currency'), c.get('amount'))}"
    status = _v(c, "status").strip().upper()
    if status == "BLOCKED":
        return f"{prefix}. {BLOCKED_COMPONENT_WORDING}"
    if status in _BOOKED_STATUSES:
        return f"{prefix}. Booked" + (f" (provider reference {c['provider_ref']})" if c.get("provider_ref") else "")
    return f"{prefix}. Status: {_v(c, 'status') or 'pending'}"


def trip_itinerary(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    comps = [f"- {_component_line(c)}" for c in (ctx.get("components") or []) if isinstance(c, dict)]
    text = (f"Hello {_v(ctx, 'contractor_name')},\n\nYour trip to {_v(ctx, 'destination')} runs from "
            f"{_v(ctx, 'start_date')} to {_v(ctx, 'end_date')}. Status of each component:\n\n" + "\n".join(comps)
            + f"\n\nIf any detail looks wrong, contact the team before you travel.\n\n{_sign(org)}")
    return (f"Trip itinerary: {_v(ctx, 'destination')} ({_v(ctx, 'start_date')} to {_v(ctx, 'end_date')})",
            text, render_html(text))


def purchase_receipt(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    facts = [f"Buyer: {_v(ctx, 'buyer_label')}", f"Item: {_v(ctx, 'item_label')}",
             f"Merchant: {_v(ctx, 'merchant')}", f"Amount: {_money_s(ctx.get('currency'), ctx.get('amount'))}"]
    for key, label in (("provider_ref", "Provider reference"), ("mode", "Mode"), ("book_label", "Recorded in book")):
        if ctx.get(key):
            facts.append(f"{label}: {_v(ctx, key)}")
    text = ("Hello,\n\nPurchase receipt\n\n" + "\n".join(facts)
            + f"\n\nThis receipt records the purchase figures exactly as approved and booked.\n\n{_sign(org)}")
    return f"Purchase receipt: {_v(ctx, 'item_label')}", text, render_html(text)


def employer_welcome(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    e = _v(ctx, "employer_name")
    text = (f"Hello,\n\nYour employer account for {e} is ready in PaySilo. Every employer's money stays in its own "
            f"separate silo: your funds are never mixed with another employer's, and payroll for {e} can only "
            f"ever draw on {e}'s own balance.\n\nNext steps:\n"
            "- Fund your silo before your first payroll date.\n"
            "- Add your contractors.\n"
            "- Approve each payroll plan before it executes: nothing is paid without a human approving the exact "
            f"figures.\n\n{_sign(org)}")
    return f"Welcome to PaySilo, {e}", text, render_html(text)


def payslip_receipt(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    """Carries the contractor's private receipt link: the caller must queue it as a sensitive notification."""
    link = ctx.get("receipt_link")
    text = (f"Hello {_v(ctx, 'contractor_name')},\n\n{_v(ctx, 'employer_name')} paid you "
            f"{_money_s(ctx.get('currency'), ctx.get('amount'))}.\n\n"
            "Your receipt shows the full breakdown of this payment"
            + (f":\n{link}\n\nNever share this link." if link else ".") + f"\n\n{_sign(org)}")
    return (f"Your payslip from {_v(ctx, 'employer_name')}: {_money_s(ctx.get('currency'), ctx.get('amount'))}",
            text, render_html(text, ("Open my receipt", link) if link else None))


def payroll_held_alert(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    cur = ctx.get("currency")
    all_or_nothing = _v(ctx, "settlement_mode") == "ALL_OR_NOTHING"
    text = (f"Hello,\n\nA payroll run is on hold for {_v(ctx, 'employer_name')}: "
            f"{_v(ctx, 'run_label', 'the latest run')}.\n\n"
            f"Required: {_money_s(cur, ctx.get('total_required'))}\nShortfall: {_money_s(cur, ctx.get('shortfall'))}\n\n"
            + ("This employer pays all or nothing, so no one was paid. " if all_or_nothing else "")
            + "Add funds to the employer's own wallet; the held payments can then be continued from the run "
              f"(an approver still signs off).\n\n{_sign(org)}")
    return f"Payroll held: {_v(ctx, 'employer_name')}", text, render_html(text)


# template name -> (builder, notification kind, sensitive)
SPEND_TEMPLATES = {
    "approval_requested": (spend_approval_requested, "SPEND_APPROVAL_REQUESTED", False),
    "kit_completed": (kit_completed, "KIT_COMPLETED", False),
    "trip_itinerary": (trip_itinerary, "TRIP_ITINERARY", False),
    "purchase_receipt": (purchase_receipt, "PURCHASE_RECEIPT", False),
    "employer_welcome": (employer_welcome, "EMPLOYER_WELCOME", False),
    "payslip_receipt": (payslip_receipt, "PAYSLIP_RECEIPT", True),
    "payroll_held_alert": (payroll_held_alert, "WORKFLOW_ALERT", False),
}
