"""Chat input screening: cross-tenant and instruction-override attempts.

This is a first, deterministic line of defence — the structural defence is
stronger: tools are built from the session's organization id, so even a
fully jailbroken model cannot read or touch another tenant's data. The
screener exists so obvious attacks are refused in plain language and
logged (``chat_security_events``) instead of being passed to the model.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ChatSecurityEvent, Contractor, Employer

_OVERRIDE_PATTERNS = [
    r"ignore (your|the|all|previous|prior) (rules|instructions|guidelines|policy|policies)",
    r"disregard (your|the|all) (rules|instructions)",
    r"forget (your|the) (rules|instructions|system prompt)",
    r"(reveal|show|print|tell me) (your|the) (system prompt|instructions|rules)",
    r"you are now (a|an|in)\b",
    r"developer mode",
    r"jailbreak",
]
_CROSS_TENANT_PATTERNS = [
    r"another (organization|org|company|tenant|employer)'?s (wallet|funds|money|account|balance)",
    r"other (organization|org|company|tenant)'?s (wallet|funds|money|account)",
    r"from a different (organization|org|company|tenant|employer)",
]
_PAY_FROM_RE = re.compile(
    r"pay\s+(?P<who>.+?)\s+from\s+(?P<source>.+?)(?:'s)?\s+(wallet|funds|money|account)\b", re.IGNORECASE)


def screen_message(db: Session, organization_id: uuid.UUID, message: str) -> tuple[str, str] | None:
    """Return (kind, refusal_text) if the message must be refused, else None."""
    lowered = message.casefold()
    for pattern in _OVERRIDE_PATTERNS:
        if re.search(pattern, lowered):
            return ("instruction_override",
                    "I cannot set aside PaySilo's rules. Tenant isolation and human, TAP-signed "
                    "approval for every payment are built into the system, not preferences I can "
                    "override. I am happy to help with questions or actions inside your own "
                    "organization instead.")
    for pattern in _CROSS_TENANT_PATTERNS:
        if re.search(pattern, lowered):
            return ("cross_tenant",
                    "I cannot use another organization's money or data. Every employer wallet in "
                    "PaySilo is a separate silo, and I only have access to your own organization. "
                    "This attempt has been logged.")
    m = _PAY_FROM_RE.search(message)
    if m:
        who, source = m.group("who").strip(), m.group("source").strip()
        verdict = _cross_silo_payment(db, organization_id, who, source)
        if verdict:
            return verdict
    return None


def _cross_silo_payment(db: Session, organization_id: uuid.UUID, who: str, source: str) -> tuple[str, str] | None:
    """'pay <person> from <employer>'s wallet' is refused when the person is
    not that employer's contractor — paying across silos is never allowed."""
    employers = list(db.scalars(select(Employer).where(Employer.organization_id == organization_id)))
    source_emp = next((e for e in employers
                       if source.casefold() in e.name.casefold() or e.name.split()[0].casefold() == source.casefold()),
                      None)
    if source_emp is None:
        return ("cross_tenant",
                f"I cannot pay anyone from '{source}'s wallet: that is not an employer in your "
                "organization, and I have no access to other organizations' wallets. "
                "This attempt has been logged.")
    contractors = list(db.scalars(
        select(Contractor).where(Contractor.organization_id == organization_id,
                                 Contractor.is_active.is_(True))))
    person = next((c for c in contractors if who.casefold() in c.full_name.casefold()), None)
    if person is not None and person.employer_id != source_emp.id:
        owner = next((e.name for e in employers if e.id == person.employer_id), "another employer")
        return ("cross_tenant",
                f"I cannot pay {person.full_name} from {source_emp.name}'s wallet. "
                f"{person.full_name} is a contractor of {owner}, and each employer's wallet is a "
                "separate silo — money never moves between them. This attempt has been logged.")
    return None


def log_security_event(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID | None,
                       thread_id: uuid.UUID | None, kind: str, detail: str) -> None:
    db.add(ChatSecurityEvent(organization_id=organization_id, user_id=user_id,
                             thread_id=thread_id, kind=kind, detail=detail[:2000]))
    db.commit()
