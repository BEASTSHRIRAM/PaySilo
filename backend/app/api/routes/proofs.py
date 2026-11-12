"""Proof-of-payment PDFs, run statements, public verification and the "where is my money?" trace.

Tenant-scoped like the evidence bundle (any role, viewers included); foreign
ids are 404. Generation is audit-logged in ``proof_accesses``. Nothing here
moves money or changes any state besides that audit row.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ...config import Settings
from ...db.models import PayLine, PayrollRun
from ...services import proof as proof_svc
from ...services.proof_pdf import render_line_pdf, render_statement_pdf
from ..deps import Tenant, current_tenant, get_db, get_settings
from .bedata import verify_receipt_token
from .runs import load_run

router = APIRouter(tags=["proofs"])


def _line_of(run: PayrollRun, line_id: uuid.UUID) -> PayLine:
    line = next((p for p in run.pay_lines if p.id == line_id), None)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pay line not found.")
    return line


def _base_url(request: Request, settings: Settings) -> str | None:
    if settings.public_app_url:
        return settings.public_app_url
    host = request.headers.get("x-forwarded-host")
    if host:
        return f"{request.headers.get('x-forwarded-proto', 'https')}://{host}"
    return None  # falls back to the first CORS origin


def _pdf(content: bytes, filename: str) -> Response:
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "contractor"


def _line_pdf(db: Session, settings: Settings, request: Request, run: PayrollRun, line: PayLine,
              user_id: uuid.UUID | None, channel: str) -> Response:
    facts = proof_svc.line_facts(db, run, line)
    token = proof_svc.make_token(settings, "l", line.id, facts)
    data = render_line_pdf(facts, sig=proof_svc.sign(settings, facts),
                           url=proof_svc.verify_url(settings, token, _base_url(request, settings)))
    proof_svc.record_access(db, org_id=run.organization_id, run_id=run.id, line_id=line.id, user_id=user_id,
                            kind=facts["doc"], channel=channel, facts=facts)
    prefix = "proof-of-payment" if facts["doc"] == "proof" else "payment-status-notice"
    return _pdf(data, f"{prefix}-{_slug(line.beneficiary_name)}-{str(line.id)[:8]}.pdf")


@router.get("/runs/{run_id}/lines/{line_id}/proof.pdf")
def line_proof_pdf(run_id: uuid.UUID, line_id: uuid.UUID, request: Request, tenant: Tenant = Depends(current_tenant),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Response:
    run = load_run(db, tenant, run_id)
    return _line_pdf(db, settings, request, run, _line_of(run, line_id), tenant.user.id, "dashboard")


@router.get("/runs/{run_id}/statement.pdf")
def run_statement_pdf(run_id: uuid.UUID, request: Request, tenant: Tenant = Depends(current_tenant),
                      db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Response:
    run = load_run(db, tenant, run_id)
    facts = proof_svc.run_facts(db, run)
    token = proof_svc.make_token(settings, "r", run.id, facts)
    data = render_statement_pdf(facts, sig=proof_svc.sign(settings, facts),
                                url=proof_svc.verify_url(settings, token, _base_url(request, settings)))
    proof_svc.record_access(db, org_id=run.organization_id, run_id=run.id, line_id=None, user_id=tenant.user.id,
                            kind="statement", channel="dashboard", facts=facts)
    return _pdf(data, f"payment-statement-{str(run.id)[:8]}.pdf")


@router.get("/runs/{run_id}/lines/{line_id}/trace")
def line_trace(run_id: uuid.UUID, line_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
               db: Session = Depends(get_db)) -> dict:
    run = load_run(db, tenant, run_id)
    line = _line_of(run, line_id)
    facts = proof_svc.line_facts(db, run, line)
    return {"line_id": str(line.id), "contractor": line.beneficiary_name,
            "amount": facts["line"]["amount"], "currency": line.currency, "method": line.method,
            "transfer_id": line.airwallex_transfer_id, **facts["trace"]}


@router.get("/receipts/{pay_line_id}/proof.pdf")
def public_receipt_pdf(pay_line_id: uuid.UUID, request: Request, token: str | None = Query(None),
                       db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Response:
    """Same PDF for the contractor, reached with the existing HMAC receipt-link token (404 otherwise)."""
    if not verify_receipt_token(settings, pay_line_id, token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found.")
    line = db.get(PayLine, pay_line_id)
    run = db.scalar(select(PayrollRun).options(
        selectinload(PayrollRun.pay_lines), selectinload(PayrollRun.approvals), selectinload(PayrollRun.employer))
        .where(PayrollRun.id == line.run_id)) if line else None
    if line is None or run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found.")
    return _line_pdf(db, settings, request, run, line, None, "receipt_link")


@router.get("/verify/{token}")
def verify(token: str, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    """Public: re-computes the HMAC from the stored record. Always 200; ``valid`` says which."""
    return proof_svc.verify_token(db, settings, token)
