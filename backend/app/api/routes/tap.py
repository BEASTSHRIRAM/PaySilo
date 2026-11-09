"""Visa TAP: public key publication and stored-approval verification.

``GET /.well-known/tap-keys`` is public by design — it publishes only
public keys (active + retired-but-verifiable), the way a TAP agent
registry lookup would. ``POST /tap/verify`` requires a session and is
tenant-scoped: an approval from another organization is a 404, never a
verification result.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import Approval, PayrollRun
from ...partners.tap_signing import compare_signed_body_to_run
from ...services.runtime import PlatformRuntime
from ..deps import Tenant, current_tenant, get_db, get_runtime
from ..schemas import TapKeysOut, TapVerifyIn, TapVerifyOut

router = APIRouter(tags=["tap"])


@router.get("/.well-known/tap-keys", response_model=TapKeysOut)
def tap_keys(db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime)) -> TapKeysOut:
    return TapKeysOut.model_validate(runtime.tap_keys_payload(db))


@router.post("/tap/verify", response_model=TapVerifyOut)
def tap_verify(body: TapVerifyIn, tenant: Tenant = Depends(current_tenant),
               db: Session = Depends(get_db),
               runtime: PlatformRuntime = Depends(get_runtime)) -> TapVerifyOut:
    result = runtime.verify_stored_approval(db, tenant.organization.id,
                                             approval_id=body.approval_id, run_id=body.run_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No signed approval found for that id in your organization.")
    if result.get("verified"):
        # A valid signature is not enough: what was signed must still be what
        # the run holds (assessment totals and every pay line).
        mismatch = _run_mismatch(db, tenant.organization.id, uuid.UUID(result["approval_id"]))
        if mismatch:
            result = {**result, "verified": False, "code": "run_mismatch",
                      "reason": f"Signature is valid but the signed body no longer matches the run: {mismatch}."}
    return TapVerifyOut.model_validate(result)


def _run_mismatch(db: Session, organization_id: uuid.UUID, approval_id: uuid.UUID) -> str | None:
    approval = db.scalar(select(Approval).join(PayrollRun, Approval.run_id == PayrollRun.id)
                         .where(Approval.id == approval_id, PayrollRun.organization_id == organization_id))
    if approval is None or not approval.signed_body:
        return None
    run = db.get(PayrollRun, approval.run_id)
    lines = [{"name": l.beneficiary_name, "amount": l.amount, "currency": l.currency, "method": l.method}
             for l in run.pay_lines]
    return compare_signed_body_to_run(approval.signed_body, run_id=str(run.id),
                                      assessment=run.assessment, pay_lines=lines)
