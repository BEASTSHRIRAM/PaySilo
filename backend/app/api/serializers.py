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
        is_active=c.is_active,
    )


def employer_out(s: Session, runtime: PlatformRuntime, e: Employer) -> EmployerOut:
    return EmployerOut(
        id=e.id, name=e.name, legal_name=e.legal_name, country=e.country, contact_name=e.contact_name,
        contact_email=e.contact_email, funding_currency=e.funding_currency, status=e.status,
        airwallex_account_id=e.airwallex_account_id, balances=runtime.balances(s, e),
        contractor_count=sum(1 for c in e.contractors if c.is_active), created_at=e.created_at,
    )


def employer_detail_out(s: Session, runtime: PlatformRuntime, e: Employer) -> EmployerDetailOut:
    base = employer_out(s, runtime, e)
    return EmployerDetailOut(**base.model_dump(), contractors=[contractor_out(c) for c in e.contractors])


def run_out(runtime: PlatformRuntime, r: PayrollRun) -> RunOut:
    return RunOut(
        id=r.id, employer_id=r.employer_id, employer_name=r.employer.name, status=r.status, source=r.source,
        parent_run_id=r.parent_run_id, period_label=r.period_label, payroll_currency=r.payroll_currency,
        required_usd=money_str(r.required_usd) if r.required_usd is not None else None,
        shortfall_usd=money_str(r.shortfall_usd) if r.shortfall_usd is not None else None,
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
    continuations = runtime.continuations_of(s, r)
    return RunDetailOut(
        continuation_run_id=continuations[0].id if continuations else None,
        held_line_count=len(runtime.held_pay_lines(r)),
        can_pay_held=runtime.pay_held_blocker(s, r) is None,
        run_fee_collected=runtime.run_fee_collected(s, r),
        **run_out(runtime, r).model_dump(),
        assessment=r.assessment,
        pay_lines=[PayLineOut(id=str(p.id), position=p.position, beneficiary_name=p.beneficiary_name, amount=money_str(p.amount),
                              currency=p.currency, method=p.method,
                              payout_fee=money_str(p.payout_fee) if p.payout_fee is not None else None,
                              status=p.status, airwallex_transfer_id=p.airwallex_transfer_id)
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
