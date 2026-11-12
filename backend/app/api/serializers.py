"""ORM -> API model conversion. Bank details are masked on the way out."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import BenefitBudget, BenefitPurchase, BenefitRequest, Contractor, Employer, PayrollRun, SettlementReceipt
from ..money import money_str
from ..services.benefits import budget_lock
from ..services.runtime import PlatformRuntime
from .schemas import (
    ApprovalOut,
    BenefitBudgetOut,
    BenefitPurchaseOut,
    BenefitRequestOut,
    ContractorOut,
    EmployerDetailOut,
    EmployerOut,
    EventOut,
    PayLineOut,
    ReceiptOut,
    RunDetailOut,
    RunOut,
)


def _last4(value: str | None) -> str | None:
    return value[-4:] if value else None


def contractor_out(c: Contractor) -> ContractorOut:
    return ContractorOut(
        id=c.id, full_name=c.full_name, email=c.email, country=c.country, currency=c.currency,
        payout_method=c.payout_method, iban_last4=_last4(c.iban), account_last4=_last4(c.account_number),
        swift_code=c.swift_code, default_amount=money_str(c.default_amount) if c.default_amount else None,
        priority=c.priority, is_active=c.is_active,
    )


def employer_out(s: Session, runtime: PlatformRuntime, e: Employer) -> EmployerOut:
    return EmployerOut(
        id=e.id, name=e.name, legal_name=e.legal_name, country=e.country, contact_name=e.contact_name,
        contact_email=e.contact_email, funding_currency=e.funding_currency,
        settlement_mode=e.settlement_mode, status=e.status,
        airwallex_account_id=e.airwallex_account_id, balances=runtime.balances(s, e),
        contractor_count=sum(1 for c in e.contractors if c.is_active), created_at=e.created_at,
    )


def employer_detail_out(s: Session, runtime: PlatformRuntime, e: Employer) -> EmployerDetailOut:
    base = employer_out(s, runtime, e)
    return EmployerDetailOut(**base.model_dump(), contractors=[contractor_out(c) for c in e.contractors])


def _residual_shortfall(r: PayrollRun) -> str | None:
    if r.residual_shortfall_usd is not None:
        return money_str(r.residual_shortfall_usd)
    value = (r.assessment or {}).get("residual_shortfall_usd")
    return str(value) if value is not None else None


def run_out(runtime: PlatformRuntime, r: PayrollRun) -> RunOut:
    return RunOut(
        id=r.id, employer_id=r.employer_id, employer_name=r.employer.name, status=r.status, source=r.source,
        period_label=r.period_label, payroll_currency=r.payroll_currency,
        required_usd=money_str(r.required_usd) if r.required_usd is not None else None,
        shortfall_usd=money_str(r.shortfall_usd) if r.shortfall_usd is not None else None,
        settlement_mode=r.employer.settlement_mode if r.employer is not None else None,
        residual_shortfall=_residual_shortfall(r) if r.status in (
            "PARTIALLY_SETTLED", "PARTIALLY_PAID", "HELD_SHORTFALL", "SETTLED") else None,
        residual_plan=r.residual_plan,
        fee_charged_usd=money_str(r.fee_charged_usd), explanation=r.explanation, error=r.error,
        pending=r.pending_interrupt, resumable=runtime.is_resumable(r), created_at=r.created_at,
        settled_at=r.settled_at,
    )


def _explorer_url(runtime: PlatformRuntime, tx_hash: str | None) -> str | None:
    base = runtime.settings.metal_explorer_url
    if not tx_hash or not base:
        return None
    return f"{base.rstrip('/')}/tx/{tx_hash}"


def benefit_budget_out(s: Session, b: BenefitBudget, employer: Employer) -> BenefitBudgetOut:
    locked, lock_reason = budget_lock(s, employer.id)
    remaining = b.monthly_cap - b.spent_this_period
    return BenefitBudgetOut(
        id=b.id, employer_id=b.employer_id, employer_name=employer.name, currency=b.currency,
        monthly_cap=money_str(b.monthly_cap),
        per_tx_limit=money_str(b.per_tx_limit) if b.per_tx_limit is not None else None,
        spent_this_period=money_str(b.spent_this_period), remaining=money_str(remaining),
        period=b.period, enabled=b.enabled, locked=locked, lock_reason=lock_reason,
    )


def benefit_purchase_out(p: BenefitPurchase) -> BenefitPurchaseOut:
    return BenefitPurchaseOut(
        id=p.id, request_id=p.request_id, employer_id=p.employer_id, attempt_no=p.attempt_no,
        mode=p.mode, credential_ref=p.credential_ref, provider_ref=p.provider_ref, status=p.status,
        failure_code=p.failure_code, failure_detail=p.failure_detail, amount=money_str(p.amount),
        currency=p.currency, journal_txn_id=p.journal_txn_id, created_at=p.created_at,
    )


def benefit_request_out(r: BenefitRequest, employer_name: str,
                        latest_purchase: BenefitPurchase | None = None) -> BenefitRequestOut:
    return BenefitRequestOut(
        id=r.id, employer_id=r.employer_id, employer_name=employer_name,
        contractor_id=str(r.contractor_id) if r.contractor_id else None,
        contractor_name=r.contractor_name, merchant=r.merchant, item=r.item,
        amount=money_str(r.amount), currency=r.currency, status=r.status,
        requested_by_email=r.requested_by_email, decided_by_email=r.decided_by_email,
        decided_at=r.decided_at.isoformat() if r.decided_at else None,
        deny_reason=r.deny_reason,
        approval_expires_at=r.approval_expires_at.isoformat() if r.approval_expires_at else None,
        approval_hash=r.approval_hash, created_at=r.created_at,
        latest_purchase=benefit_purchase_out(latest_purchase) if latest_purchase else None,
    )


def run_detail_out(s: Session, runtime: PlatformRuntime, r: PayrollRun) -> RunDetailOut:
    receipt = s.scalar(select(SettlementReceipt).where(SettlementReceipt.run_id == r.id))
    return RunDetailOut(
        **run_out(runtime, r).model_dump(),
        assessment=r.assessment,
        pay_lines=[PayLineOut(id=str(p.id), position=p.position, beneficiary_name=p.beneficiary_name, amount=money_str(p.amount),
                              currency=p.currency, method=p.method,
                              payout_fee=money_str(p.payout_fee) if p.payout_fee is not None else None,
                              status=p.status,
                              settlement_state=("PAID" if p.status == "PAID" else
                                                "HELD" if p.status == "HELD" else "PENDING"),
                              settled_phase=p.settled_phase, settled_at=p.settled_at,
                              airwallex_transfer_id=p.airwallex_transfer_id)
                   for p in r.pay_lines],
        events=[EventOut(seq=e.seq, agent=e.agent, level=e.level, message=e.message, created_at=e.created_at)
                for e in r.events],
        approvals=[ApprovalOut(kind=a.kind, decision=a.decision, decided_by_email=a.decided_by_email,
                               verified=a.verified, tap_key_id=a.tap_key_id, tap_signature=a.tap_signature,
                               created_at=a.created_at) for a in r.approvals],
        receipt=ReceiptOut(chain=receipt.chain, chain_index=receipt.chain_index, receipt_hash=receipt.receipt_hash,
                           payload_hash=receipt.payload_hash, tx_hash=receipt.tx_hash,
                           explorer_url=_explorer_url(runtime, receipt.tx_hash)) if receipt else None,
    )


# ---------------------------------------------------------------- workflows
from ..db.models import Workflow, WorkflowRun, WorkflowRunStep, WorkflowTask  # noqa: E402
from .schemas import WorkflowOut, WorkflowRunOut, WorkflowRunStepOut, WorkflowTaskOut  # noqa: E402


def workflow_out(w: Workflow) -> WorkflowOut:
    return WorkflowOut(
        id=w.id, organization_id=w.organization_id, name=w.name, description=w.description,
        enabled=w.enabled, trigger_type=w.trigger_type, graph=w.graph,
        created_by_id=w.created_by_id, created_at=w.created_at, updated_at=w.updated_at)


def workflow_step_out(st: WorkflowRunStep) -> WorkflowRunStepOut:
    return WorkflowRunStepOut(
        id=st.id, run_id=st.run_id, node_id=st.node_id, node_type=st.node_type, status=st.status,
        detail=st.detail, output=st.output if isinstance(st.output, dict) else None,
        created_at=st.created_at, started_at=st.started_at or st.created_at,
        finished_at=st.finished_at)


def _entity_label(s: Session | None, r: WorkflowRun) -> str | None:
    """Human label for a run's trigger entity (FE waiting-banner deep link)."""
    facts = (r.context or {}).get("facts") or {} if isinstance(r.context, dict) else {}
    if s is not None:
        try:
            from ..db.models import BenefitRequest as _BR, Contractor as _C, Employer as _E, PayrollRun as _PR
            import uuid as _uuid

            try:
                eid = _uuid.UUID(str(r.entity_id))
            except (ValueError, TypeError):
                eid = None
            if eid is not None:
                kind = (r.entity_kind or "").lower()
                if kind == "employer":
                    obj = s.get(_E, eid)
                    if obj is not None:
                        return obj.name
                elif kind == "contractor":
                    obj = s.get(_C, eid)
                    if obj is not None:
                        return obj.full_name
                elif kind in ("payroll_run", "run"):
                    obj = s.get(_PR, eid)
                    if obj is not None:
                        emp = s.get(_E, obj.employer_id)
                        return f"Payroll run — {emp.name if emp else obj.employer_id} ({obj.status})"
                elif kind in ("benefit_request", "benefit"):
                    obj = s.get(_BR, eid)
                    if obj is not None:
                        return f"{obj.item} — {obj.contractor_name}"
                else:
                    import importlib as _il

                    models = _il.import_module("app.db.models")
                    model = getattr(models, "OnboardingKit", None) if "kit" in kind else \
                        getattr(models, "TripRequest", None) if "trip" in kind else None
                    if model is not None:
                        obj = s.get(model, eid)
                        if obj is not None:
                            return (getattr(obj, "title", None) or getattr(obj, "name", None)
                                    or f"{r.entity_kind} {r.entity_id}")
        except Exception:
            pass
    for key in ("employer_name", "contractor_name"):
        if facts.get(key):
            return str(facts[key])
    return f"{r.entity_kind} {r.entity_id}"


def workflow_run_out(r: WorkflowRun, s: Session | None = None, *, include_steps: bool = False) -> WorkflowRunOut:
    return WorkflowRunOut(
        id=r.id, workflow_id=r.workflow_id, organization_id=r.organization_id,
        trigger_event=r.trigger_event, entity_kind=r.entity_kind, entity_id=r.entity_id,
        entity_label=_entity_label(s, r),
        dedupe_key=r.dedupe_key, status=r.status,
        context=r.context if isinstance(r.context, dict) else None,
        error_detail=r.error_detail, started_at=r.started_at, finished_at=r.finished_at,
        created_at=r.created_at,
        steps=[workflow_step_out(st) for st in r.steps] if include_steps else [])


def workflow_task_out(t: WorkflowTask) -> WorkflowTaskOut:
    return WorkflowTaskOut(
        id=t.id, organization_id=t.organization_id, kind=t.kind, title=t.title, detail=t.detail,
        employer_id=t.employer_id, entity_kind=t.entity_kind, entity_id=t.entity_id,
        status=t.status, created_by_run_id=t.created_by_run_id, created_at=t.created_at)
