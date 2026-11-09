"""Visa TAP: public key publication and stored-approval verification.

``GET /.well-known/tap-keys`` is public by design — it publishes only
public keys (active + retired-but-verifiable), the way a TAP agent
registry lookup would. ``POST /tap/verify`` requires a session and is
tenant-scoped: an approval from another organization is a 404, never a
verification result.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

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
    return TapVerifyOut.model_validate(result)
