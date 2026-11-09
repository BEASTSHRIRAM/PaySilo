"""Demo organization: Vertex Payroll Services and its four clients.

Seeded on startup when ``SEED_DEMO=true`` and ``APP_ENV`` is not ``live``.
Idempotent: nothing is created twice. Funding goes through the runtime, so
balances exist as real ledger entries (never typed in).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Contractor, Employer, Membership, Organization, User
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
        "name": "Acme Manufacturing GmbH", "country": "DE", "contact_name": "Ana Weber",
        "contact_email": "ana.weber@acme.example", "funding_usd": Decimal("20000"),
        "contractors": [
            {"full_name": "Lena Fischer", "country": "DE", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "DE89370400440532013000", "swift_code": "COBADEFFXXX", "default_amount": Decimal("3400")},
            {"full_name": "Tomas Rivera", "country": "ES", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "ES9121000418450200051332", "swift_code": "CAIXESBBXXX", "default_amount": Decimal("2600")},
        ],
    },
    {
        "name": "Borealis Labs AB", "country": "SE", "contact_name": "Bo Lindqvist",
        "contact_email": "bo@borealis.example", "funding_usd": Decimal("5000"),
        "contractors": [
            {"full_name": "Ingrid Solberg", "country": "SE", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "SE4550000000058398257466", "swift_code": "ESSESESSXXX", "default_amount": Decimal("6000")},
        ],
    },
    {
        "name": "Cobalt Retail LLC", "country": "US", "contact_name": "Cleo Nakamura",
        "contact_email": "cleo@cobalt.example", "funding_usd": Decimal("0"),
        "contractors": [
            {"full_name": "Sam Lee", "country": "US", "currency": "USD", "payout_method": "LOCAL",
             "account_number": "000123456789", "routing_code": "021000021", "default_amount": Decimal("1800")},
        ],
    },
    # Kit 6 shortfall story: USD 5,000 pays the smaller EUR line (plus its EUR 12.85 SWIFT fee, the USD 149
    # run fee and the USD 1,000 reserve) but not both, and not the larger line alone -> smallest-first partial.
    {
        "name": "Delta Studios BV", "country": "NL", "contact_name": "Daan de Vries",
        "contact_email": "daan@deltastudios.example", "funding_usd": Decimal("5000"),
        "contractors": [
            {"full_name": "Noor van Dijk", "country": "NL", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "NL91ABNA0417164300", "swift_code": "ABNANL2AXXX", "default_amount": Decimal("2200")},
            {"full_name": "Pieter Jansen", "country": "NL", "currency": "EUR", "payout_method": "SWIFT",
             "iban": "NL02ABNA0123456789", "swift_code": "INGBNL2AXXX", "default_amount": Decimal("3600")},
        ],
    },
]


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
            continue
        emp = Employer(organization_id=org.id, name=spec["name"], legal_name=spec["name"], country=spec["country"],
                       contact_name=spec["contact_name"], contact_email=spec["contact_email"])
        s.add(emp)
        s.flush()
        for c in spec["contractors"]:
            s.add(Contractor(organization_id=org.id, employer_id=emp.id, **c))
        s.flush()
        runtime.ensure_account(s, emp)
        if spec["funding_usd"] > 0:
            runtime.fund(s, emp, spec["funding_usd"], reference="seed")
        logger.info("Seeded demo employer %s", emp.name)
    s.commit()
    return org
