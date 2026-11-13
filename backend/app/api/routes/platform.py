"""Health, the agent crew, the organization overview, and the scripted Kit 6 story."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ...agents.demo import run_demo
from ...agents.graph import AGENTS
from ...config import Settings
from ...db.models import Employer, LedgerTransaction, PayrollRun
from ...services.runtime import MONEY_MOVED, PlatformRuntime
from ..deps import Tenant, current_tenant, get_db, get_runtime, get_settings
from ..schemas import OrganizationOut, OverviewOut
from ..serializers import employer_out

router = APIRouter(tags=["platform"])


@router.get("/health")
def health(settings: Settings = Depends(get_settings), runtime: PlatformRuntime = Depends(get_runtime)) -> dict:
    db_ok = True
    try:
        runtime.db.ping()
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "mode": "demo" if settings.demo_mode else "live",
        "database": runtime.db.engine.dialect.name if db_ok else "unreachable",
        "partners": {
            "airwallex": "simulator" if settings.demo_mode else "sandbox",
            "claude": "configured" if settings.anthropic_api_key else "deterministic-fallback",
            "visa_tap": f"signing ({runtime.signer.key.key_id})",
            "metal": "mock-chain" if not (settings.metal_rpc_url and settings.metal_private_key
                                          and settings.metal_registry_address) else "configured",
            "bedrock_agentcore": (f"plan cross check ({runtime.services.cloud_planner.region})"
                                  if runtime.services.cloud_planner is not None else "off"),
        },
    }


@router.get("/agents")
def agents() -> list[dict]:
    """The crew, in graph order. Only the Intake Agent uses an LLM."""
    return [{"id": key, **meta} for key, meta in AGENTS.items()]


@router.get("/overview", response_model=OverviewOut)
def overview(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
             runtime: PlatformRuntime = Depends(get_runtime), settings: Settings = Depends(get_settings)) -> OverviewOut:
    org_id = tenant.organization.id
    employers = db.scalars(select(Employer).options(selectinload(Employer.contractors))
                           .where(Employer.organization_id == org_id).order_by(Employer.created_at)).all()

    def count(*conditions) -> int:
        return db.scalar(select(func.count()).select_from(PayrollRun)
                         .where(PayrollRun.organization_id == org_id, *conditions)) or 0

    return OverviewOut(
        organization=OrganizationOut.model_validate(tenant.organization),
        employers=[employer_out(db, runtime, e) for e in employers],
        platform_fees=runtime.platform_fees(db, org_id),
        runs_total=count(),
        runs_settled=count(PayrollRun.status.in_(MONEY_MOVED)),  # money moved: incl. partial / post-payout follow-ups
        runs_waiting=count(PayrollRun.pending_interrupt.is_not(None)),
        mode="demo" if settings.demo_mode else "live",
    )


def _platform_journals(db: Session, tenant: Tenant) -> list[dict]:
    """Platform book: every transaction in the caller's organization that
    touches the ``platform`` namespace (fee revenue). Full transaction
    lines are returned so the balanced flag is the computed truth."""
    from .bedata import journals_for

    txns = db.scalars(
        select(LedgerTransaction).options(selectinload(LedgerTransaction.entries))
        .where(LedgerTransaction.organization_id == tenant.organization.id)
        .order_by(LedgerTransaction.id)).all()
    platform_txns = [t for t in txns if any(e.namespace == "platform" for e in t.entries)]
    return journals_for(db, platform_txns)


@router.get("/platform/journals")
def platform_journals(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[dict]:
    """Platform book: every transaction in the caller's organization that
    touches the ``platform`` namespace (fee revenue)."""
    return _platform_journals(db, tenant)


@router.get("/platform/journals/export")
def platform_journals_export(format: str = Query("json", pattern="^(csv|json)$"),
                             tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> Response:
    from .bedata import journals_download

    return journals_download(_platform_journals(db, tenant), format, f"platform-journals-{tenant.organization.id}")


@router.post("/demo/kit6")
def kit6_story(_: Tenant = Depends(current_tenant)) -> dict:
    """The scripted Starter Kit 6 scenario on fresh, isolated services (nothing persisted)."""
    return run_demo()
