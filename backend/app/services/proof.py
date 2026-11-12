"""Facts and integrity for proof-of-payment documents.

``line_facts`` / ``run_facts`` assemble ONLY what PaySilo really stores (pay
line, run, approvals, Airwallex request log, settlement receipt, contractor
record). Nothing is invented: an unknown value is ``None`` and the PDF says
so. The canonical JSON of those facts is signed with HMAC-SHA256 (a key
derived from the session secret under its own label, same construction as
the public receipt-link key). The public verify page recomputes the facts
from the database and compares: an altered document or changed record
no longer matches.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..airwallex.simulator import request_id_for
from ..config import Settings
from ..db.models import (
    AirwallexRequest, Contractor, Organization, PayLine, PayrollRun, ProofAccess, SettlementReceipt,
)
from ..money import money_str
from .payment_trace import explain_run_error, extract_failure_code, payment_trace

FACTS_VERSION = "1"


def _proof_key(settings: Settings) -> bytes:
    """Own label, so a proof signature can never be confused with a receipt-link or session signature."""
    return hmac.new(settings.session_secret().encode("utf-8"), b"paysilo/proof-of-payment/v1",
                    hashlib.sha256).digest()


def _iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def mask_last4(raw: str | None) -> str | None:
    raw = (raw or "").replace(" ", "")
    return f"****{raw[-4:]}" if len(raw) >= 4 else ("****" if raw else None)


def _dig(d: Any, *keys: str) -> Any:
    """First non-empty value among ``keys``, looking at the top level and one level down (live payloads nest)."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if d.get(k):
            return d[k]
    for v in d.values():
        if isinstance(v, dict):
            for k in keys:
                if v.get(k):
                    return v[k]
    return None


def _requests(db: Session, run: PayrollRun) -> list[AirwallexRequest]:
    return list(db.scalars(select(AirwallexRequest).where(
        AirwallexRequest.run_id == run.id, AirwallexRequest.organization_id == run.organization_id)
        .order_by(AirwallexRequest.id)))


def _line_core(db: Session, run: PayrollRun, line: PayLine, reqs: list[AirwallexRequest]) -> dict:
    by_rid = {r.request_id: r for r in reqs}
    contractor = db.get(Contractor, line.contractor_id) if line.contractor_id else None
    bene = by_rid.get(request_id_for(str(run.id), f"beneficiary-{line.position}"))
    bene_resp = bene.response if bene else None
    # Prefer what was sent to Airwallex for THIS payout over today's contractor record.
    raw = (_dig(bene_resp, "iban") or _dig(bene_resp, "account_number")
           or (contractor.iban or contractor.account_number if contractor else None))
    bic = _dig(bene_resp, "swift_code") or (contractor.swift_code if contractor else None)
    country = _dig(bene_resp, "country_code") or (contractor.country if contractor else None)

    create = by_rid.get(line.request_id) if line.airwallex_transfer_id else None
    transfer_status = (create.response or {}).get("status") if create else None
    transition = next((r for r in reqs if r.operation == "transition_transfer"
                       and line.airwallex_transfer_id and (r.response or {}).get("id") == line.airwallex_transfer_id), None)
    if transition and (transition.response or {}).get("status"):
        transfer_status = transition.response["status"]
    paid_at = _iso(transition.created_at) if transition and (transition.response or {}).get("status") == "PAID" else None

    fx = None
    conv = next((r for r in reqs if r.operation == "create_fx_conversion"
                 and (r.response or {}).get("buy_currency") == line.currency), None)
    if conv:
        quote = next((r for r in reqs if r.operation == "create_fx_quote"
                      and (r.response or {}).get("id") == conv.response.get("quote_id")), None)
        rate = (quote.response or {}).get("rate") if quote else None
        fx = {"scope": "run-level conversion for this currency",
              "sold": f"{conv.response.get('sell_currency', 'USD')} {money_str(conv.response['sell_amount'])}"
              if conv.response.get("sell_amount") is not None else None,
              "bought": f"{line.currency} {money_str(conv.response['buy_amount'])}"
              if conv.response.get("buy_amount") is not None else None,
              "rate": str(rate) if rate is not None else None}

    return {
        "line_id": str(line.id), "position": line.position,
        "contractor": line.beneficiary_name,
        "amount": money_str(line.amount), "currency": line.currency, "method": line.method,
        "payout_fee": money_str(line.payout_fee) if line.payout_fee is not None else None,
        "status": line.status,
        "bank": {"masked": mask_last4(raw), "kind": "IBAN" if (raw or "")[:2].isalpha() and len(raw or "") > 14 else "Account",
                 "bic": bic, "country": country},
        "airwallex": {"transfer_id": line.airwallex_transfer_id, "request_id": line.request_id,
                      "transfer_status": transfer_status,
                      "created_status": (create.response or {}).get("status") if create else None,
                      "transfer_created_at": _iso(create.created_at) if create else None,
                      "paid_at": paid_at},
        "fx": fx,
    }


def _approval(run: PayrollRun) -> dict | None:
    ok = sorted((a for a in run.approvals if a.decision == "approve"), key=lambda a: a.created_at or datetime.min.replace(tzinfo=UTC))
    if not ok:
        return None
    a = ok[0]
    return {"by": a.decided_by_email, "at": _iso(a.created_at), "tap_key_id": a.tap_key_id, "tap_verified": bool(a.verified)}


def _receipt(db: Session, run: PayrollRun) -> dict | None:
    r = db.scalar(select(SettlementReceipt).where(SettlementReceipt.run_id == run.id))
    if r is None:
        return None
    return {"chain": r.chain, "index": r.chain_index, "receipt_hash": r.receipt_hash, "tx_hash": r.tx_hash}


def _run_block(db: Session, run: PayrollRun) -> dict:
    org = db.get(Organization, run.organization_id)
    return {"agency": (org.legal_name or org.name) if org else None,
            "employer": run.employer.name, "run_id": str(run.id), "status": run.status,
            "period": run.period_label, "pay_date": run.pay_date.isoformat() if run.pay_date else None,
            "created_at": _iso(run.created_at), "settled_at": _iso(run.settled_at)}


def line_facts(db: Session, run: PayrollRun, line: PayLine) -> dict:
    reqs = _requests(db, run)
    core = _line_core(db, run, line, reqs)
    trace = payment_trace(line, run, transfer_status=core["airwallex"]["transfer_status"],
                          failure_code=extract_failure_code(run.error))
    doc = "proof" if line.status == "PAID" else "notice"
    timeline = [{"label": "Payroll run created", "at": _iso(run.created_at)}]
    appr = _approval(run)
    if appr:
        timeline.append({"label": f"Approved by {appr['by']}", "at": appr["at"]})
    aw = core["airwallex"]
    if aw["transfer_created_at"]:
        timeline.append({"label": f"Airwallex transfer created (status {aw['created_status'] or 'not recorded'})", "at": aw["transfer_created_at"]})
    if aw["paid_at"]:
        timeline.append({"label": "Airwallex reports the transfer PAID", "at": aw["paid_at"]})
    if run.settled_at:
        timeline.append({"label": "Payroll run settled in PaySilo", "at": _iso(run.settled_at)})
    notice = None
    if doc == "notice":
        exp = explain_run_error(run.error)
        notice = {"headline": trace["headline"], "why": trace["diagnosis"], "next": trace["next_step"],
                  "technical": exp.title if exp else None}
    return {"version": FACTS_VERSION, "doc": doc, **_run_block(db, run), "line": core,
            "approval": appr, "receipt": _receipt(db, run), "timeline": timeline,
            "trace": {k: trace[k] for k in ("state", "paid", "headline", "diagnosis", "next_step", "expected_window", "failure_code")},
            "notice": notice}


def run_facts(db: Session, run: PayrollRun) -> dict:
    reqs = _requests(db, run)
    lines = [_line_core(db, run, ln, reqs) for ln in sorted(run.pay_lines, key=lambda p: p.position)]
    exp = explain_run_error(run.error)
    return {"version": FACTS_VERSION, "doc": "statement", **_run_block(db, run), "lines": lines,
            "approval": _approval(run), "receipt": _receipt(db, run),
            "fee_charged_usd": money_str(run.fee_charged_usd),
            "run_error": ({"title": exp.title, "message": exp.message} if exp else None)}


# ---------------------------------------------------------------- integrity

def canonical(facts: dict) -> bytes:
    return json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def digest(facts: dict) -> str:
    return hashlib.sha256(canonical(facts)).hexdigest()


def sign(settings: Settings, facts: dict) -> str:
    return hmac.new(_proof_key(settings), canonical(facts), hashlib.sha256).hexdigest()


def make_token(settings: Settings, kind: str, object_id: uuid.UUID, facts: dict) -> str:
    """``l.<pay-line-id>.<hmac>`` (line) or ``r.<run-id>.<hmac>`` (statement)."""
    return f"{kind}.{object_id}.{sign(settings, facts)}"


def verify_url(settings: Settings, token: str, base: str | None = None) -> str:
    root = (base or settings.public_app_url or (settings.cors_origins[0] if settings.cors_origins else "")).rstrip("/")
    return f"{root}/verify/{token}"


def verify_token(db: Session, settings: Settings, token: str) -> dict:
    """Recompute the facts from the DB and compare. Invalid reveals nothing about the payment."""
    invalid = {"valid": False, "reason": "This code does not match PaySilo's records. The document may have been "
                                         "altered, or the payment record changed after it was issued."}
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] not in ("l", "r") or len(parts[2]) != 64:
        return invalid
    try:
        oid = uuid.UUID(parts[1])
    except ValueError:
        return invalid
    if parts[0] == "l":
        line = db.get(PayLine, oid)
        run = db.get(PayrollRun, line.run_id) if line else None
        facts = line_facts(db, run, line) if run else None
    else:
        run = db.get(PayrollRun, oid)
        facts = run_facts(db, run) if run else None
    if facts is None or not hmac.compare_digest(parts[2], sign(settings, facts)):
        return invalid
    return {"valid": True, "kind": "line" if parts[0] == "l" else "statement", "facts": public_summary(facts)}


def public_summary(facts: dict) -> dict:
    """What the public verify page may show: names, amounts, masked bank. No approver email, no ids of other lines' people."""
    def line_view(c: dict) -> dict:
        return {"contractor": c["contractor"], "amount": c["amount"], "currency": c["currency"], "method": c["method"],
                "status": c["status"], "bank_masked": c["bank"]["masked"],
                "transfer_id": c["airwallex"]["transfer_id"], "paid_at": c["airwallex"]["paid_at"]}
    out = {"document": facts["doc"], "agency": facts["agency"], "employer": facts["employer"],
           "run_id": facts["run_id"], "period": facts["period"], "pay_date": facts["pay_date"],
           "tap_verified": bool(facts["approval"] and facts["approval"]["tap_verified"]),
           "receipt_hash": facts["receipt"]["receipt_hash"] if facts["receipt"] else None}
    if facts["doc"] == "statement":
        out["lines"] = [line_view(c) for c in facts["lines"]]
    else:
        out["line"] = line_view(facts["line"])
        out["state"] = facts["trace"]["state"]
    return out


def record_access(db: Session, *, org_id: uuid.UUID, run_id: uuid.UUID, line_id: uuid.UUID | None,
                  user_id: uuid.UUID | None, kind: str, channel: str, facts: dict) -> None:
    db.add(ProofAccess(organization_id=org_id, run_id=run_id, pay_line_id=line_id, user_id=user_id,
                       kind=kind, channel=channel, document_digest=digest(facts)))
    db.commit()
