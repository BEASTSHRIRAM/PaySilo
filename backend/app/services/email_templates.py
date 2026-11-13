"""Email templates: one function per kind, each returning (subject, plain text, HTML).

Every template is built from the same parts (``email_layout.Mail``) so the text and HTML twins never drift, and
every one follows the house style checked by ``email_style`` (short, professional, no dashes or hyphens as
punctuation, one clear action, 'Thank you,' closing). Nothing here decides anything: amounts, dates and
statuses come from the caller's recorded rows and are only formatted. Links are in app URLs built by code.

``TEMPLATES`` is the registry (label, group, kind, sensitivity, who gets it, when, sample data). The preview
tool, the workflow 'send email' node and the tests all read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..crew import draft
from .email_layout import Mail, compose, money, nice_date, render_html, tidy  # noqa: F401  (render_html re-exported)

REASON_RUN = "You received this email because you can approve payroll runs in PaySilo."
REASON_STAFF = "You received this email because you manage payroll in PaySilo."
REASON_ACCOUNT = "You received this email because of activity on your PaySilo account."
REASON_CONTRACTOR = "You received this email because you are paid through PaySilo."
REASON_EMPLOYER = "You received this email because you are the contact for this employer."


def _v(ctx: dict, key: str, default: str = "") -> str:
    value = ctx.get(key)
    return default if value is None or value == "" else str(value)


def _name(ctx: dict, key: str = "name") -> str | None:
    return _v(ctx, key) or None


def _sign(org: str) -> str:
    return f"Thank you,\n{org}"


def _money_s(currency: object, amount: object) -> str:
    return money(currency, amount)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# --------------------------------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------------------------------

def verify_email_otp(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail("Your PaySilo verification code", "PaySilo", "You received this email because someone used this address "
             "to create a PaySilo account. If it was not you, ignore this email.", name=_name(ctx),
             paras=[f"Use this code to verify your email address. It expires in {_v(ctx, 'minutes', '10')} minutes."],
             code=_v(ctx, "code"), after=["Never share this code with anyone."], footer_org=False)
    return compose(m)


def password_reset_otp(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail("Your PaySilo password reset code", "PaySilo", "You received this email because a password reset was "
             "requested for this address. If it was not you, ignore this email and your password stays the same.",
             name=_name(ctx), paras=[f"Use this code to reset your password. It expires in {_v(ctx, 'minutes', '10')} minutes."],
             code=_v(ctx, "code"), after=["Never share this code with anyone."], footer_org=False)
    return compose(m)


def password_changed(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail("Your PaySilo password was changed", "PaySilo", REASON_ACCOUNT, name=_name(ctx),
             paras=[f"Your password was changed on {nice_date(ctx.get('when'))}. If this was you, no action is needed.",
                    "If it was not you, reset your password now."],
             button=("Reset password", _v(ctx, "url")), footer_org=False)
    return compose(m)


def account_welcome(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    o = _v(ctx, "org_name", org)
    m = Mail(f"Welcome to PaySilo, {o}", o, REASON_ACCOUNT, name=_name(ctx),
             paras=[f"Your email is verified and {o} is ready on PaySilo. Nothing is paid until an approver signs."],
             steps=["Add your first employer.", "Add contractors and bank details.", "Fund the wallet and review a run."],
             button=("Open PaySilo", _v(ctx, "url")))
    return compose(m)


def invite(org: str, inviter: str, role: str, url: str) -> tuple[str, str, str]:
    m = Mail(f"You are invited to {org} on PaySilo", org,
             f"You received this email because {inviter} invited this address. If you did not expect it, ignore it.",
             paras=[f"{inviter} invited you to join {org} on PaySilo as {role}.",
                    "The link works once and expires in 7 days."],
             button=("Accept invitation", url))
    return compose(m)


def invite_accepted(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"{_v(ctx, 'member_name', 'A teammate')} joined {org}", org, REASON_STAFF, name=_name(ctx),
             paras=[f"{_v(ctx, 'member_name')} accepted your invitation and joined {org}."],
             rows=[("Email", _v(ctx, "member_email")), ("Role", _v(ctx, "role"))],
             button=("Open team", _v(ctx, "url")))
    return compose(m)


def role_changed(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"Your role in {org} changed", org, REASON_ACCOUNT, name=_name(ctx),
             paras=[f"{_v(ctx, 'by', 'An owner')} changed your role in {org} from {_v(ctx, 'old_role')} "
                    f"to {_v(ctx, 'new_role')}."],
             button=("Open PaySilo", _v(ctx, "url")))
    return compose(m)


def member_removed(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"Your access to {org} was removed", org, REASON_ACCOUNT, name=_name(ctx),
             paras=[f"{_v(ctx, 'by', 'An administrator')} removed your access to {org} on PaySilo. "
                    "You can no longer open its payroll data.", "If you think this is a mistake, contact the team."])
    return compose(m)


# --------------------------------------------------------------------------------------------------
# Employers and contractors
# --------------------------------------------------------------------------------------------------

def employer_welcome(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    e = _v(ctx, "employer_name")
    m = Mail(f"Welcome to PaySilo, {e}", org, REASON_EMPLOYER,
             paras=[f"Your employer account for {e} is ready. Your funds sit in your own silo and are never mixed "
                    f"with another employer's. Nothing is paid until an approver signs."],
             steps=["Fund your silo before your first payroll date.", "Add your contractors.",
                    "Approve each payroll plan before it runs."])
    return compose(m)


def onboarding_link(contractor: str, employer: str, org: str, url: str, days: int) -> tuple[str, str, str]:
    m = Mail(f"Add your payout details for {employer}", org, "You received this email because you are paid through "
             "PaySilo. If you did not expect it, ignore this email.", name=contractor or None,
             paras=[f"{org} pays you for {employer} and needs your payout and tax details.",
                    f"Your private link works once and expires in {days} days. Never share it."],
             button=("Add my details", url))
    return compose(m)


def onboarding_submitted(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"Details submitted: {_v(ctx, 'contractor_name')}", org, REASON_STAFF, name=_name(ctx),
             paras=[f"{_v(ctx, 'contractor_name')} ({_v(ctx, 'employer_name')}) submitted payout and tax details. "
                    "They are not paid until a second person verifies the bank details by phone."],
             button=("Open the employer", _v(ctx, "url")))
    return compose(m)


def bank_change_callback(contractor: str, employer: str, masked: str, url: str, org: str) -> tuple[str, str, str]:
    m = Mail(f"Verify new bank details: {contractor}", org, REASON_STAFF,
             paras=[f"The bank details of {contractor} ({employer}) were changed or submitted. "
                    "They are not paid until a second person verifies them.",
                    "Call the contractor on a phone number you already know, not one from an email, and confirm the change."],
             rows=[("Account", masked)], button=("Verify the details", url))
    return compose(m)


def bank_verified(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail("Your bank details were verified", org, REASON_CONTRACTOR, name=_name(ctx, "contractor_name"),
             paras=[f"Your bank account ending {_v(ctx, 'last4')} for {_v(ctx, 'employer_name')} was verified. "
                    "You are included in the next payment run."])
    return compose(m)


def tax_form_alert(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    items = ctx.get("items") or []
    rows = [(f"{i.get('contractor_name', '')} ({i.get('employer_name', '')})",
             {"expired": "Form expired", "missing": "Form missing", "expiring_soon": "Form expires "
              + nice_date(i.get("expires")), "status_unknown": "Tax status unknown"}.get(i.get("issue"), "Needs review"))
            for i in items[:8]]
    n = int(ctx.get("count") or len(items))
    m = Mail(f"Tax forms need attention: {n}", org, REASON_STAFF, name=_name(ctx),
             paras=[f"{_plural(n, 'contractor')} need a tax form update. Collect the forms before the next payment."],
             rows=rows, button=("Open compliance", _v(ctx, "url")))
    return compose(m)


# --------------------------------------------------------------------------------------------------
# Funding
# --------------------------------------------------------------------------------------------------

def funding_shortfall(plan: dict, org: str, *, overdue: bool = False) -> tuple[str, str, str]:
    subject, text = draft.draft_funding_email("FUNDING_OVERDUE" if overdue else "FUNDING_REMINDER", plan, org)
    return subject, text, render_html(text, org=org, reason=REASON_EMPLOYER, subject=subject)


def funding_statement(plan: dict, org: str) -> tuple[str, str, str]:
    subject, text = draft.draft_funding_email("FUNDING_STATEMENT", plan, org)
    return subject, text, render_html(text, org=org, reason=REASON_EMPLOYER, subject=subject)


def funds_received(employer: str, amount: str, org: str, *, continuation_started: bool = False) -> tuple[str, str, str]:
    m = Mail(f"Funds received for {employer}", org, REASON_EMPLOYER,
             paras=[f"We received {amount} for {employer}. Thank you.",
                    "The held payments are now waiting for approval. Nothing is paid until an approver signs."
                    if continuation_started else "It will be used for the next payroll run."])
    return compose(m)


# --------------------------------------------------------------------------------------------------
# Payroll
# --------------------------------------------------------------------------------------------------

def approval_requested(employer: str, run_label: str, total: str, url: str, org: str, *,
                       continuation: bool = False, pays: int | None = None, held: int | None = None
                       ) -> tuple[str, str, str]:
    what = "a continuation run for held payments" if continuation else "a payroll run"
    rows = [("Run", run_label), ("Total", total)]
    if pays is not None:
        rows.append(("Will be paid", _plural(pays, "contractor")))
    if held:
        rows.append(("On hold", _plural(held, "contractor")))
    m = Mail(f"Approval needed: {employer} {run_label}", org, REASON_RUN,
             paras=[f"{employer} has {what} waiting for your decision. Nothing is paid until an approver signs."],
             rows=rows, button=("Review the run", url))
    return compose(m)


def approval_reminder(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"Reminder: {_v(ctx, 'employer_name')} run waiting", org, REASON_RUN, name=_name(ctx),
             paras=[f"The {_v(ctx, 'employer_name')} run {_v(ctx, 'run_label')} has waited {_v(ctx, 'hours')} hours "
                    "for approval. Nobody is paid until an approver signs."],
             rows=[("Total", _v(ctx, "total")), ("Reminder", f"{_v(ctx, 'number', '1')} of {_v(ctx, 'max', '3')}")],
             button=("Review the run", _v(ctx, "url")))
    return compose(m)


def run_approved(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"Run approved: {_v(ctx, 'employer_name')}", org, REASON_STAFF, name=_name(ctx),
             paras=[f"{_v(ctx, 'approver', 'An approver')} approved the {_v(ctx, 'employer_name')} run "
                    f"{_v(ctx, 'run_label')}. Payments are being sent now.",
                    "You will get another email when the run is settled."],
             rows=[("Total", _v(ctx, "total"))], button=("Open the run", _v(ctx, "url")))
    return compose(m)


def run_rejected(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"Run not approved: {_v(ctx, 'employer_name')}", org, REASON_STAFF, name=_name(ctx),
             paras=[f"{_v(ctx, 'approver', 'An approver')} did not approve the {_v(ctx, 'employer_name')} run "
                    f"{_v(ctx, 'run_label')}. Nobody was paid and no money moved.",
                    "Fix any issue, then prepare a new run."],
             rows=[("Total", _v(ctx, "total"))], button=("Open the run", _v(ctx, "url")))
    return compose(m)


def run_settled(employer: str, run_label: str, total: str, url: str, org: str) -> tuple[str, str, str]:
    m = Mail(f"Payroll settled: {employer}", org, REASON_RUN,
             paras=[f"The {employer} payroll ({run_label}) is settled. The payments were sent and reconciled."],
             rows=[("Total paid", total)], button=("Open the run", url),
             after=["Receipts and the audit trail are on the run page."])
    return compose(m)


def run_partial(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    held = ctx.get("held") or []
    rows = [(f"{h.get('name', '')}", _money_s(h.get("currency"), h.get("amount"))) for h in held[:10]]
    rows += [("Paid so far", _v(ctx, "paid_total")), ("Still needed", _v(ctx, "fund_amount"))]
    m = Mail(f"Part of the payroll is held: {_v(ctx, 'employer_name')}", org, REASON_RUN,
             paras=[f"{_v(ctx, 'employer_name')} paid part of {_v(ctx, 'run_label')}. "
                    f"{_plural(len(held), 'payment')} below {'is' if len(held) == 1 else 'are'} held because the wallet was short by "
                    f"{_v(ctx, 'shortfall')}.",
                    "Add funds to this employer's own wallet, then use Pay held lines. An approver signs again."],
             rows=rows, button=("Open the run", _v(ctx, "url")))
    return compose(m)


def run_held(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    all_or_nothing = _v(ctx, "settlement_mode") == "ALL_OR_NOTHING"
    cur = ctx.get("currency")
    lead = ("This employer pays all or nothing, so nobody was paid." if all_or_nothing
            else "The wallet could not cover the run, so nobody was paid.")
    m = Mail(f"Payroll held: {_v(ctx, 'employer_name')}", org, REASON_RUN,
             paras=[f"{_v(ctx, 'run_label', 'The latest run')} for {_v(ctx, 'employer_name')} is on hold. {lead}",
                    "Add funds to the employer's own wallet, then start the run again."],
             rows=[("Required", _money_s(cur, ctx.get("total_required"))),
                   ("Shortfall", _money_s(cur, ctx.get("shortfall")))],
             button=("Open the run", _v(ctx, "url")) if ctx.get("url") else None)
    return compose(m)


def payroll_held_alert(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    return run_held(ctx, org)


def held_lines_reminder(employer: str, run_label: str, count: int, url: str, org: str, *,
                        funded: bool) -> tuple[str, str, str]:
    line = (f"{employer} has {_plural(count, 'payment')} still on hold from {run_label}. "
            + ("The wallet now covers them. Open the run and use Pay held lines." if funded else
               "The wallet does not cover them yet, so funds are needed first."))
    m = Mail(f"Held payments waiting: {employer}", org, REASON_RUN, paras=[line], button=("Open the run", url))
    return compose(m)


_RISK_WHY = {"BANK_UNVERIFIED": "bank details are not verified",
             "RECENT_BANK_CHANGE_LARGE_PAYMENT": "bank details changed recently and the amount is large"}


def risk_hold(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    rows = [(h.get("name", ""), f"{_money_s(h.get('currency'), h.get('amount'))}, "
             f"{_RISK_WHY.get(h.get('reason'), 'needs review')}") for h in (ctx.get("held") or [])[:10]]
    m = Mail(f"Payments held for review: {_v(ctx, 'employer_name')}", org, REASON_RUN,
             paras=[f"{_plural(len(ctx.get('held') or []), 'payment')} in {_v(ctx, 'run_label')} were held by the risk check. "
                    "Nothing is paid to them until this is resolved.",
                    "Call the contractor on a number you already know and verify the bank details, then pay the held lines."],
             rows=rows, button=("Open the run", _v(ctx, "url")))
    return compose(m)


def payment_failed(employer: str, run_label: str, reason: str, url: str, org: str) -> tuple[str, str, str]:
    m = Mail(f"Payroll problem: {employer}", org, REASON_RUN,
             paras=[f"The {employer} payroll ({run_label}) did not complete.", f"What happened: {tidy(reason)}"],
             button=("Open the run", url), after=["The run page shows what to do next."])
    return compose(m)


def payslip_receipt(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    """Carries the contractor's private receipt link: the caller must queue it as a sensitive notification."""
    link, proof = ctx.get("receipt_link"), ctx.get("proof_link")
    amount = _money_s(ctx.get("currency"), ctx.get("amount"))
    m = Mail(f"Payment from {_v(ctx, 'employer_name')}: {amount}", org, REASON_CONTRACTOR,
             name=_name(ctx, "contractor_name"),
             paras=[f"{_v(ctx, 'employer_name')} paid you {amount}.",
                    "Your receipt shows the full breakdown." + (" Never share the links in this email." if link else "")],
             button=("Open my receipt", link) if link else None,
             after=[f"Download proof PDF: {proof}"] if proof else [])
    return compose(m)


def payment_returned(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"Payment problem: {_v(ctx, 'contractor_name')}", org, REASON_STAFF, name=_name(ctx),
             paras=[f"A payment to {_v(ctx, 'contractor_name')} ({_v(ctx, 'employer_name')}) did not arrive.",
                    f"Why: {tidy(_v(ctx, 'reason'))}", "No further action is taken until someone fixes the cause."],
             rows=[("Amount", _money_s(ctx.get("currency"), ctx.get("amount")))],
             button=("Open the run", _v(ctx, "url")))
    return compose(m)


# --------------------------------------------------------------------------------------------------
# Spend, kits and trips
# --------------------------------------------------------------------------------------------------

BLOCKED_COMPONENT_WORDING = ("Not yet booked: exceeds the current credential limit; "
                             "the team will arrange this separately")
_BOOKED_STATUSES = ("PURCHASED", "SUCCEEDED", "BOOKED", "CONFIRMED")


def spend_approval_requested(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    cur = ctx.get("currency")
    entity = _v(ctx, "entity_label", "Approval")
    payroll = entity == "Payroll run"
    total = _money_s(cur, ctx.get("total"))
    rows = []
    for line in ctx.get("lines") or []:
        rows.append((_v(line, "label"), _money_s(line.get("currency", cur), line.get("amount")))
                    if isinstance(line, dict) else (str(line), ""))
    rows.append(("Total", total))
    for_whom = f" (requested for {_v(ctx, 'requested_for')})" if ctx.get("requested_for") else ""
    m = Mail(f"Approval requested: {entity}"[:60], org, REASON_RUN,
             paras=[f"{entity} for {_v(ctx, 'employer_name')}{for_whom} needs your decision. "
                    f"Nothing is {'paid' if payroll else 'bought'} until an approver reviews the exact "
                    f"{'amounts' if payroll else 'items'} and approves.",
                    "If you were not expecting this request, do not approve it."],
             rows=rows, button=("Review and decide", ctx["link_url"]) if ctx.get("link_url") else None)
    return compose(m)


def purchase_auth_needed(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    """Never carries the Airi URL: the approver goes to the Benefits page in PaySilo."""
    m = Mail("Authorise a purchase in Airi", org, REASON_RUN, name=_name(ctx),
             paras=[f"You started a purchase of {_v(ctx, 'item_label')} for {_money_s(ctx.get('currency'), ctx.get('amount'))}. "
                    "It needs your authorisation in Airi before any money is spent.",
                    "Open the Benefits page and follow the steps there."],
             button=("Open Benefits", _v(ctx, "url")))
    return compose(m)


def kit_completed(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    rows = []
    for item in ctx.get("items") or []:
        if not isinstance(item, dict):
            rows.append((str(item), ""))
            continue
        extra = f" (reference {item['provider_ref']})" if item.get("provider_ref") else ""
        rows.append((_v(item, "label"), _money_s(item.get("currency"), item.get("amount")) + extra))
    rows.append(("Total", _v(ctx, "total")))
    m = Mail(f"Onboarding kit complete: {_v(ctx, 'employer_name', 'PaySilo')}", org, REASON_CONTRACTOR,
             name=_name(ctx, "contractor_name"),
             paras=[f"{_v(ctx, 'employer_name')} bought your onboarding kit. Keep this email as your record."],
             rows=rows)
    return compose(m)


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
    comps = [_component_line(c) for c in (ctx.get("components") or []) if isinstance(c, dict)]
    start, end = nice_date(ctx.get("start_date")), nice_date(ctx.get("end_date"))
    m = Mail(f"Trip itinerary: {_v(ctx, 'destination')}", org, REASON_CONTRACTOR, name=_name(ctx, "contractor_name"),
             paras=[f"Your trip to {_v(ctx, 'destination')} runs from {start} to {end}. Each part is listed below.",
                    "If any detail looks wrong, contact the team before you travel."],
             steps=comps)
    return compose(m)


def purchase_receipt(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    rows = [("Buyer", _v(ctx, "buyer_label")), ("Item", _v(ctx, "item_label")), ("Merchant", _v(ctx, "merchant")),
            ("Amount", _money_s(ctx.get("currency"), ctx.get("amount")))]
    for key, label in (("provider_ref", "Provider reference"), ("mode", "Mode"), ("book_label", "Recorded in book")):
        if ctx.get(key):
            rows.append((label, _v(ctx, key)))
    m = Mail(f"Purchase receipt: {_v(ctx, 'item_label')}", org, REASON_RUN,
             paras=["This receipt records the purchase exactly as approved and booked."], rows=rows)
    return compose(m)


# --------------------------------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------------------------------

def workflow_failed(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"Workflow failed: {_v(ctx, 'workflow_name', 'a workflow')}", org, REASON_STAFF, name=_name(ctx),
             paras=[f"The workflow {_v(ctx, 'workflow_name')} stopped with an error. No money moved because of it.",
                    f"What happened: {tidy(_v(ctx, 'detail'))}"],
             button=("Open workflows", _v(ctx, "url")))
    return compose(m)


def schedule_skipped(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    m = Mail(f"Schedule skipped: {_v(ctx, 'workflow_name', 'a workflow')}", org, REASON_STAFF, name=_name(ctx),
             paras=[f"The scheduled workflow {_v(ctx, 'workflow_name')} did not run for {nice_date(ctx.get('when'), _v(ctx, 'when'))}.",
                    f"Why: {tidy(_v(ctx, 'reason'))}", "Fix the schedule, or run the workflow by hand."],
             button=("Open workflows", _v(ctx, "url")))
    return compose(m)


CUSTOM_MAX_BODY = 600
CUSTOM_MAX_SUBJECT = 60
CUSTOM_VARIABLES = ("employer_name", "run_label", "total", "shortfall", "currency", "contractor_name", "amount",
                    "organization_name", "status")


def custom_message(ctx: dict, org: str = "PaySilo") -> tuple[str, str, str]:
    """A user written message. Plain text only, length capped, variables from a fixed set, HTML escaped (by the
    layout), and no links except the one in app deep link that code supplies as ``link_url``."""
    from .email_custom import render_custom

    subject, body = render_custom(ctx.get("subject", ""), ctx.get("body", ""), ctx)
    paras = [p.strip() for p in body.split("\n\n") if p.strip()] or [body]
    m = Mail(subject, org, "You received this email because a PaySilo workflow sent it.", paras=paras,
             button=("Open in PaySilo", ctx["link_url"]) if ctx.get("link_url") else None)
    return compose(m)


# --------------------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    group: str
    kind: str
    sensitive: bool
    recipient: str
    when: str
    build: Callable[[dict, str], tuple[str, str, str]]
    sample: dict
    workflow: bool = False  # selectable in the workflow 'send email' node


_U = "https://app.paysilo.example"
_PLAN = {"employer_name": "Acme GmbH", "contact_name": "Ana", "pay_date": "2026-11-20", "fund_by": "2026-11-18",
         "payroll_usd": "6500.00", "run_fee_usd": "149.00", "reserve_floor_usd": "1000.00",
         "required_in_wallet_usd": "7649.00", "available_usd": "5000.00", "gap_usd": "2649.00",
         "recommended_transfer_usd": "2700.00",
         "lines": [{"name": "Lena Fischer", "currency": "EUR", "amount": "3400.00"}],
         "wire": {"status": "READY", "account_name": "Acme Wallet", "account_number": "1234567890",
                  "swift_code": "CMFGUS33", "bank_name": "Example Bank", "reference": "ACME 1234",
                  "routing": [{"type": "ach", "label": "ACH routing number", "value": "026073150"}]}}
_RUN = {"employer_name": "Acme GmbH", "run_label": "Nov 2026", "total": "USD 7,649.00", "url": f"{_U}/dashboard/runs/1"}


def _specs() -> list[Spec]:
    S = Spec
    return [
        S("verify_email_otp", "Verify email code", "Account", "EMAIL_OTP", True, "The person signing up",
          "Right after signup, when email verification is on.", verify_email_otp, {"name": "Priya", "code": "482913"}),
        S("account_welcome", "Welcome to PaySilo", "Account", "ACCOUNT_WELCOME", False, "The new organisation owner",
          "After the email is verified.", account_welcome,
          {"name": "Priya", "org_name": "Vertex Payroll Services", "url": f"{_U}/dashboard"}),
        S("password_reset_otp", "Password reset code", "Account", "EMAIL_OTP", True, "The person who asked for a reset",
          "When someone chooses Forgot password.", password_reset_otp, {"name": "Priya", "code": "205871"}),
        S("password_changed", "Password changed", "Account", "PASSWORD_CHANGED", False, "The account owner",
          "After a password change or reset.", password_changed,
          {"name": "Priya", "when": "2026-11-13", "url": f"{_U}/forgot-password"}),
        S("invite", "Team invitation", "Account", "INVITE", True, "The invited person", "When an owner or admin invites someone.",
          lambda c, o: invite(o, c["inviter"], c["role"], c["url"]),
          {"inviter": "Priya Nair", "role": "approver", "url": f"{_U}/invite/example-token"}),
        S("invite_accepted", "Invitation accepted", "Account", "INVITE_ACCEPTED", False, "The person who invited",
          "When an invitation is accepted.", invite_accepted,
          {"name": "Priya", "member_name": "Sam Okafor", "member_email": "sam@example.com", "role": "approver",
           "url": f"{_U}/dashboard/team"}),
        S("role_changed", "Role changed", "Account", "ROLE_CHANGED", False, "The member", "When an owner changes a role.",
          role_changed, {"name": "Sam", "by": "Priya Nair", "old_role": "viewer", "new_role": "approver", "url": f"{_U}/dashboard"}),
        S("member_removed", "Member removed", "Account", "MEMBER_REMOVED", False, "The removed member",
          "When an owner or admin removes a member.", member_removed, {"name": "Sam", "by": "Priya Nair"}),

        S("employer_welcome", "Employer welcome", "Employers and contractors", "EMPLOYER_WELCOME", False,
          "The employer contact", "When an employer is added.", employer_welcome, {"employer_name": "Acme GmbH"}, True),
        S("onboarding_link", "Contractor onboarding link", "Employers and contractors", "ONBOARDING_LINK", True,
          "The contractor", "When staff send an onboarding link.",
          lambda c, o: onboarding_link(c["contractor"], c["employer"], o, c["url"], c["days"]),
          {"contractor": "Lena Fischer", "employer": "Acme GmbH", "url": f"{_U}/onboard/example-token", "days": 7}),
        S("onboarding_submitted", "Onboarding submitted", "Employers and contractors", "ONBOARDING_SUBMITTED", False,
          "Owners, admins and approvers", "When a contractor submits their details.", onboarding_submitted,
          {"contractor_name": "Lena Fischer", "employer_name": "Acme GmbH", "url": f"{_U}/dashboard/employers/1"}),
        S("bank_change_callback", "Bank change callback request", "Employers and contractors", "BANK_CHANGE_CALLBACK",
          False, "Approvers and the employer contact", "When bank details change or are submitted.",
          lambda c, o: bank_change_callback(c["contractor"], c["employer"], c["masked"], c["url"], o),
          {"contractor": "Lena Fischer", "employer": "Acme GmbH", "masked": "DE89 **** 3000", "url": f"{_U}/dashboard/employers/1"}),
        S("bank_verified", "Bank details verified", "Employers and contractors", "BANK_VERIFIED", False,
          "The contractor", "After a second person verifies the bank details.", bank_verified,
          {"contractor_name": "Lena Fischer", "employer_name": "Acme GmbH", "last4": "3000"}),
        S("tax_form_alert", "Tax forms need attention", "Employers and contractors", "TAX_FORM_ALERT", False,
          "Owners and admins", "Weekly, when a tax form is missing, expired or expiring.", tax_form_alert,
          {"count": 2, "url": f"{_U}/dashboard/compliance",
           "items": [{"contractor_name": "Lena Fischer", "employer_name": "Acme GmbH", "issue": "expiring_soon", "expires": "2026-12-01"},
                     {"contractor_name": "Joao Silva", "employer_name": "Borealis", "issue": "missing"}]}),

        S("funding_reminder", "Funding reminder", "Funding", "FUNDING_REMINDER", False, "The employer contact",
          "3, 2 and 1 business days before the fund by date.", lambda c, o: funding_shortfall(c["plan"], o), {"plan": _PLAN}),
        S("funding_overdue", "Funding overdue", "Funding", "FUNDING_OVERDUE", False, "The employer contact",
          "Daily while the funding is late.", lambda c, o: funding_shortfall(c["plan"], o, overdue=True), {"plan": _PLAN}),
        S("funding_statement", "Funding statement", "Funding", "FUNDING_STATEMENT", False, "The employer contact",
          "When staff send a statement.", lambda c, o: funding_statement(c["plan"], o), {"plan": _PLAN}),
        S("funds_received", "Funds received", "Funding", "FUNDS_RECEIVED", False, "The employer contact",
          "When a deposit arrives.",
          lambda c, o: funds_received(c.get("employer_name", ""), money("USD", c.get("amount")), o,
                                      continuation_started=bool(c.get("continuation"))),
          {"employer_name": "Acme GmbH", "amount": "2700.00", "continuation": True}, True),

        S("approval_requested", "Run waiting for approval", "Payroll", "APPROVAL_REQUESTED", False,
          "Owners, admins and approvers", "When a run is prepared (manual, scheduled or continuation).",
          lambda c, o: approval_requested(c["employer_name"], c["run_label"], c["total"], c["url"], o,
                                          continuation=bool(c.get("continuation")), pays=c.get("pays"), held=c.get("held")),
          {**_RUN, "pays": 4, "held": 1}, True),
        S("approval_reminder", "Approval reminder", "Payroll", "APPROVAL_REMINDER", False,
          "Owners, admins and approvers", "When a run still waits after the reminder hours (up to 3 times).",
          approval_reminder, {**_RUN, "hours": 6, "number": 1, "max": 3}),
        S("run_approved", "Run approved", "Payroll", "RUN_APPROVED", False, "The person who prepared the run",
          "When an approver signs a run (optional, setting NOTIFY_RUN_APPROVED).", run_approved,
          {**_RUN, "approver": "Priya Nair", "name": "Sam"}),
        S("run_rejected", "Run not approved", "Payroll", "RUN_REJECTED", False, "The preparer and the approvers",
          "When an approver holds or rejects a run.", run_rejected, {**_RUN, "approver": "Priya Nair"}),
        S("run_settled", "Payroll settled", "Payroll", "RUN_SETTLED", False, "Owners, admins and approvers",
          "When a run is settled.", lambda c, o: run_settled(c.get("employer_name", ""), c.get("run_label", ""), c.get("total", ""),
                                   c.get("url") or c.get("link_url") or _U, o),
          _RUN, True),
        S("run_partial", "Payroll partly paid", "Payroll", "RUN_PARTIAL", False, "Owners, admins and approvers",
          "When a run pays some contractors and holds others for lack of funds.", run_partial,
          {**_RUN, "paid_total": "USD 5,200.00", "shortfall": "USD 2,649.00", "fund_amount": "USD 2,700.00",
           "held": [{"name": "Joao Silva", "amount": "2400.00", "currency": "EUR"}]}),
        S("run_held", "Payroll held", "Payroll", "PAYROLL_HELD", False, "Owners, admins and approvers",
          "When a run is held and nobody can be paid, including all or nothing employers.", run_held,
          {"employer_name": "Acme GmbH", "run_label": "Nov 2026", "currency": "USD", "total_required": "7649.00",
           "shortfall": "2649.00", "settlement_mode": "ALL_OR_NOTHING", "url": f"{_U}/dashboard/runs/1"}),
        S("payroll_held_alert", "Payroll held alert", "Payroll", "WORKFLOW_ALERT", False, "Set by the workflow",
          "From a workflow when a payroll is held.", payroll_held_alert,
          {"employer_name": "Acme GmbH", "run_label": "Nov 2026", "currency": "USD", "total_required": "7649.00",
           "shortfall": "2649.00", "settlement_mode": "PARTIAL"}, True),
        S("held_lines_reminder", "Held payments reminder", "Payroll", "HELD_LINES_REMINDER", False,
          "Owners, admins and approvers", "Daily while held lines wait for over 24 hours.",
          lambda c, o: held_lines_reminder(c["employer_name"], c["run_label"], c["count"], c["url"], o, funded=bool(c.get("funded"))),
          {**_RUN, "count": 2, "funded": True}, True),
        S("risk_hold", "Payments held by risk check", "Payroll", "RISK_HOLD", False, "Owners, admins and approvers",
          "When a line is held for unverified or recently changed bank details.", risk_hold,
          {**_RUN, "held": [{"name": "Joao Silva", "amount": "2400.00", "currency": "EUR", "reason": "BANK_UNVERIFIED"}]}),
        S("payment_failed", "Payroll problem", "Payroll", "PAYMENT_FAILED", False, "Owners, admins and approvers",
          "When a run fails.", lambda c, o: payment_failed(c["employer_name"], c["run_label"], c["reason"], c["url"], o),
          {**_RUN, "reason": "The receiving bank could not match the name to the account."}),
        S("payment_returned", "Payment failed or returned", "Payroll", "PAYMENT_FAILED", False,
          "Owners, admins and approvers", "When a single payment is returned, with the plain reason.", payment_returned,
          {"contractor_name": "Joao Silva", "employer_name": "Acme GmbH", "amount": "2400.00", "currency": "EUR",
           "reason": "The name did not match the bank account.", "url": f"{_U}/dashboard/runs/1"}),
        S("payslip_receipt", "Contractor payment notice", "Payroll", "PAYSLIP_RECEIPT", True, "The paid contractor",
          "When a contractor line is paid.", payslip_receipt,
          {"contractor_name": "Lena Fischer", "employer_name": "Acme GmbH", "amount": "3400.00", "currency": "EUR",
           "receipt_link": f"{_U}/receipt/1?token=example", "proof_link": f"{_U}/api/receipts/1/proof.pdf?token=example"}, True),

        S("spend_approval_requested", "Spend approval requested", "Spend", "SPEND_APPROVAL_REQUESTED", False,
          "Owners, admins and approvers", "When a benefit, kit or trip needs a decision.", spend_approval_requested,
          {"entity_label": "Onboarding kit", "employer_name": "Acme GmbH", "requested_for": "Lena Fischer", "currency": "USD",
           "total": "75.00", "lines": [{"label": "Welcome gift card", "amount": "25.00"}, {"label": "Headset", "amount": "50.00"}],
           "link_url": f"{_U}/dashboard/onboarding"}),
        S("purchase_auth_needed", "Authorise purchase in Airi", "Spend", "PURCHASE_AUTH_NEEDED", False,
          "The approver who started the purchase", "When a live purchase waits for Airi authorisation.",
          purchase_auth_needed, {"name": "Priya", "item_label": "Online course", "amount": "49.00", "currency": "USD",
                                  "url": f"{_U}/dashboard/benefits"}),
        S("purchase_receipt", "Purchase receipt", "Spend", "PURCHASE_RECEIPT", False, "The requester",
          "When a benefit purchase succeeds.", purchase_receipt,
          {"buyer_label": "Acme GmbH", "item_label": "Online course", "merchant": "coursera.org", "currency": "USD",
           "amount": "49.00", "provider_ref": "vic_sim_abc", "mode": "SIMULATED"}, True),
        S("kit_completed", "Onboarding kit complete", "Spend", "KIT_COMPLETED", False, "The contractor",
          "When every kit item is bought.", kit_completed,
          {"contractor_name": "Lena Fischer", "employer_name": "Acme GmbH", "total": "USD 75.00",
           "items": [{"label": "Welcome gift card", "amount": "25.00", "currency": "USD"}]}, True),
        S("trip_itinerary", "Trip itinerary", "Spend", "TRIP_ITINERARY", False, "The contractor", "When a trip is booked.",
          trip_itinerary,
          {"contractor_name": "Lena Fischer", "destination": "Berlin", "start_date": "2026-12-05", "end_date": "2026-12-06",
           "components": [{"label": "Flight", "kind": "TRANSPORT", "amount": "450.00", "currency": "USD", "status": "BLOCKED"},
                          {"label": "Hotel night", "kind": "LODGING", "amount": "180.00", "currency": "USD",
                           "status": "PURCHASED", "provider_ref": "vic_sim_abc"}]}, True),

        S("workflow_failed", "Workflow failed", "Workflows", "WORKFLOW_FAILED", False, "Owners and admins",
          "When a workflow run fails.", workflow_failed,
          {"name": "Priya", "workflow_name": "Monthly payroll", "detail": "Acme GmbH has no active contractors.",
           "url": f"{_U}/dashboard/workflows"}),
        S("schedule_skipped", "Schedule skipped", "Workflows", "SCHEDULE_SKIPPED", False, "Owners and admins",
          "When a scheduled workflow cannot run.", schedule_skipped,
          {"name": "Priya", "workflow_name": "Monthly payroll", "when": "1 Nov 2026",
           "reason": "The schedule settings are invalid.", "url": f"{_U}/dashboard/workflows"}),
        S("custom_message", "Custom message", "Workflows", "CUSTOM_MESSAGE", False, "Set by the workflow",
          "From a workflow 'send email' step with your own subject and text.", custom_message,
          {"subject": "Payroll held for {employer_name}", "body": "The run {run_label} is held.\n\nShortfall: {shortfall}.",
           "employer_name": "Acme GmbH", "run_label": "Nov 2026", "shortfall": "USD 2,649.00",
           "link_url": f"{_U}/dashboard/runs/1"}, True),
    ]


TEMPLATES: dict[str, Spec] = {s.key: s for s in _specs()}


# Workflow 'send email' registry: template name -> (builder(ctx, org), notification kind, sensitive).
# Kept in the shape the spend module has always used.
def _wf_entry(key: str):
    s = TEMPLATES[key]
    return s.build, s.kind, s.sensitive


SPEND_TEMPLATES = {
    "approval_requested": (spend_approval_requested, "SPEND_APPROVAL_REQUESTED", False),
    "kit_completed": (kit_completed, "KIT_COMPLETED", False),
    "trip_itinerary": (trip_itinerary, "TRIP_ITINERARY", False),
    "purchase_receipt": (purchase_receipt, "PURCHASE_RECEIPT", False),
    "employer_welcome": (employer_welcome, "EMPLOYER_WELCOME", False),
    "payslip_receipt": (payslip_receipt, "PAYSLIP_RECEIPT", True),
    "payroll_held_alert": (payroll_held_alert, "WORKFLOW_ALERT", False),
    "custom_message": (custom_message, "CUSTOM_MESSAGE", False),
    "funds_received": _wf_entry("funds_received"),
    "run_settled": _wf_entry("run_settled"),
}
