"""Demo organization: Vertex Payroll Services and its four clients.

Seeded on startup when ``SEED_DEMO=true`` and ``APP_ENV`` is not ``live``.
Idempotent: nothing is created twice. Funding goes through the runtime, so
balances exist as real ledger entries (never typed in).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    BenefitBudget,
    BenefitRequest,
    Contractor,
    ContractorChange,
    Employer,
    Membership,
    OnboardingBudget,
    OnboardingState,
    OnboardingKit,
    Organization,
    TravelBudget,
    TripRequest,
    User,
)
from ..security import hash_password
from .benefits import BenefitError, create_request, current_period, decide_request, execute_purchase
from .onboarding import OnboardingError
from .onboarding import create_kit as create_onboarding_kit
from .onboarding import decide_kit as decide_onboarding_kit
from .onboarding import purchase_all as purchase_onboarding_all
from .runtime import PlatformRuntime
from .travel import TravelError
from .travel import create_trip as create_travel_trip
from .travel import decide_trip as decide_travel_trip
from .travel import purchase_all as purchase_travel_all

logger = logging.getLogger(__name__)

DEMO_ORG_NAME = "Vertex Payroll Services"
DEMO_ORG_SLUG = "vertex-payroll"
DEMO_EMAIL = "demo@paysilo.test"
DEMO_PASSWORD = "SiloDemo2026"
DEMO_USER_NAME = "Priya Raman"

DEMO_EMPLOYERS: list[dict] = [
    {
        "name": "Acme Manufacturing GmbH", "pay_frequency": "MONTHLY", "pay_in_days": 12, "country": "DE", "contact_name": "Ana Weber",
        "contact_email": "ana.weber@acme.example", "funding_usd": Decimal("20000"),
        "contractors": [
            {"full_name": "Lena Fischer", "country": "DE", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "DE89370400440532013000", "swift_code": "COBADEFFXXX", "default_amount": Decimal("3400")},
            {"full_name": "Tomas Rivera", "country": "ES", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "ES9121000418450200051332", "swift_code": "CAIXESBBXXX", "default_amount": Decimal("2600")},
        ],
    },
    {
        "name": "Borealis Labs AB", "pay_frequency": "BIWEEKLY", "pay_in_days": 10, "country": "SE", "contact_name": "Bo Lindqvist",
        "contact_email": "bo@borealis.example", "funding_usd": Decimal("5000"),
        "settlement_mode": "ALL_OR_NOTHING",
        "contractors": [
            {"full_name": "Ingrid Solberg", "country": "SE", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "SE4550000000058398257466", "swift_code": "ESSESESSXXX", "default_amount": Decimal("6000")},
        ],
    },
    {
        "name": "Cobalt Retail LLC", "pay_frequency": "WEEKLY", "pay_in_days": 3, "country": "US", "contact_name": "Cleo Nakamura",
        "contact_email": "cleo@cobalt.example", "funding_usd": Decimal("0"),
        "settlement_mode": "ALL_OR_NOTHING",
        "contractors": [
            {"full_name": "Sam Lee", "country": "US", "currency": "USD", "payout_method": "LOCAL",
             "account_number": "000123456789", "routing_code": "021000021", "default_amount": Decimal("1800")},
        ],
    },
    # Kit 6 shortfall story: USD 5,000 pays the smaller EUR line (plus its EUR 12.85 SWIFT fee, the USD 149
    # run fee and the USD 1,000 reserve) but not both, and not the larger line alone -> smallest-first partial.
    {
        "name": "Delta Studios BV", "pay_frequency": "MONTHLY", "pay_in_days": 6, "country": "NL", "contact_name": "Daan de Vries",
        "contact_email": "daan@deltastudios.example", "funding_usd": Decimal("5000"),
        "contractors": [
            {"full_name": "Noor van Dijk", "country": "NL", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "NL91ABNA0417164300", "swift_code": "ABNANL2AXXX", "default_amount": Decimal("2200")},
            {"full_name": "Pieter Jansen", "country": "NL", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "NL02ABNA0123456789", "swift_code": "INGBNL2AXXX", "default_amount": Decimal("3600")},
        ],
    },
]


def _seed_contractor_trust(s: Session, org: Organization, user: User) -> None:
    """Varied tax / verification states so the compliance page has something to show.

    Only contractors still at the defaults (tax status UNKNOWN) are touched, so edits survive restarts.
    No payroll runs are seeded (existing tests assert a fresh org has none): the 1099 watch lights up
    after real payments to a US-person contractor reach the configured threshold.
    """
    today = datetime.now(UTC).date()
    specs = {
        # Sam Lee (Cobalt, not part of the Delta shortfall or Acme payroll stories) carries the demo bank change
        # awaiting a second person, so every seeded payroll story still pays as documented.
        "Sam Lee": dict(tax_status="US_PERSON", tax_form_type="W9", tax_form_status="RECEIVED",
                        tax_form_received_at=today - timedelta(days=200),
                        bank_verification_status="PENDING_VERIFICATION"),
        "Lena Fischer": dict(tax_status="NON_US", tax_form_type="W8BEN", tax_form_status="RECEIVED",
                             tax_form_received_at=today - timedelta(days=120),
                             tax_form_expires_at=today + timedelta(days=365 * 3 - 120)),
        "Noor van Dijk": dict(tax_status="NON_US", tax_form_type="W8BEN", tax_form_status="RECEIVED",
                              tax_form_received_at=today - timedelta(days=1065),
                              tax_form_expires_at=today + timedelta(days=30)),  # expiring soon
        "Tomas Rivera": dict(tax_status="NON_US"),  # form still missing
        "Pieter Jansen": dict(tax_status="NON_US", tax_form_type="W8BEN", tax_form_status="RECEIVED",
                              tax_form_received_at=today - timedelta(days=90),
                              tax_form_expires_at=today + timedelta(days=365 * 3 - 90)),
    }
    for c in s.scalars(select(Contractor).where(Contractor.organization_id == org.id,
                                                Contractor.full_name.in_(list(specs)))):
        if c.tax_status != "UNKNOWN":
            continue
        for key, value in specs[c.full_name].items():
            setattr(c, key, value)
        if c.bank_verification_status == "PENDING_VERIFICATION":
            c.bank_details_changed_at = datetime.now(UTC) - timedelta(hours=5)
            s.add(ContractorChange(organization_id=org.id, contractor_id=c.id, change_group="bank",
                                   summary="Bank details submitted for verification (demo data)", changed_by_id=None))


def _move_demo_bank_change(s: Session, org: Organization) -> None:
    """Older demo databases held the demo bank change on Pieter Jansen (the Delta shortfall story needs him
    payable). Release only that seeded marker and park it on Sam Lee; real edits are never touched."""
    pieter = s.scalar(select(Contractor).where(Contractor.organization_id == org.id,
                                               Contractor.full_name == "Pieter Jansen"))
    if pieter is None or pieter.bank_verification_status != "PENDING_VERIFICATION" or pieter.bank_change_requested_by_id is not None:
        return
    marker = s.scalar(select(ContractorChange).where(
        ContractorChange.contractor_id == pieter.id, ContractorChange.summary.like("%(demo data)%")))
    if marker is None:
        return
    pieter.bank_verification_status, pieter.bank_details_changed_at = "VERIFIED", None
    s.delete(marker)
    sam = s.scalar(select(Contractor).where(Contractor.organization_id == org.id, Contractor.full_name == "Sam Lee"))
    if sam is not None and sam.bank_verification_status == "VERIFIED" and sam.bank_details_changed_at is None:
        sam.bank_verification_status = "PENDING_VERIFICATION"
        sam.bank_details_changed_at = datetime.now(UTC) - timedelta(hours=5)
        s.add(ContractorChange(organization_id=org.id, contractor_id=sam.id, change_group="bank",
                               summary="Bank details submitted for verification (demo data)", changed_by_id=None))


def _fill_schedule(emp: Employer, spec: dict) -> None:
    """Funding-planner defaults, only into fields that are still empty (never overwrites a user's edits)."""
    if emp.pay_frequency is None and emp.next_pay_date is None and spec.get("pay_frequency"):
        emp.pay_frequency = spec["pay_frequency"]
        emp.next_pay_date = date.today() + timedelta(days=spec["pay_in_days"])
    if not emp.contact_email:
        emp.contact_email = spec["contact_email"]


def seed_demo(s: Session, runtime: PlatformRuntime) -> Organization:
    org = s.scalar(select(Organization).where(Organization.slug == DEMO_ORG_SLUG))
    if org is None:
        org = Organization(name=DEMO_ORG_NAME, slug=DEMO_ORG_SLUG, legal_name="Vertex Payroll Services Pvt Ltd",
                           country="IN", base_currency="USD", industry="Payroll services", size_band="11-50",
                           website="https://vertexpayroll.example", is_demo=True)
        s.add(org)
        s.flush()
    user = s.scalar(select(User).where(User.email == DEMO_EMAIL))
    if user is None:
        user = User(email=DEMO_EMAIL, full_name=DEMO_USER_NAME, password_hash=hash_password(DEMO_PASSWORD))
        s.add(user)
        s.flush()
    if not s.scalar(select(Membership.id).where(Membership.user_id == user.id,
                                               Membership.organization_id == org.id)):
        s.add(Membership(organization_id=org.id, user_id=user.id, role="owner"))

    for spec in DEMO_EMPLOYERS:
        emp = s.scalar(select(Employer).where(Employer.organization_id == org.id, Employer.name == spec["name"]))
        if emp is not None:
            _fill_schedule(emp, spec)
            continue
        emp = Employer(organization_id=org.id, name=spec["name"], legal_name=spec["name"], country=spec["country"],
                       contact_name=spec["contact_name"], contact_email=spec["contact_email"],
                       settlement_mode=spec.get("settlement_mode", "PARTIAL"))
        _fill_schedule(emp, spec)
        s.add(emp)
        s.flush()
        for c in spec["contractors"]:
            s.add(Contractor(organization_id=org.id, employer_id=emp.id, **c))
        s.flush()
        runtime.ensure_account(s, emp)
        if spec["funding_usd"] > 0:
            runtime.fund(s, emp, spec["funding_usd"], reference="seed")
        logger.info("Seeded demo employer %s", emp.name)

    s.flush()
    # The demo account offers the tour through a pill instead of auto-starting it every time.
    if not s.scalar(select(OnboardingState.id).where(OnboardingState.organization_id == org.id,
                                                    OnboardingState.user_id == user.id)):
        s.add(OnboardingState(organization_id=org.id, user_id=user.id, status="SKIPPED"))
    _seed_contractor_trust(s, org, user)
    _move_demo_bank_change(s, org)
    _seed_benefits(s, runtime, org, user)
    _seed_spend(s, runtime, org, user)
    _seed_workflows(s, org, user)
    s.commit()
    return org


# Demo benefits (Silo Benefits / Visa VIC): budgets + three requests, one
# of them already purchased in SIMULATED mode. Idempotent even when the
# employers already exist — budgets key on (employer, currency) and
# requests on (org, employer, merchant, item).
_BENEFIT_BUDGETS: list[tuple[str, Decimal, bool]] = [
    ("Acme Manufacturing GmbH", Decimal("500.00"), True),
    ("Borealis Labs AB", Decimal("500.00"), True),
    ("Cobalt Retail LLC", Decimal("250.00"), False),
]

_BENEFIT_REQUESTS: list[dict] = [
    {"employer": "Acme Manufacturing GmbH", "contractor": "Lena Fischer", "merchant": "Coursera",
     "item": "Advanced Python for Data Engineering (specialization)", "amount": Decimal("149.00"),
     "purchase": False},
    {"employer": "Acme Manufacturing GmbH", "contractor": "Tomas Rivera", "merchant": "O'Reilly Media",
     "item": "Platform Engineering book bundle", "amount": Decimal("59.00"), "purchase": True},
    {"employer": "Borealis Labs AB", "contractor": "Ingrid Solberg", "merchant": "LinkedIn Learning",
     "item": "Payroll compliance fundamentals (course)", "amount": Decimal("120.00"), "purchase": False},
]


def _seed_benefits(s: Session, runtime: PlatformRuntime, org: Organization, user: User) -> None:
    employers: dict[str, Employer] = {}
    for name, cap, enabled in _BENEFIT_BUDGETS:
        emp = s.scalar(select(Employer).where(Employer.organization_id == org.id, Employer.name == name))
        if emp is None:
            continue
        employers[name] = emp
        budget = s.scalar(select(BenefitBudget).where(BenefitBudget.employer_id == emp.id,
                                                      BenefitBudget.currency == "USD"))
        if budget is None:
            s.add(BenefitBudget(organization_id=org.id, employer_id=emp.id, currency="USD",
                                monthly_cap=cap, per_tx_limit=None, spent_this_period=Decimal("0.00"),
                                period=current_period(), enabled=enabled))
            s.flush()

    for spec in _BENEFIT_REQUESTS:
        emp = employers.get(spec["employer"]) or s.scalar(
            select(Employer).where(Employer.organization_id == org.id, Employer.name == spec["employer"]))
        if emp is None:
            continue
        existing = s.scalar(select(BenefitRequest).where(
            BenefitRequest.organization_id == org.id, BenefitRequest.employer_id == emp.id,
            BenefitRequest.merchant == spec["merchant"], BenefitRequest.item == spec["item"]))
        if existing is not None and (not spec["purchase"] or existing.status == "PURCHASED"):
            continue
        contractor = s.scalar(select(Contractor).where(Contractor.employer_id == emp.id,
                                                       Contractor.full_name == spec["contractor"]))
        req = existing
        if req is None:
            req = create_request(
                s, organization_id=org.id, employer=emp, contractor=contractor,
                contractor_name=spec["contractor"], merchant=spec["merchant"], item=spec["item"],
                amount=spec["amount"], currency="USD",
                requested_by_id=user.id, requested_by_email=user.email)
        if spec["purchase"]:
            # Driven through the service like a real purchase. A refusal
            # (e.g. VIC configured but its CLI missing) is logged, never
            # fatal: the request stays behind, honestly APPROVED.
            try:
                if req.status == "REQUESTED":
                    decide_request(s, request=req, decision="approve", user=user)
                # Live VIC is two-phase and needs a real human in Airi: never
                # seed a mandate (or a fake PURCHASED row) in live mode.
                if req.status in ("APPROVED", "FAILED") and not getattr(runtime.vic, "two_phase", False):
                    execute_purchase(s, runtime, request=req, user=user)
            except BenefitError as exc:
                logger.warning("Seeded benefit purchase %s / %s was not completed: %s",
                               spec["merchant"], spec["item"], exc)
    s.flush()


# ---------------------------------------------------------------------------
# Spend categories (Onboarding Kit + Travel) and workflow templates
# ---------------------------------------------------------------------------

_SPEND_BUDGETS: list[tuple[str, Decimal, Decimal, bool]] = [
    # (employer, onboarding cap, travel cap, enabled)
    ("Acme Manufacturing GmbH", Decimal("1000.00"), Decimal("2000.00"), True),
    ("Borealis Labs AB", Decimal("500.00"), Decimal("500.00"), True),
    ("Cobalt Retail LLC", Decimal("250.00"), Decimal("250.00"), False),
]


def _seed_spend(s: Session, runtime: PlatformRuntime, org: Organization, user: User) -> None:
    employers: dict[str, Employer] = {}
    for name, onboarding_cap, travel_cap, enabled in _SPEND_BUDGETS:
        emp = s.scalar(select(Employer).where(Employer.organization_id == org.id, Employer.name == name))
        if emp is None:
            continue
        employers[name] = emp
        if s.scalar(select(OnboardingBudget).where(OnboardingBudget.employer_id == emp.id,
                                                   OnboardingBudget.currency == "USD")) is None:
            s.add(OnboardingBudget(organization_id=org.id, employer_id=emp.id, currency="USD",
                                   monthly_cap=onboarding_cap, per_tx_limit=None,
                                   spent_this_period=Decimal("0.00"), period=current_period(),
                                   enabled=enabled))
        if s.scalar(select(TravelBudget).where(TravelBudget.employer_id == emp.id,
                                               TravelBudget.currency == "USD")) is None:
            s.add(TravelBudget(organization_id=org.id, employer_id=emp.id, currency="USD",
                               monthly_cap=travel_cap, per_tx_limit=None,
                               spent_this_period=Decimal("0.00"), period=current_period(),
                               enabled=enabled))
        s.flush()

    acme = employers.get("Acme Manufacturing GmbH")
    if acme is None:
        return

    def _contractor(employer: Employer, full_name: str) -> Contractor | None:
        return s.scalar(select(Contractor).where(Contractor.employer_id == employer.id,
                                                 Contractor.full_name == full_name))

    # --- Kits -------------------------------------------------------------
    marta_kit = s.scalar(select(OnboardingKit).where(
        OnboardingKit.organization_id == org.id, OnboardingKit.employer_id == acme.id,
        OnboardingKit.contractor_name == "Marta Kowalski"))
    if marta_kit is None:
        marta_kit = create_onboarding_kit(
            s, organization_id=org.id, employer=acme, contractor=None,
            contractor_name="Marta Kowalski",
            items=[
                {"kind": "BACKGROUND_CHECK", "label": "Background verification", "merchant": "checkr.com",
                 "amount": Decimal("40.00"), "currency": "USD"},
                {"kind": "SAAS_SEAT", "label": "GitHub + Notion seats", "merchant": "github.com",
                 "amount": Decimal("36.00"), "currency": "USD"},
                {"kind": "GIFT_CARD", "label": "Welcome gift card", "merchant": "amazon.com",
                 "amount": Decimal("50.00"), "currency": "USD"},
            ],
            created_by_id=user.id, created_by_email=user.email)
    if marta_kit.status != "COMPLETED":
        try:
            if marta_kit.status == "DRAFT":
                decide_onboarding_kit(s, kit=marta_kit, decision="approve", user=user)
            if marta_kit.status in ("APPROVED", "PARTIALLY_PURCHASED") and not getattr(runtime.vic, "two_phase", False):
                purchase_onboarding_all(s, runtime, kit=marta_kit, user=user)
        except OnboardingError as exc:
            logger.warning("Seeded onboarding kit for Marta Kowalski was not completed: %s", exc)

    lena = _contractor(acme, "Lena Fischer")
    draft_kit = s.scalar(select(OnboardingKit).where(
        OnboardingKit.organization_id == org.id, OnboardingKit.employer_id == acme.id,
        OnboardingKit.contractor_name == "Lena Fischer", OnboardingKit.status == "DRAFT"))
    if draft_kit is None:
        create_onboarding_kit(
            s, organization_id=org.id, employer=acme, contractor=lena,
            contractor_name="Lena Fischer",
            items=[
                {"kind": "BACKGROUND_CHECK", "label": "Background verification", "merchant": "checkr.com",
                 "amount": Decimal("40.00"), "currency": "USD"},
                {"kind": "PERIPHERAL", "label": "Keyboard and mouse", "merchant": "logitech.com",
                 "amount": Decimal("120.00"), "currency": "USD"},
            ],
            created_by_id=user.id, created_by_email=user.email)

    # --- Trips ------------------------------------------------------------
    tomas = _contractor(acme, "Tomas Rivera")
    munich = s.scalar(select(TripRequest).where(
        TripRequest.organization_id == org.id, TripRequest.employer_id == acme.id,
        TripRequest.contractor_name == "Tomas Rivera", TripRequest.destination == "Munich",
        TripRequest.purpose == "Munich client workshop"))
    if munich is None:
        munich = create_travel_trip(
            s, organization_id=org.id, employer=acme, contractor=tomas,
            contractor_name="Tomas Rivera", destination="Munich",
            start_date=date(2026, 11, 20), end_date=date(2026, 11, 22),
            purpose="Munich client workshop",
            components=[
                {"kind": "TRANSPORT", "label": "Airport transfer", "merchant": "flughafen-muenchen.de",
                 "amount": Decimal("60.00"), "currency": "USD"},
                {"kind": "LODGING", "label": "Hotel night", "merchant": "booking.com",
                 "amount": Decimal("180.00"), "currency": "USD"},
                {"kind": "PER_DIEM", "label": "Per-diem card", "merchant": "wise.com",
                 "amount": Decimal("100.00"), "currency": "USD"},
            ],
            created_by_id=user.id, created_by_email=user.email)
    if munich.status != "COMPLETED":
        try:
            if munich.status == "REQUESTED":
                decide_travel_trip(s, trip=munich, decision="approve", override_caps=False, user=user)
            if munich.status in ("APPROVED", "PARTIALLY_PURCHASED") and not getattr(runtime.vic, "two_phase", False):
                purchase_travel_all(s, runtime, trip=munich, user=user)
        except TravelError as exc:
            logger.warning("Seeded Munich trip was not completed: %s", exc)

    berlin = s.scalar(select(TripRequest).where(
        TripRequest.organization_id == org.id, TripRequest.employer_id == acme.id,
        TripRequest.contractor_name == "Lena Fischer", TripRequest.destination == "Berlin",
        TripRequest.purpose == "Berlin customer onsite"))
    if berlin is None:
        berlin = create_travel_trip(
            s, organization_id=org.id, employer=acme, contractor=lena,
            contractor_name="Lena Fischer", destination="Berlin",
            start_date=date(2026, 12, 5), end_date=date(2026, 12, 6),
            purpose="Berlin customer onsite",
            components=[
                {"kind": "TRANSPORT", "label": "Flight", "merchant": "lufthansa.com",
                 "amount": Decimal("450.00"), "currency": "USD"},
                {"kind": "LODGING", "label": "Hotel night", "merchant": "booking.com",
                 "amount": Decimal("180.00"), "currency": "USD"},
            ],
            created_by_id=user.id, created_by_email=user.email)
    if berlin.status == "REQUESTED":
        try:
            decide_travel_trip(s, trip=berlin, decision="approve", override_caps=False, user=user)
        except TravelError as exc:
            logger.warning("Seeded Berlin trip was not approved: %s", exc)
    s.flush()
# Workflow templates: # Four enabled templates, created only if the org has NO workflows at
# all (idempotent). Graphs are React Flow-shaped, laid out left->right.
# ---------------------------------------------------------------------------

def _wf_graph(steps: list[tuple[str, str, dict]]) -> dict:
    """Build a linear React Flow graph from (id, type, data) steps, x += 220."""
    nodes = [{"id": node_id, "type": node_type,
              "position": {"x": 40 + i * 220, "y": 120}, "data": data}
             for i, (node_id, node_type, data) in enumerate(steps)]
    edges = [{"id": f"e{i}-{steps[i][0]}-{steps[i + 1][0]}", "source": steps[i][0],
              "target": steps[i + 1][0]} for i in range(len(steps) - 1)]
    return {"nodes": nodes, "edges": edges}


_WORKFLOW_TEMPLATES: list[tuple[str, str, dict]] = [
    (
        "New Contractor Onboarding",
        "Draft a contractor's onboarding kit, request approval, notify deciders, then wait for the human decision.",
        _wf_graph([
            ("trigger", "trigger.contractor_joined", {"label": "Contractor joined"}),
            ("draft-kit", "action.draft_onboarding_kit", {"label": "Draft onboarding kit"}),
            ("approval", "action.create_approval_request", {"label": "Create approval request"}),
            ("email-deciders", "action.send_email",
             {"label": "Email deciders", "template": "approval_requested", "recipient": "decider"}),
            ("gate", "gate.wait_for_approval", {"label": "Wait for approval", "entity": "kit"}),
        ]),
    ),
    (
        "Kit Completion Notice",
        "When an onboarding kit is completed, email the contractor.",
        _wf_graph([
            ("trigger", "trigger.kit_completed", {"label": "Kit completed"}),
            ("email-contractor", "action.send_email",
             {"label": "Email contractor", "template": "kit_completed", "recipient": "contractor"}),
        ]),
    ),
    (
        "New Employer Welcome",
        "Welcome a new employer and create a funding reminder.",
        _wf_graph([
            ("trigger", "trigger.employer_added", {"label": "Employer added"}),
            ("email-welcome", "action.send_email",
             {"label": "Welcome email", "template": "employer_welcome", "recipient": "employer_contact"}),
            ("reminder", "action.create_funding_reminder",
             {"label": "Funding reminder",
              "note": "Fund {employer_name}'s wallet before its first payroll run."}),
        ]),
    ),
    (
        "Payroll Held Alert",
        "When payroll is held, open an exception, alert the employer contact, and post a chat notice.",
        _wf_graph([
            ("trigger", "trigger.payroll_held", {"label": "Payroll held"}),
            ("exception", "action.create_exception",
             {"label": "Create exception", "title": "Payroll held for {employer_name}",
              "detail": "Payroll is held: shortfall {currency} {amount}."}),
            ("email-contact", "action.send_email",
             {"label": "Alert employer contact", "template": "payroll_held_alert",
              "recipient": "employer_contact"}),
            ("chat-notice", "action.post_chat_notice",
             {"label": "Post chat notice",
              "message": "Payroll for {employer_name} is held: shortfall {currency} {amount}. Exception created."}),
        ]),
    ),
]


def _wf_node(node_id: str, node_type: str, x: int, y: int, data: dict) -> dict:
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": y}, "data": data}


def _wf_edge(edge_id: str, source: str, target: str, handle: str | None = None) -> dict:
    edge = {"id": edge_id, "source": source, "target": target}
    if handle:
        edge["sourceHandle"] = handle
    return edge


_MONTHLY_PAYROLL_DAY = {
    "nodes": [
        _wf_node("trigger", "trigger.schedule", 40, 160,
                 {"label": "Monthly (day 3)", "cadence": "monthly", "day": 3, "fan_out": "employers"}),
        _wf_node("prepare", "action.prepare_payroll_run", 260, 160, {"label": "Prepare payroll run"}),
        _wf_node("check", "condition.field_check", 480, 160,
                 {"label": "Shortfall is zero?", "field": "shortfall", "operator": "eq", "value": "0"}),
        _wf_node("gate", "gate.wait_for_approval", 700, 70, {"label": "Morning Brief approval", "entity": "run"}),
        _wf_node("notice", "action.post_chat_notice", 920, 70,
                 {"label": "Approval notice",
                  "message": "Payroll for {employer_name} approved from the Morning Brief — execution continues in the payroll module."}),
        _wf_node("exception", "action.create_exception", 700, 260,
                 {"label": "Shortfall exception", "title": "Funding shortfall — {employer_name}",
                  "detail": "Prepared payroll is short {currency} {shortfall}."}),
        _wf_node("email", "action.send_email", 920, 260,
                 {"label": "Alert employer contact", "template": "payroll_held_alert",
                  "recipient": "employer_contact"}),
    ],
    "edges": [
        _wf_edge("e1", "trigger", "prepare"), _wf_edge("e2", "prepare", "check"),
        _wf_edge("e3", "check", "gate", "true"), _wf_edge("e4", "gate", "notice"),
        _wf_edge("e5", "check", "exception", "false"), _wf_edge("e6", "exception", "email"),
    ],
}

_HELD_PAYROLL_RESCUE = {
    "nodes": [
        _wf_node("trigger", "trigger.funds_received", 40, 160, {"label": "Funds received"}),
        _wf_node("revalidate", "action.revalidate_held_run", 260, 160, {"label": "Revalidate held run"}),
        _wf_node("check", "condition.field_check", 480, 160,
                 {"label": "Now funded?", "field": "now_funded", "operator": "eq", "value": "true"}),
        _wf_node("notice-ok", "action.post_chat_notice", 700, 60,
                 {"label": "Now validates notice",
                  "message": "Funds received for {employer_name}: the held payroll lines now fit the wallet. A continuation run is waiting for an approver."}),
        _wf_node("email", "action.send_email", 920, 60,
                 {"label": "Email deciders", "template": "approval_requested", "recipient": "decider"}),
        _wf_node("gate", "gate.wait_for_approval", 1140, 60,
                 {"label": "Wait for approval (continuation run)", "entity": "run"}),
        _wf_node("notice-approved", "action.post_chat_notice", 1360, 60,
                 {"label": "Approved notice",
                  "message": "Held payroll for {employer_name} approved to resume."}),
        _wf_node("notice-still", "action.post_chat_notice", 700, 280,
                 {"label": "Still short notice",
                  "message": "Funds received for {employer_name}, but the held payroll is still short {currency} {shortfall}."}),
    ],
    "edges": [
        _wf_edge("e1", "trigger", "revalidate"), _wf_edge("e2", "revalidate", "check"),
        _wf_edge("e3", "check", "notice-ok", "true"), _wf_edge("e4", "notice-ok", "email"),
        _wf_edge("e5", "email", "gate"), _wf_edge("e6", "gate", "notice-approved"),
        _wf_edge("e7", "check", "notice-still", "false"),
    ],
}

_FRIDAY_FUNDING_REMINDER = {
    "nodes": [
        _wf_node("trigger", "trigger.schedule", 40, 160,
                 {"label": "Weekly (Friday)", "cadence": "weekly", "day": 4, "fan_out": "employers"}),
        _wf_node("prepare", "action.prepare_payroll_run", 260, 160, {"label": "Prepare payroll run"}),
        _wf_node("check", "condition.field_check", 480, 160,
                 {"label": "Shortfall above zero?", "field": "shortfall", "operator": "gt", "value": "0"}),
        _wf_node("reminder", "action.create_funding_reminder", 700, 160,
                 {"label": "Funding reminder", "note": "Top up required before payday"}),
        _wf_node("email", "action.send_email", 920, 160,
                 {"label": "Alert employer contact", "template": "payroll_held_alert",
                  "recipient": "employer_contact"}),
    ],
    "edges": [
        _wf_edge("e1", "trigger", "prepare"), _wf_edge("e2", "prepare", "check"),
        _wf_edge("e3", "check", "reminder", "true"), _wf_edge("e4", "reminder", "email"),
        # false branch intentionally has NO outgoing edge (terminal OK).
    ],
}

_WORKFLOW_TEMPLATES.extend([
    ("Monthly Payroll Day",
     "On day 3 each month, prepare every employer's payroll; funded runs park at the Morning Brief gate, shortfalls raise an exception and alert the employer.",
     _MONTHLY_PAYROLL_DAY),
    ("Held Payroll Rescue",
     "When funds arrive, re-check the employer's held payroll lines; if the wallet now covers them, prepare the continuation run (or link to the one autopilot already made), email the deciders and park at a gate until a person approves it on the run.",
     _HELD_PAYROLL_RESCUE),
    ("Friday Funding Reminder",
     "Every Friday, prepare every employer's payroll; employers with a shortfall get a funding reminder and an alert email.",
     _FRIDAY_FUNDING_REMINDER),
    ("Payslip Day",
     "When payroll settles, email each contractor their payslip receipt (one private notification per paid contractor).",
     _wf_graph([
         ("trigger", "trigger.payroll_settled", {"label": "Payroll settled"}),
         ("email-payslips", "action.send_email",
          {"label": "Email payslips", "template": "payslip_receipt", "recipient": "contractor"}),
     ])),
])


# Templates that PREPARE payroll runs or continuations on their own are seeded switched off: an open prepared run
# blocks starting another payroll for that employer, so the owner opts in (Workflows > enable).
_SEEDED_DISABLED = {"Monthly Payroll Day", "Held Payroll Rescue", "Friday Funding Reminder"}


def _seed_workflows(s: Session, org: Organization, user: User) -> None:
    from ..db.models import Workflow
    from .workflow_templates import build_payroll_schedule_graph, layout_graph
    from .workflows import trigger_type_of, validate_graph

    existing_names = set(s.scalars(
        select(Workflow.name).where(Workflow.organization_id == org.id)).all())
    for name, description, graph in _WORKFLOW_TEMPLATES:
        if name in existing_names:
            continue  # per-name idempotent: never duplicate a template
        if name == "Monthly Payroll Day":  # scoped to ONE employer (the first), like a real per-employer schedule
            first = s.scalar(select(Employer).where(Employer.organization_id == org.id).order_by(Employer.created_at))
            if first is not None:
                graph = build_payroll_schedule_graph(employer_id=str(first.id), employer_name=first.name, day=3)
                description = (f"On day 3 each month at 09:00 (Asia/Kolkata), prepare {first.name}'s payroll run, "
                               "email the approvers and wait for a person to approve; a shortfall raises a funding "
                               "reminder instead. The workflow never approves or pays. Duplicate it per employer.")
        graph = layout_graph(graph)
        errors = validate_graph(graph)
        if errors:  # a broken template must be loud in logs, never silently seeded
            logger.warning("Seed workflow %r failed validation: %s", name, errors)
            continue
        s.add(Workflow(organization_id=org.id, name=name, description=description,
                       enabled=name not in _SEEDED_DISABLED,
                       trigger_type=trigger_type_of(graph) or "", graph=graph, created_by_id=user.id))
    s.flush()


