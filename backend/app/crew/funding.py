"""Funding Planner: what each employer must send, by when, computed by code.

Real-life payroll is PRE-FUNDED: the employer's wallet has to hold contractor pay + payout fees + the
PaySilo run fee (+ the reserve floor) a day or two before pay day. This module answers "how much, by
which date, from which wallet" for every employer, deterministically. No LLM is involved in any number.

Rules (reused, never re-implemented):

* Fees, run fee, reserve floor and affordability come from :func:`app.policy.assess_payroll` and
  :data:`app.policy.PAYROLL_POLICY` - the exact rules the real run applies.
* ``available_usd`` is THAT employer's own wallet only (ledger-derived). Nothing is ever summed across
  employers for affordability; the only cross-employer totals are plain sums of per-employer gaps
  ("total still to collect"), which never offset one another.
* ``fund_by`` = pay date minus ``funding_lead_days`` BUSINESS days (Mon-Fri). Public holidays are NOT
  modelled: a bank holiday in between means funds should arrive earlier than shown.
* ``recommended_transfer_usd`` = gap + 3% FX buffer on the non-USD part of the gap, rounded UP to the
  next USD 50. The non-USD part is ``min(gap, USD value of the non-USD payroll)``; the buffer covers rate
  drift between the transfer and the run. A zero gap recommends nothing.
* Rates: demo mode uses the static ``USD_VALUE`` table; live mode asks the runtime's live rate provider
  (Airwallex quotes on the employer's own account) behind a 5 minute TTL cache. A failed lookup yields
  status ``RATE_UNAVAILABLE`` with a message - never a guessed rate.
* A pay date that is already in the past is rolled forward by the pay frequency (the run for it is
  assumed done); without a frequency it stays as set (and an unfunded employer becomes OVERDUE).
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
import uuid
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db.models import Employer
from ..money import money_str, round_money, to_decimal
from ..policy import PAYROLL_POLICY, ContractorPayroll, PayrollAssessment, RefusalError, assess_payroll

logger = logging.getLogger(__name__)

FX_BUFFER_RATE = Decimal("0.03")
TRANSFER_ROUNDING = Decimal("50")
DUE_SOON_DAYS = 3
RATE_TTL_SECONDS = 300
WIRE_TTL_SECONDS = 600

STATUSES = ("FUNDED", "SHORT", "DUE_SOON", "OVERDUE", "NO_SCHEDULE", "NO_LINES", "RATE_UNAVAILABLE")


# ---------------------------------------------------------------------------- dates
def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def business_days_before(d: date, n: int) -> date:
    """``d`` minus ``n`` business days (weekends skipped; holidays are not modelled)."""
    cur = d
    left = max(0, n)
    while left > 0:
        cur -= timedelta(days=1)
        if is_business_day(cur):
            left -= 1
    return cur


def business_days_between(start: date, end: date) -> int:
    """Business days in ``(start, end]``; 0 when ``end`` is not after ``start``."""
    n, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if is_business_day(cur):
            n += 1
    return n


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) + months, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


def next_occurrence(pay_date: date, frequency: str | None, today: date) -> date:
    """Roll a stale pay date forward by its frequency until it is today or later."""
    if frequency is None or pay_date >= today:
        return pay_date
    if frequency == "WEEKLY":
        return pay_date + timedelta(days=7 * -(-(today - pay_date).days // 7))
    if frequency == "BIWEEKLY":
        return pay_date + timedelta(days=14 * -(-(today - pay_date).days // 14))
    months, cur = 0, pay_date
    while cur < today:
        months += 1
        cur = _add_months(pay_date, months)
    return cur


# ---------------------------------------------------------------------------- rates
_cache_lock = threading.Lock()
_rate_cache: dict[tuple, tuple[float, dict[str, Decimal]]] = {}
_wire_cache: dict[str, tuple[float, dict]] = {}
_clock = time.monotonic


def clear_caches() -> None:
    with _cache_lock:
        _rate_cache.clear()
        _wire_cache.clear()


def usd_rates_for(runtime, employer: Employer, currencies: set[str]) -> dict[str, Decimal]:
    """USD value of one unit per currency. Raises ``RefusalError`` when a live rate is unavailable."""
    wanted = {c.upper() for c in currencies} | {"USD"}
    provider = runtime.services.rate_provider
    if provider is None:  # demo mode: the static table
        return {c: to_decimal(v) for c, v in runtime.services.usd_rates.items()}
    key = (str(employer.airwallex_account_id), tuple(sorted(wanted)))
    now = _clock()
    with _cache_lock:
        hit = _rate_cache.get(key)
        if hit and now - hit[0] < RATE_TTL_SECONDS:
            return dict(hit[1])
    bucket = int(now // RATE_TTL_SECONDS)  # fresh quote id per 5 minute bucket
    rates = provider.rates_for(wanted, account_id=employer.airwallex_account_id,
                               run_id=f"funding-plan:{employer.id}:{bucket}")
    with _cache_lock:
        _rate_cache[key] = (now, dict(rates))
    return dict(rates)


# ---------------------------------------------------------------------------- wire details
_ROUTING_LABELS = {"ach": "ACH routing number", "fedwire": "Fedwire routing number", "swift": "SWIFT/BIC"}


def wire_details(runtime, employer: Employer) -> dict | None:
    """Where the employer sends money: its OWN USD Global Account. Only transfer-relevant fields.

    ``status`` is READY, PENDING (the bank has not issued the number yet) or UNAVAILABLE.
    """
    if not employer.airwallex_account_id or not employer.airwallex_global_account_id:
        return None
    gid = employer.airwallex_global_account_id
    now = _clock()
    with _cache_lock:
        hit = _wire_cache.get(gid)
        if hit and now - hit[0] < WIRE_TTL_SECONDS:
            return dict(hit[1])
    try:
        raw = runtime.recorder.get_global_account(account_id=employer.airwallex_account_id, global_account_id=gid)
    except Exception as exc:  # noqa: BLE001 - never break the whole plan over wire details
        logger.warning("Wire details unavailable for employer %s: %s", employer.id, type(exc).__name__)
        return {"status": "UNAVAILABLE", "currency": employer.funding_currency, "routing": [],
                "message": "Wire details could not be loaded right now. Try again shortly."}
    details = _wire_from_raw(raw, employer)
    if details["status"] == "READY":
        with _cache_lock:
            _wire_cache[gid] = (now, dict(details))
    return details


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text if text and text != "-" else None


def _wire_from_raw(raw: dict, employer: Employer) -> dict:
    number, name = _clean(raw.get("account_number")), _clean(raw.get("account_name"))
    routing: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for feat in raw.get("supported_features") or []:
        if feat.get("type") not in (None, "DEPOSIT"):
            continue
        for rc in feat.get("routing_codes") or []:
            kind, value = str(rc.get("type", "")).lower(), _clean(rc.get("value"))
            if value and (kind, value) not in seen:
                seen.add((kind, value))
                routing.append({"type": kind, "label": _ROUTING_LABELS.get(kind, kind.upper()), "value": value})
    inst = raw.get("institution") or {}
    ready = bool(number and name)
    return {
        "status": "READY" if ready else "PENDING",
        "currency": employer.funding_currency,
        "account_name": name, "account_number": number, "account_type": _clean(raw.get("account_type")),
        "swift_code": _clean(raw.get("swift_code")), "iban": _clean(raw.get("iban")),
        "routing": routing,
        "bank_name": _clean(inst.get("name")),
        "bank_address": ", ".join(x for x in (_clean(inst.get("address")), _clean(inst.get("city")),
                                              _clean(inst.get("zip_code"))) if x) or None,
        "reference": employer.name,
        "message": None if ready else
        "The bank is still issuing this account's number. Details appear here once it is ready.",
    }


# ---------------------------------------------------------------------------- plan
@dataclass
class PlanLine:
    contractor_id: str
    name: str
    currency: str
    method: str
    amount: Decimal | None
    fee: Decimal | None = None
    amount_usd: Decimal | None = None
    fee_usd: Decimal | None = None
    needs_amount: bool = False


@dataclass
class FundingPlan:
    employer_id: str
    employer_name: str
    status: str
    message: str
    pay_frequency: str | None
    funding_lead_days: int
    scheduled_pay_date: date | None
    pay_date: date | None
    fund_by: date | None
    days_until: int | None  # calendar days until pay date
    days_until_fund_by: int | None  # negative = past due
    today: date
    lines: list[PlanLine] = field(default_factory=list)
    needs_amount: list[PlanLine] = field(default_factory=list)
    by_currency: list[dict] = field(default_factory=list)
    wallet: dict[str, str] = field(default_factory=dict)
    payroll_usd: Decimal = Decimal("0.00")
    run_fee_usd: Decimal = Decimal("0.00")
    reserve_floor_usd: Decimal = Decimal("0.00")
    required_in_wallet_usd: Decimal = Decimal("0.00")
    available_usd: Decimal = Decimal("0.00")
    gap_usd: Decimal = Decimal("0.00")
    recommended_transfer_usd: Decimal = Decimal("0.00")
    is_short: bool = False
    contact_email: str | None = None
    contact_name: str | None = None
    wire: dict | None = None

    def as_dict(self) -> dict:
        return _jsonable(self)


def _jsonable(v):
    if isinstance(v, Decimal):
        return money_str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "__dataclass_fields__"):
        return {k: _jsonable(getattr(v, k)) for k in v.__dataclass_fields__}
    return v


def recommended_transfer(gap: Decimal, non_usd_required_usd: Decimal) -> Decimal:
    """gap + 3% FX buffer on the non-USD part of the gap, rounded UP to the next USD 50."""
    gap = round_money(gap)
    if gap <= 0:
        return Decimal("0.00")
    non_usd_part = min(gap, max(Decimal("0"), non_usd_required_usd))
    total = gap + non_usd_part * FX_BUFFER_RATE
    return round_money((total / TRANSFER_ROUNDING).to_integral_value(rounding=ROUND_CEILING) * TRANSFER_ROUNDING)


def classify(*, has_lines: bool, has_schedule: bool, is_short: bool, days_until_fund_by: int | None) -> str:
    """Status from facts only. Rate failures are decided before this is reached."""
    if not has_lines:
        return "NO_LINES"
    if not has_schedule:
        return "NO_SCHEDULE"
    if not is_short:
        return "FUNDED"
    assert days_until_fund_by is not None
    if days_until_fund_by < 0:
        return "OVERDUE"
    if days_until_fund_by <= DUE_SOON_DAYS:
        return "DUE_SOON"
    return "SHORT"


def _message(plan: FundingPlan) -> str:
    gap, rec = money_str(plan.gap_usd), money_str(plan.recommended_transfer_usd)
    when = plan.fund_by.strftime("%a %d %b %Y") if plan.fund_by else ""
    return {
        "FUNDED": f"Wallet already covers this payroll (USD {money_str(plan.required_in_wallet_usd)} needed).",
        "NO_SCHEDULE": ("Set a pay date to get a fund-by date and reminders."
                        + (f" Wallet is short by USD {gap}." if plan.is_short else "")),
        "NO_LINES": "No active contractors with a default amount yet, so there is nothing to fund.",
        "SHORT": f"Send USD {rec} by {when}. Wallet is short by USD {gap}.",
        "DUE_SOON": f"Send USD {rec} by {when}. Wallet is short by USD {gap}.",
        "OVERDUE": f"Funding is late: it was due {when}. Send USD {rec} now. Short by USD {gap}.",
    }.get(plan.status, "")


def plan_employer(db: Session, runtime, employer: Employer, today: date | None = None, *,
                  with_wire: bool = True) -> FundingPlan:
    today = today or date.today()
    scheduled = employer.next_pay_date
    pay_date = next_occurrence(scheduled, employer.pay_frequency, today) if scheduled else None
    lead = employer.funding_lead_days if employer.funding_lead_days is not None else 2
    fund_by = business_days_before(pay_date, lead) if pay_date else None
    plan = FundingPlan(
        employer_id=str(employer.id), employer_name=employer.name, status="NO_LINES", message="",
        pay_frequency=employer.pay_frequency, funding_lead_days=lead, scheduled_pay_date=scheduled,
        pay_date=pay_date, fund_by=fund_by,
        days_until=(pay_date - today).days if pay_date else None,
        days_until_fund_by=(fund_by - today).days if fund_by else None,
        today=today, contact_email=employer.contact_email, contact_name=employer.contact_name,
        wallet=runtime.balances(db, employer),
        wire=wire_details(runtime, employer) if with_wire else None,
    )
    active = [c for c in employer.contractors if c.is_active]
    priced = [c for c in active if c.default_amount]
    plan.needs_amount = [PlanLine(str(c.id), c.full_name, c.currency, c.payout_method, None, needs_amount=True)
                         for c in active if not c.default_amount]
    wallet = {k: Decimal(v) for k, v in plan.wallet.items()}
    currencies = {c.currency for c in priced} | set(wallet)
    try:
        rates = usd_rates_for(runtime, employer, currencies)
        payroll = [ContractorPayroll(c.full_name, to_decimal(c.default_amount), c.currency.upper(),
                                     c.payout_method) for c in priced]
        a = assess_payroll(payroll, rates, wallet, PAYROLL_POLICY)
        _fill_amounts(plan, a, priced, rates)
    except RefusalError as exc:
        plan.status = "RATE_UNAVAILABLE"
        plan.message = (f"Cannot price this payroll right now: {exc}" if "fee schedule" in str(exc)
                        else f"A live exchange rate is unavailable right now, so amounts are not shown. {exc}")
        plan.lines = []
        return plan
    plan.is_short = bool(priced) and a.shortfall_usd > 0
    plan.status = classify(has_lines=bool(priced), has_schedule=pay_date is not None,
                           is_short=plan.is_short, days_until_fund_by=plan.days_until_fund_by)
    plan.message = _message(plan)
    return plan


def _fill_amounts(plan: FundingPlan, a: PayrollAssessment, priced, rates: dict[str, Decimal]) -> None:
    for c in priced:
        ccy = c.currency.upper()
        fee = PAYROLL_POLICY.payout_fee(ccy, c.payout_method)
        rate = to_decimal(rates[ccy])
        plan.lines.append(PlanLine(str(c.id), c.full_name, ccy, c.payout_method, round_money(c.default_amount),
                                   fee=round_money(fee), amount_usd=round_money(to_decimal(c.default_amount) * rate),
                                   fee_usd=round_money(fee * rate)))
    plan.by_currency = [{"currency": r.currency, "payouts": r.payouts, "payroll": r.payroll, "fees": r.fees,
                         "required": r.required, "required_usd": r.required_usd} for r in a.by_currency]
    plan.payroll_usd = a.payroll_usd
    plan.run_fee_usd = a.platform_fee_usd
    plan.reserve_floor_usd = a.reserve_floor_usd if plan.lines else Decimal("0.00")
    plan.required_in_wallet_usd = round_money(a.required_usd + a.reserve_floor_usd) if plan.lines else Decimal("0.00")
    plan.available_usd = a.available_usd
    plan.gap_usd = a.shortfall_usd
    non_usd = sum((r.required_usd for r in a.by_currency if r.currency != "USD"), Decimal("0"))
    plan.recommended_transfer_usd = recommended_transfer(a.shortfall_usd, non_usd)


# ---------------------------------------------------------------------------- org-wide
def plan_organization(db: Session, runtime, organization_id: uuid.UUID, today: date | None = None, *,
                      with_wire: bool = True) -> dict:
    today = today or date.today()
    employers = db.scalars(select(Employer).options(selectinload(Employer.contractors))
                           .where(Employer.organization_id == organization_id)
                           .order_by(Employer.created_at)).all()
    plans = [plan_employer(db, runtime, e, today, with_wire=with_wire) for e in employers]
    return {"today": today.isoformat(), "summary": summarize(plans), "plans": [p.as_dict() for p in plans]}


def summarize(plans: list[FundingPlan]) -> dict:
    counts = {s: 0 for s in STATUSES}
    for p in plans:
        counts[p.status] += 1
    short = [p for p in plans if p.is_short]
    return {
        "employers": len(plans), "counts": counts,
        "short": len(short), "overdue": counts["OVERDUE"], "funded": counts["FUNDED"],
        # A plain sum of each employer's own gap: gaps never offset one another across silos.
        "total_to_collect_usd": money_str(sum((p.recommended_transfer_usd for p in short), Decimal("0"))),
        "total_gap_usd": money_str(sum((p.gap_usd for p in short), Decimal("0"))),
    }


def get_funding_plan(db: Session, runtime, organization_id: uuid.UUID, employer_id: uuid.UUID | None = None,
                     today: date | None = None) -> dict:
    """Read helper for callers such as the chat agent. Always tenant-scoped by ``organization_id``."""
    if employer_id is None:
        return plan_organization(db, runtime, organization_id, today)
    emp = db.scalar(select(Employer).options(selectinload(Employer.contractors))
                    .where(Employer.id == employer_id, Employer.organization_id == organization_id))
    if emp is None:
        raise LookupError("Employer not found.")
    return plan_employer(db, runtime, emp, today).as_dict()


# ---------------------------------------------------------------------------- statement
def statement(plan: dict, organization_name: str) -> dict:
    """Funding statement: a plain restatement of the plan (JSON; CSV via :func:`statement_csv`)."""
    return {"organization": organization_name, "employer": plan["employer_name"], "generated_on": plan["today"],
            "status": plan["status"], "pay_date": plan["pay_date"], "fund_by": plan["fund_by"],
            "lines": plan["lines"], "needs_amount": plan["needs_amount"], "by_currency": plan["by_currency"],
            "payroll_usd": plan["payroll_usd"], "run_fee_usd": plan["run_fee_usd"],
            "reserve_floor_usd": plan["reserve_floor_usd"], "required_in_wallet_usd": plan["required_in_wallet_usd"],
            "available_usd": plan["available_usd"], "gap_usd": plan["gap_usd"],
            "recommended_transfer_usd": plan["recommended_transfer_usd"], "wire": plan["wire"]}


def _cell(value):
    """Neutralise spreadsheet formula injection in free-text cells."""
    text = str(value) if value is not None else ""
    return "'" + text if text[:1] in ("=", "+", "-", "@") and not text.replace(".", "", 1).isdigit() else value


def statement_csv(st: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Funding statement", _cell(st["employer"])])
    w.writerow(["Prepared by", st["organization"]])
    w.writerow(["Generated on", st["generated_on"]])
    w.writerow(["Pay date", st["pay_date"] or "not scheduled"])
    w.writerow(["Fund by", st["fund_by"] or "not scheduled"])
    w.writerow([])
    w.writerow(["Contractor", "Currency", "Method", "Amount", "Payout fee", "Amount (USD)", "Fee (USD)"])
    for ln in st["lines"]:
        w.writerow([_cell(ln["name"]), ln["currency"], ln["method"], ln["amount"], ln["fee"], ln["amount_usd"], ln["fee_usd"]])
    for ln in st["needs_amount"]:
        w.writerow([_cell(ln["name"]), ln["currency"], ln["method"], "needs an amount", "", "", ""])
    w.writerow([])
    for label, key in (("Contractor pay + payout fees (USD)", "payroll_usd"), ("PaySilo run fee (USD)", "run_fee_usd"),
                       ("Reserve floor (USD)", "reserve_floor_usd"),
                       ("Needs in wallet (USD)", "required_in_wallet_usd"),
                       ("Already in wallet (USD)", "available_usd"), ("Gap (USD)", "gap_usd"),
                       ("Recommended transfer (USD)", "recommended_transfer_usd")):
        w.writerow([label, st[key]])
    wire = st.get("wire")
    if wire and wire.get("status") == "READY":
        w.writerow([])
        w.writerow(["Send funds to"])
        for label, key in (("Account name", "account_name"), ("Account number", "account_number"),
                           ("SWIFT/BIC", "swift_code"), ("Bank", "bank_name"), ("Reference", "reference")):
            if wire.get(key):
                w.writerow([label, wire[key]])
        for r in wire.get("routing", []):
            w.writerow([r["label"], r["value"]])
    return out.getvalue()
