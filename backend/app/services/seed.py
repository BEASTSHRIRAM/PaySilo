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

from ..db.models import Contractor, ContractorChange, Employer, Membership, Organization, User
from ..security import hash_password
from .runtime import PlatformRuntime

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
        "contractors": [
            {"full_name": "Ingrid Solberg", "country": "SE", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "SE4550000000058398257466", "swift_code": "ESSESESSXXX", "default_amount": Decimal("6000")},
        ],
    },
    {
        "name": "Cobalt Retail LLC", "pay_frequency": "WEEKLY", "pay_in_days": 3, "country": "US", "contact_name": "Cleo Nakamura",
        "contact_email": "cleo@cobalt.example", "funding_usd": Decimal("0"),
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
        "Sam Lee": dict(tax_status="US_PERSON", tax_form_type="W9", tax_form_status="RECEIVED",
                        tax_form_received_at=today - timedelta(days=200)),
        "Lena Fischer": dict(tax_status="NON_US", tax_form_type="W8BEN", tax_form_status="RECEIVED",
                             tax_form_received_at=today - timedelta(days=120),
                             tax_form_expires_at=today + timedelta(days=365 * 3 - 120)),
        "Noor van Dijk": dict(tax_status="NON_US", tax_form_type="W8BEN", tax_form_status="RECEIVED",
                              tax_form_received_at=today - timedelta(days=1065),
                              tax_form_expires_at=today + timedelta(days=30)),  # expiring soon
        "Tomas Rivera": dict(tax_status="NON_US"),  # form still missing
        "Pieter Jansen": dict(tax_status="NON_US", tax_form_type="W8BEN", tax_form_status="RECEIVED",
                              tax_form_received_at=today - timedelta(days=90),
                              tax_form_expires_at=today + timedelta(days=365 * 3 - 90),
                              bank_verification_status="PENDING_VERIFICATION"),  # bank change awaiting a second person
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
                       contact_name=spec["contact_name"], contact_email=spec["contact_email"])
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
    _seed_contractor_trust(s, org, user)
    s.commit()
    return org
