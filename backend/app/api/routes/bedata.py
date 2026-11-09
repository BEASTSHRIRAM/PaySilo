"""Backend data endpoints (guide §8.4 / §9 / §10, v3 bedata lane).

CSV import, ledger entries, funding history, reconciliation, journals,
run evidence bundles, derived exceptions, employer calendar and public
worker receipts. Every number here is computed from real DB state
(ledger entries / pay lines / runs) for the caller's organization only;
cross-tenant ids are 404, exactly like the existing routes. Nothing is
summed across employers: every query is pinned to one employer's ledger
namespace (``Employer.ledger_key``) or one employer's runs.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ...config import Settings
from ...db.models import (
    ChatSecurityEvent,
    Contractor,
    Employer,
    LedgerEntry,
    LedgerTransaction,
    PayLine,
    PayrollRun,
    SettlementReceipt,
    SourceImport,
    SourceImportRow,
)
from ...money import money_str, round_money
from ...services.runtime import PlatformRuntime
from ..deps import EDITOR_ROLES, Tenant, current_tenant, get_db, get_runtime, get_settings, require_roles
from ..schemas import RunDetailOut
from ..serializers import run_detail_out
from .employers import load_employer
from .runs import load_run

router = APIRouter(tags=["bedata"])

SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP"}
MAX_IMPORT_ROWS = 5_000
MAX_IMPORT_BYTES = 2_000_000
_AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
NON_CANCELLED_RUN_STATUSES_EXCLUDED = {"REFUSED", "APPROVAL_REJECTED"}

# Canonical import fields -> accepted CSV header aliases (normalised).
ALIASES: dict[str, tuple[str, ...]] = {
    "beneficiary_name": ("beneficiary_name", "name", "contractor", "payee", "beneficiary", "full_name"),
    "email": ("email", "email_address"),
    "amount": ("amount", "salary", "pay"),
    "currency": ("currency", "ccy"),
    "method": ("method", "payout_method", "payment_method"),
    "iban": ("iban",),
    "account_number": ("account_number", "account", "bank_account"),
    "swift_code": ("swift_code", "swift", "bic"),
    "country": ("country", "country_code"),
}
_HEADER_TO_FIELD: dict[str, str] = {}
for _field, _names in ALIASES.items():
    for _n in _names:
        _HEADER_TO_FIELD[_n] = _field


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def _norm_bank(v: str | None) -> str:
    return re.sub(r"\s+", "", v or "").upper()


def _business_key(data: dict) -> str:
    bank = _norm_bank(data.get("iban") or data.get("account_number") or "")
    return "|".join([
        (data.get("beneficiary_name") or "").strip().casefold(),
        str(data.get("amount") or ""),
        (data.get("currency") or "").upper(),
        bank,
    ])


# ---------------------------------------------------------------------------
# 1. CSV import (C04)
# ---------------------------------------------------------------------------

class ImportPreviewIn(BaseModel):
    filename: str = Field(default="", max_length=255)
    content: str = Field(min_length=1, max_length=2_000_000)
    mapping: dict[str, str] | None = None  # {csv_column: canonical_field}


class ImportCommitIn(BaseModel):
    period_label: str | None = Field(default=None, max_length=60)
    pay_date: str | None = None  # YYYY-MM-DD


def _active_contractors(db: Session, emp: Employer) -> list[Contractor]:
    return list(db.scalars(select(Contractor).where(
        Contractor.employer_id == emp.id, Contractor.organization_id == emp.organization_id,
        Contractor.is_active.is_(True))))


def match_contractor(contractors: list[Contractor], data: dict) -> tuple[Contractor | None, str | None]:
    """Map an import row to an existing active contractor of the employer.

    Bank identifier first (iban / account number, whitespace + case
    insensitive), else exact (case-insensitive) name. Returns
    ``(contractor, None)`` or ``(None, reason)``. Never creates anything.
    """
    bank = _norm_bank(data.get("iban") or data.get("account_number"))
    hit: Contractor | None = None
    if bank:
        hit = next((c for c in contractors if bank in (_norm_bank(c.iban), _norm_bank(c.account_number))), None)
    if hit is None:
        name = (data.get("beneficiary_name") or "").strip().casefold()
        hit = next((c for c in contractors if c.full_name.strip().casefold() == name), None) if name else None
    if hit is None:
        return None, "no matching contractor for this employer; add the contractor first"
    if (data.get("currency") or "").upper() != hit.currency:
        return None, f"currency differs from contractor record ({hit.currency})"
    return hit, None


def _import_out(imp: SourceImport, contractors: list[Contractor] | None = None) -> dict:
    rows = []
    needs_contractor = 0
    for r in imp.rows:
        row = {"row_number": r.row_number, "status": r.status, "errors": list(r.errors or []),
               "data": dict(r.data or {}), "contractor_id": None, "needs_contractor": False}
        if contractors is not None and r.status == "ok":
            c, reason = match_contractor(contractors, row["data"])
            row["contractor_id"] = str(c.id) if c else None
            row["needs_contractor"] = c is None
            row["match_note"] = reason
            needs_contractor += 1 if c is None else 0
        rows.append(row)
    valid = sum(1 for r in imp.rows if r.status == "ok")
    errors = sum(1 for r in imp.rows if r.status == "error")
    dups = sum(1 for r in imp.rows if r.status == "duplicate")
    summary = {
        "row_count": len(rows), "valid_count": valid, "error_count": errors, "duplicate_count": dups,
        "needs_contractor_count": needs_contractor,
        "status": imp.status,
        "text": f"{valid} valid, {errors} with errors, {dups} duplicate(s) out of {len(rows)} row(s)."
                + (f" {needs_contractor} valid row(s) need a contractor record first." if needs_contractor else ""),
    }
    return {
        "import_id": str(imp.id), "content_hash": imp.content_hash, "filename": imp.filename,
        "status": imp.status, "run_id": str(imp.run_id) if imp.run_id else None,
        "row_count": len(rows), "valid_count": valid, "error_count": errors, "duplicate_count": dups,
        "needs_contractor_count": needs_contractor,
        "rows": rows, "summary": summary,
    }


def _existing_business_keys(db: Session, employer_id: uuid.UUID) -> set[str]:
    """Business keys of pay lines already owed by THIS employer.

    Bank identifiers resolve through the line's contractor (pay lines do
    not store bank details themselves); lines without a contractor fall
    back to name+amount+currency with an empty bank component, which can
    only match an import row that also has no bank identifier — and such
    rows fail validation first, so no false dedupe is possible there.
    """
    keys: set[str] = set()
    rows = db.execute(
        select(PayLine, PayrollRun, Contractor)
        .join(PayrollRun, PayLine.run_id == PayrollRun.id)
        .outerjoin(Contractor, PayLine.contractor_id == Contractor.id)
        .where(PayrollRun.employer_id == employer_id,
               PayrollRun.status.notin_(NON_CANCELLED_RUN_STATUSES_EXCLUDED))
    ).all()
    for line, _run, contractor in rows:
        bank = ""
        if contractor is not None:
            bank = _norm_bank(contractor.iban or contractor.account_number or "")
        keys.add(_business_key({
            "beneficiary_name": line.beneficiary_name, "amount": money_str(line.amount),
            "currency": line.currency, "iban": bank,
        }))
    # Rows of previously COMMITTED imports for this employer (re-imports of
    # the same payees in a different file/order must be flagged).
    keys.update(k for k in db.scalars(
        select(SourceImportRow.business_key)
        .join(SourceImport, SourceImportRow.import_id == SourceImport.id)
        .where(SourceImport.employer_id == employer_id, SourceImport.status == "COMMITTED",
               SourceImportRow.status == "ok", SourceImportRow.business_key.is_not(None))) if k)
    return keys


@router.post("/employers/{employer_id}/imports/preview")
def preview_import(employer_id: uuid.UUID, body: ImportPreviewIn,
                   tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                   db: Session = Depends(get_db)) -> dict:
    emp = load_employer(db, tenant, employer_id)
    content_hash = hashlib.sha256(body.content.encode("utf-8")).hexdigest()
    if len(body.content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Import file is too large.")
    contractors = _active_contractors(db, emp)
    existing = db.scalar(select(SourceImport).options(selectinload(SourceImport.rows)).where(
        SourceImport.employer_id == emp.id, SourceImport.content_hash == content_hash))
    if existing is not None and (existing.status == "COMMITTED"
                                 or (existing.mapping or None) == (body.mapping or None)):
        return _import_out(existing, contractors)  # same file + same mapping: no new rows, ever

    # Resolve column mapping: explicit {csv_column: field} wins over aliases.
    explicit: dict[str, str] = {}
    if body.mapping:
        for csv_col, field in body.mapping.items():
            if field in ALIASES:
                explicit[_norm_header(csv_col)] = field

    try:
        reader = csv.DictReader(io.StringIO(body.content))
        raw_rows = list(reader)
        headers = reader.fieldnames or []
    except csv.Error as exc:
        raise HTTPException(422, f"Could not parse CSV: {exc}") from None
    if not headers:
        raise HTTPException(422, "CSV has no header row.")
    if len(raw_rows) > MAX_IMPORT_ROWS:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE,
                            f"Import has {len(raw_rows)} rows; the limit is {MAX_IMPORT_ROWS}.")

    col_field: dict[str, str] = {}
    for h in headers:
        nh = _norm_header(h)
        if nh in explicit:
            col_field[h] = explicit[nh]
        elif nh in _HEADER_TO_FIELD:
            col_field[h] = _HEADER_TO_FIELD[nh]

    existing_keys = _existing_business_keys(db, emp.id)
    seen_in_file: set[str] = set()

    if existing is not None:
        # Same file previewed again with a different mapping: re-run it.
        existing.rows.clear()
        existing.mapping = body.mapping
        existing.filename = body.filename or existing.filename
        db.flush()
        imp = existing
    else:
        imp = SourceImport(organization_id=tenant.organization.id, employer_id=emp.id,
                           created_by_id=tenant.user.id, filename=body.filename,
                           content_hash=content_hash, content=body.content, mapping=body.mapping,
                           status="PREVIEWED")
        db.add(imp)
        db.flush()

    for idx, raw in enumerate(raw_rows, start=1):
        data: dict[str, str] = {}
        for col, field in col_field.items():
            val = (raw.get(col) or "").strip()
            if val:
                data[field] = val
        errors: list[str] = []
        # beneficiary_name (required)
        name = (data.get("beneficiary_name") or "").strip()
        if not name:
            errors.append("beneficiary_name is required")
        else:
            data["beneficiary_name"] = name
        # amount (positive decimal, money conventions: 2dp half-up domain)
        amount_str: str | None = None
        raw_amount = (data.get("amount") or "").strip().replace(",", "")
        if not raw_amount:
            errors.append("amount is required")
        else:
            if not _AMOUNT_RE.fullmatch(raw_amount):
                errors.append("amount must be a positive decimal with at most 2 decimal places")
            else:
                amt = Decimal(raw_amount)
                if amt <= 0:
                    errors.append("amount must be a positive decimal")
                else:
                    amount_str = money_str(amt)
                    data["amount"] = amount_str
        # currency
        ccy = (data.get("currency") or "").strip().upper()
        if not ccy:
            errors.append("currency is required")
        elif ccy not in SUPPORTED_CURRENCIES:
            errors.append(f"currency must be one of {sorted(SUPPORTED_CURRENCIES)}")
        else:
            data["currency"] = ccy
        # method
        method = (data.get("method") or "SWIFT").strip().upper()
        if method not in ("SWIFT", "LOCAL"):
            errors.append("method must be SWIFT or LOCAL")
        else:
            data["method"] = method
        # bank identifier
        iban = _norm_bank(data.get("iban")) or None
        acct = (data.get("account_number") or "").strip() or None
        if iban:
            data["iban"] = iban
        if acct:
            data["account_number"] = acct
        if not iban and not acct:
            errors.append("iban or account_number is required")
        if data.get("swift_code"):
            data["swift_code"] = _norm_bank(data["swift_code"])
        if data.get("country"):
            country = data["country"].strip().upper()
            if len(country) != 2 or not country.isalpha():
                errors.append("country must be a 2-letter code")
            else:
                data["country"] = country
        if data.get("email") and "@" not in data["email"]:
            errors.append("email is not valid")

        canonical = {k: data[k] for k in
                     ("beneficiary_name", "email", "amount", "currency", "method", "iban",
                      "account_number", "swift_code", "country") if data.get(k)}
        key = _business_key(canonical) if amount_str and name and ccy in SUPPORTED_CURRENCIES else None
        if errors:
            row_status = "error"
        elif key is not None and (key in existing_keys or key in seen_in_file):
            row_status = "duplicate"
            errors = ["duplicate obligation for this employer (name + amount + currency + bank identifier)"]
        else:
            row_status = "ok"
        if key is not None:
            seen_in_file.add(key)
        db.add(SourceImportRow(import_id=imp.id, row_number=idx, status=row_status,
                               errors=errors, data=canonical, business_key=key))
    db.commit()
    db.expire_all()
    imp = db.scalar(select(SourceImport).options(selectinload(SourceImport.rows))
                    .where(SourceImport.id == imp.id))
    return _import_out(imp, contractors)


@router.post("/employers/{employer_id}/imports/{import_id}/commit", response_model=RunDetailOut)
def commit_import(employer_id: uuid.UUID, import_id: uuid.UUID, body: ImportCommitIn,
                  tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                  db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime)) -> RunDetailOut:
    """Commit a previewed import by starting a normal payroll run.

    Every valid row must map to an existing active contractor of this
    employer (bank identifier, else exact name); the run then goes through
    the agent graph (pricing, TAP approval, payout) exactly like a manual
    run. Nothing is created behind the graph's back.
    """
    emp = load_employer(db, tenant, employer_id)
    imp = db.scalar(select(SourceImport).options(selectinload(SourceImport.rows)).where(
        SourceImport.id == import_id, SourceImport.employer_id == emp.id,
        SourceImport.organization_id == tenant.organization.id))
    if imp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import not found.")
    if imp.status == "COMMITTED":
        if imp.run_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This import is being committed; retry shortly.")
        return run_detail_out(db, runtime, load_run(db, tenant, imp.run_id))  # idempotent replay
    if emp.status != "ACTIVE":
        raise HTTPException(status.HTTP_409_CONFLICT, f"{emp.name} is not active.")

    pay_date_val: date | None = None
    if body.pay_date:
        try:
            pay_date_val = date.fromisoformat(body.pay_date)
        except ValueError:
            raise HTTPException(422, "pay_date must be YYYY-MM-DD.") from None

    ok_rows = [r for r in imp.rows if r.status == "ok"]
    if not ok_rows:
        raise HTTPException(422, "This import has no valid rows to commit.")
    contractors = _active_contractors(db, emp)
    lines: list[dict] = []
    unmatched: list[dict] = []
    seen: set[uuid.UUID] = set()
    for row in ok_rows:
        d = dict(row.data or {})
        c, reason = match_contractor(contractors, d)
        if c is None:
            unmatched.append({"row_number": row.row_number, "beneficiary_name": d.get("beneficiary_name"),
                              "reason": reason})
            continue
        if c.id in seen:
            unmatched.append({"row_number": row.row_number, "beneficiary_name": d.get("beneficiary_name"),
                              "reason": f"{c.full_name} appears twice in this import"})
            continue
        seen.add(c.id)
        lines.append(runtime.contractor_line(c, Decimal(d["amount"])))
    if unmatched:
        raise HTTPException(422, detail={
            "message": "Some rows need a contractor record before this import can become a run.",
            "needs_contractor": unmatched})

    # Atomic PREVIEWED -> COMMITTED claim: a concurrent commit updates zero
    # rows and gets 409 instead of starting a second run. The UPDATE shares
    # the transaction that start_run commits, so a failed run start rolls the
    # claim back too.
    claimed = db.execute(update(SourceImport).where(
        SourceImport.id == imp.id, SourceImport.status == "PREVIEWED").values(status="COMMITTED"))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "This import was already committed.")
    try:
        run = runtime.start_run(db, org=tenant.organization, user=tenant.user, employer=emp,
                                pay_lines=lines, raw_input=None, source="import",
                                period_label=body.period_label or f"Import {imp.filename}".strip()[:60])
    except Exception:
        db.rollback()
        raise
    if pay_date_val is not None:
        run.pay_date = pay_date_val
    db.execute(update(SourceImport).where(SourceImport.id == imp.id).values(run_id=run.id))
    db.commit()
    db.expire_all()
    return run_detail_out(db, runtime, load_run(db, tenant, run.id))


def _load_import(db: Session, tenant: Tenant, employer_id: uuid.UUID, import_id: uuid.UUID) -> SourceImport:
    emp = load_employer(db, tenant, employer_id)
    imp = db.scalar(select(SourceImport).options(selectinload(SourceImport.rows)).where(
        SourceImport.id == import_id, SourceImport.employer_id == emp.id,
        SourceImport.organization_id == tenant.organization.id))
    if imp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import not found.")
    return imp


@router.get("/employers/{employer_id}/imports/{import_id}")
def get_import(employer_id: uuid.UUID, import_id: uuid.UUID,
               tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> dict:
    emp = load_employer(db, tenant, employer_id)
    return _import_out(_load_import(db, tenant, employer_id, import_id), _active_contractors(db, emp))


# ---------------------------------------------------------------------------
# 2/3/5. Ledger entries, funding history, journals (shared helpers)
# ---------------------------------------------------------------------------

def _employer_transactions(db: Session, tenant: Tenant, emp: Employer) -> list[LedgerTransaction]:
    return db.scalars(
        select(LedgerTransaction).options(selectinload(LedgerTransaction.entries))
        .where(LedgerTransaction.organization_id == tenant.organization.id,
               LedgerTransaction.employer_id == emp.id)
        .order_by(LedgerTransaction.id)).all()


def _journal_out(t: LedgerTransaction) -> dict:
    lines, per_ccy = [], defaultdict(lambda: Decimal("0"))
    for e in t.entries:
        per_ccy[e.currency] += e.amount
        lines.append({
            "account": e.account,
            "debit": money_str(e.amount) if e.amount > 0 else "0.00",
            "credit": money_str(-e.amount) if e.amount < 0 else "0.00",
            "currency": e.currency,
        })
    return {
        "id": t.id,
        "date": t.created_at.isoformat() if t.created_at else None,
        "memo": t.memo,
        "employer_id": str(t.employer_id) if t.employer_id else None,
        "run_id": str(t.run_id) if t.run_id else None,
        "lines": lines,
        "balanced": all(v == 0 for v in per_ccy.values()),
    }


def journals_for(db: Session, txns: list[LedgerTransaction]) -> list[dict]:
    return [_journal_out(t) for t in txns]


@router.get("/employers/{employer_id}/ledger-entries")
def ledger_entries(employer_id: uuid.UUID, limit: int = Query(100, ge=1, le=500),
                   offset: int = Query(0, ge=0), currency: str | None = None,
                   tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[dict]:
    emp = load_employer(db, tenant, employer_id)
    q = (select(LedgerEntry, LedgerTransaction)
         .join(LedgerTransaction, LedgerEntry.transaction_id == LedgerTransaction.id)
         .where(LedgerTransaction.organization_id == tenant.organization.id,
                LedgerTransaction.employer_id == emp.id,
                LedgerEntry.namespace == emp.ledger_key)
         .order_by(LedgerEntry.id))
    if currency:
        q = q.where(LedgerEntry.currency == currency.upper())
    rows = db.execute(q.offset(offset).limit(limit)).all()
    return [{
        "id": e.id, "transaction_id": e.transaction_id, "account": e.account,
        "amount": money_str(e.amount), "currency": e.currency, "memo": t.memo,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    } for e, t in rows]


@router.get("/employers/{employer_id}/funding-history")
def funding_history(employer_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
                    db: Session = Depends(get_db)) -> list[dict]:
    """Deposits only, derived from the ledger: a transaction counts as
    funding when value enters ``<key>:cash:<CCY>`` from ``external:bank``
    (the exact posting made by ``PlatformRuntime.fund`` and by graph
    intake funding). No such transaction -> no row. Nothing invented."""
    emp = load_employer(db, tenant, employer_id)
    out = []
    for t in _employer_transactions(db, tenant, emp):
        cash = [e for e in t.entries if e.account.startswith(f"{emp.ledger_key}:cash:")]
        external_bank = [e for e in t.entries if e.account.startswith("external:bank:")]
        if not cash or not external_bank:
            continue
        for e in cash:
            if e.amount <= 0:
                continue
            source = "deposit" if t.memo.startswith("deposit") else "funding"
            out.append({
                "id": t.id, "amount": money_str(e.amount), "currency": e.currency,
                "source": source, "status": "confirmed",
                "memo": t.memo,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            })
    out.sort(key=lambda r: r["id"], reverse=True)
    return out


@router.get("/employers/{employer_id}/journals")
def employer_journals(employer_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
                      db: Session = Depends(get_db)) -> list[dict]:
    emp = load_employer(db, tenant, employer_id)
    return journals_for(db, _employer_transactions(db, tenant, emp))


def journals_download(journals: list[dict], fmt: str, fname_stem: str) -> Response:
    fname = f"{fname_stem}.{fmt}"
    if fmt == "json":
        return Response(content=json.dumps(journals, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["transaction_id", "date", "memo", "employer_id", "account", "debit", "credit", "currency", "balanced"])
    for j in journals:
        for line in j["lines"]:
            w.writerow([j["id"], j["date"], j["memo"], j["employer_id"], line["account"],
                        line["debit"], line["credit"], line["currency"], j["balanced"]])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/employers/{employer_id}/journals/export")
def employer_journals_export(employer_id: uuid.UUID, format: str = Query("json", pattern="^(csv|json)$"),
                             tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> Response:
    emp = load_employer(db, tenant, employer_id)
    return journals_download(journals_for(db, _employer_transactions(db, tenant, emp)), format,
                             f"journals-{emp.id}")


# ---------------------------------------------------------------------------
# 4. Reconciliation (guide §10.3)
# ---------------------------------------------------------------------------

@router.get("/employers/{employer_id}/reconciliation")
def reconciliation(employer_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
                   db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime)) -> dict:
    """Cash identity per currency, classified from this employer's ledger:

        closing = opening(0) + deposits + refunds + fx_buys - fx_sells
                  - payout_debits - provider_fees - platform_charges + other

    Classification uses the graph's real postings:
      deposits        cash in from ``external:bank`` (runtime.fund / intake)
      fx_buys/sells   cash legs of ``<run> FX USD->CCY`` transactions
                      (counterpart ``<key>:fx_clearing:*``)
      payout_debits   ``external:payees:*`` legs of ``<run> payout …``
      provider_fees   ``external:payout_fees:*`` legs of the same
      platform_charges ``platform:fees:*`` legs of ``<run> platform run fee``
      refunds         cash in from ``external:payees/payout_fees`` (none are
                      posted by the current graph, so honestly 0 unless real
                      refund postings exist)
      other           any remaining cash movement, reported — never hidden
    ``variance`` = ledger closing cash minus the employer's wallet balance
    as reported by the provider (``wallet_balance``). If the wallet cannot be
    read (no connected account, provider error) ``wallet_available`` is false,
    ``variance`` falls back to the internal identity check and the response
    is labelled ``basis: "ledger_self_check"`` so it never claims external
    assurance. ``identity_variance`` is always the internal check.
    """
    emp = load_employer(db, tenant, employer_id)
    key = emp.ledger_key
    txns = _employer_transactions(db, tenant, emp)

    cats = ("deposits", "refunds", "fx_buys", "fx_sells", "payout_debits",
            "provider_fees", "platform_charges", "other", "closing_cash")
    per: dict[str, dict[str, Decimal]] = defaultdict(lambda: {c: Decimal("0") for c in cats})
    currencies: set[str] = set()

    for t in txns:
        accounts = {e.account.rsplit(":", 1)[0] for e in t.entries}
        is_fx = any(a == f"{key}:fx_clearing" for a in accounts)
        has_bank = any(a == "external:bank" for a in accounts)
        for e in t.entries:
            if e.namespace != key:
                continue
            currencies.add(e.currency)
            acc_name = e.account.split(":")[1] if e.account.count(":") >= 2 else ""
            if acc_name != "cash":
                continue
            per[e.currency]["closing_cash"] += e.amount
            if has_bank and e.amount > 0:
                per[e.currency]["deposits"] += e.amount
            elif is_fx:
                if e.amount > 0:
                    per[e.currency]["fx_buys"] += e.amount
                else:
                    per[e.currency]["fx_sells"] += -e.amount
            elif any(x.account.startswith("platform:fees:") for x in t.entries):
                per[e.currency]["platform_charges"] += -e.amount
            elif any(x.account.startswith("external:payees:") or x.account.startswith("external:payout_fees:")
                     for x in t.entries):
                if e.amount < 0:
                    principal = sum(x.amount for x in t.entries if x.account.startswith("external:payees:")
                                    and x.currency == e.currency)
                    fees = sum(x.amount for x in t.entries if x.account.startswith("external:payout_fees:")
                               and x.currency == e.currency)
                    per[e.currency]["payout_debits"] += principal
                    per[e.currency]["provider_fees"] += fees
                else:
                    per[e.currency]["refunds"] += e.amount
            else:
                per[e.currency]["other"] += e.amount

    # Settlements from this employer's pay lines.
    settlements = []
    lines = db.execute(
        select(PayLine, PayrollRun).join(PayrollRun, PayLine.run_id == PayrollRun.id)
        .where(PayrollRun.employer_id == emp.id, PayrollRun.organization_id == tenant.organization.id)
        .order_by(PayLine.id)).all()
    for line, run in lines:
        matched = line.status == "PAID" and bool(line.airwallex_transfer_id)
        settlements.append({
            "run_id": str(run.id), "pay_line_id": str(line.id),
            "beneficiary_name": line.beneficiary_name, "amount": money_str(line.amount),
            "currency": line.currency, "status": line.status, "matched": matched,
            "provider_ref": line.airwallex_transfer_id,
        })
    matched_n = sum(1 for s in settlements if s["matched"])
    matched_percent = round(100.0 * matched_n / len(settlements), 2) if settlements else None
    has_open = any(s["status"] in ("PENDING", "HELD") for s in settlements)

    wallet: dict[str, Decimal] | None = None
    if emp.airwallex_account_id:
        try:
            wallet = {c: round_money(v) for c, v in
                      runtime.recorder.get_balances(account_id=emp.airwallex_account_id).items()}
        except Exception:  # provider unreachable: degrade to a labelled self-check
            wallet = None
    if wallet is not None:
        currencies |= {c for c, v in wallet.items() if v != 0}

    currency_rows = []
    for ccy in sorted(currencies):
        p = per[ccy]
        computed = (p["deposits"] + p["refunds"] + p["fx_buys"] - p["fx_sells"]
                    - p["payout_debits"] - p["provider_fees"] - p["platform_charges"] + p["other"])
        identity_variance = round_money(p["closing_cash"] - computed)
        if wallet is not None:
            wallet_bal = wallet.get(ccy, Decimal("0.00"))
            variance = round_money(p["closing_cash"] - wallet_bal)
            if identity_variance != 0:
                variance = identity_variance if variance == 0 else variance
        else:
            wallet_bal = None
            variance = identity_variance
        if variance != 0:
            rstatus = "variance"
        elif has_open:
            rstatus = "pending_evidence"
        else:
            rstatus = "matched"
        currency_rows.append({
            "currency": ccy, "opening_cash": "0.00",
            "deposits": money_str(p["deposits"]), "refunds": money_str(p["refunds"]),
            "fx_buys": money_str(p["fx_buys"]), "fx_sells": money_str(p["fx_sells"]),
            "payout_debits": money_str(p["payout_debits"]),
            "provider_fees": money_str(p["provider_fees"]),
            "platform_charges": money_str(p["platform_charges"]),
            "other": money_str(p["other"]),
            "computed_closing_cash": money_str(computed),
            "closing_cash": money_str(p["closing_cash"]),
            "wallet_balance": money_str(wallet_bal) if wallet_bal is not None else None,
            "wallet_available": wallet_bal is not None,
            "identity_variance": money_str(identity_variance),
            "variance": money_str(variance), "status": rstatus,
        })

    overall = ("variance" if any(r["status"] == "variance" for r in currency_rows)
               else "pending_evidence" if any(r["status"] == "pending_evidence" for r in currency_rows)
               else "matched")
    return {
        "employer_id": str(emp.id), "employer_name": emp.name,
        "currencies": currency_rows, "status": overall,
        "settlements": settlements, "matched_percent": matched_percent,
        "basis": "ledger_vs_wallet" if wallet is not None else "ledger_self_check",
        "basis_note": ("Ledger cash compared with the employer's wallet balance reported by the provider."
                       if wallet is not None else
                       "Ledger self-check only: the provider wallet could not be read, so this is not "
                       "external assurance."),
    }


# ---------------------------------------------------------------------------
# 8. Calendar (derived scheduling defaults)
# ---------------------------------------------------------------------------

@router.get("/employers/{employer_id}/calendar")
def employer_calendar(employer_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
                      db: Session = Depends(get_db)) -> dict:
    emp = load_employer(db, tenant, employer_id)
    runs = db.scalars(select(PayrollRun).where(
        PayrollRun.employer_id == emp.id, PayrollRun.organization_id == tenant.organization.id,
        PayrollRun.pay_date.is_not(None)).order_by(PayrollRun.pay_date)).all()
    events = []
    note = "Derived scheduling default, not a provider-confirmed date."
    for run in runs:
        pd = run.pay_date
        label_base = run.period_label or "Payroll run"
        for kind, d, suffix in (
            ("period_start", pd.replace(day=1), "period start (derived: first of pay month)"),
            ("funding_due", pd - timedelta(days=3), "funding due (derived: pay_date - 3d)"),
            ("cutoff", pd - timedelta(days=2), "cutoff (derived: pay_date - 2d)"),
            ("pay_date", pd, "pay date"),
        ):
            events.append({
                "date": d.isoformat(), "kind": kind, "run_id": str(run.id),
                "label": f"{label_base} — {suffix}", "derived": True, "note": note,
            })
    events.sort(key=lambda e: (e["date"], e["kind"]))
    return {"events": events}


# ---------------------------------------------------------------------------
# 6. Evidence bundle (guide §10.4)
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}/evidence")
def run_evidence(run_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
                 db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime),
                 settings: Settings = Depends(get_settings)) -> Response:
    run = load_run(db, tenant, run_id)
    emp = run.employer
    receipt = db.scalar(select(SettlementReceipt).where(SettlementReceipt.run_id == run.id))
    explorer = None
    if receipt and receipt.tx_hash and settings.metal_explorer_url:
        explorer = f"{settings.metal_explorer_url.rstrip('/')}/tx/{receipt.tx_hash}"
    journal_ids = db.scalars(select(LedgerTransaction.id).where(
        LedgerTransaction.run_id == run.id,
        LedgerTransaction.organization_id == tenant.organization.id).order_by(LedgerTransaction.id)).all()
    # Ledger transactions whose memo embeds the run id but have no run_id
    # column set (defensive: runtime sets run_id from the memo prefix).
    imports = db.scalars(select(SourceImport).where(SourceImport.run_id == run.id)).all()

    exceptions: list[dict] = []
    if run.status == "HELD_SHORTFALL" or (run.shortfall_usd and run.shortfall_usd > 0):
        exceptions.append({"type": "shortfall", "detail": f"Run held: shortfall USD {money_str(run.shortfall_usd or 0)}."})
    for line in run.pay_lines:
        if line.status == "FAILED":
            exceptions.append({"type": "failed_pay_line", "pay_line_id": str(line.id),
                               "detail": f"Payout to {line.beneficiary_name} failed."})
    if run.error:
        exceptions.append({"type": "run_error", "detail": run.error})

    limitations = [
        "A database audit table is not tamper-proof against database administrators; the Metal anchor is the external commitment.",
        "Bank details are limited to provider references in this bundle; full IBANs are not exported.",
    ]
    if settings.demo_mode:
        limitations.append("Demo mode: Airwallex calls were served by the in-repo simulator, labelled simulator — not live provider evidence.")
    if receipt and receipt.chain == "mock":
        limitations.append("Metal anchor is on the mock settlement chain, not Metal L2 testnet.")

    bundle = {
        "manifest": {"version": "1.0", "mode": "demo" if settings.demo_mode else "live",
                     "provider_mode": "simulator" if settings.demo_mode else "sandbox",
                     "generated_at": datetime.now(UTC).isoformat()},
        "employer": {"id": str(emp.id), "name": emp.name},
        "run": {
            "id": str(run.id), "status": run.status, "source": run.source,
            "period_label": run.period_label,
            "pay_date": run.pay_date.isoformat() if run.pay_date else None,
            "payroll_currency": run.payroll_currency,
            "required_usd": money_str(run.required_usd) if run.required_usd is not None else None,
            "shortfall_usd": money_str(run.shortfall_usd) if run.shortfall_usd is not None else None,
            "fee_charged_usd": money_str(run.fee_charged_usd),
            "assessment": run.assessment, "explanation": run.explanation, "error": run.error,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "settled_at": run.settled_at.isoformat() if run.settled_at else None,
        },
        "source_imports": [{"id": str(i.id), "filename": i.filename, "content_hash": i.content_hash}
                           for i in imports],
        "approvals": [{
            "id": str(a.id), "kind": a.kind, "decision": a.decision,
            "decided_by_email": a.decided_by_email, "verified": a.verified,
            "tap_verified": a.verified, "tap_key_id": a.tap_key_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        } for a in run.approvals],
        "pay_lines": [{
            "id": str(p.id), "position": p.position, "beneficiary_name": p.beneficiary_name,
            "amount": money_str(p.amount), "currency": p.currency, "method": p.method,
            "payout_fee": money_str(p.payout_fee) if p.payout_fee is not None else None,
            "status": p.status, "request_id": p.request_id,
            "provider_ref": p.airwallex_transfer_id, "airwallex_transfer_id": p.airwallex_transfer_id,
        } for p in run.pay_lines],
        "agent_events": [{
            "seq": e.seq, "agent": e.agent, "level": e.level, "message": e.message,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        } for e in run.events],
        "tap": {
            "total_approvals": len(run.approvals),
            "verified_count": sum(1 for a in run.approvals if a.verified),
            "all_verified": bool(run.approvals) and all(a.verified for a in run.approvals),
            "any_verified": any(a.verified for a in run.approvals),
        },
        "metal": {
            "receipt": ({
                "chain": receipt.chain, "chain_index": receipt.chain_index,
                "receipt_hash": receipt.receipt_hash, "payload_hash": receipt.payload_hash,
                "prev_hash": receipt.prev_hash, "tx_hash": receipt.tx_hash,
            } if receipt else None),
            "tx_hash": receipt.tx_hash if receipt else None,
            "explorer_url": explorer,
        },
        "journal_transaction_ids": list(journal_ids),
        "exceptions": exceptions,
        "limitations": limitations,
    }
    return Response(content=json.dumps(bundle, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="evidence-{run.id}.json"'})


# ---------------------------------------------------------------------------
# 7. Exceptions (derived, org-scoped, no new table)
# ---------------------------------------------------------------------------

@router.get("/exceptions")
def list_exceptions(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                    tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[dict]:
    org_id = tenant.organization.id
    employers = {e.id: e for e in db.scalars(select(Employer).where(Employer.organization_id == org_id))}
    out: list[dict] = []

    runs = db.scalars(select(PayrollRun).options(selectinload(PayrollRun.pay_lines))
                      .where(PayrollRun.organization_id == org_id)
                      .order_by(PayrollRun.created_at.desc()).limit(1000)).all()
    for run in runs:
        emp = employers.get(run.employer_id)
        ename = emp.name if emp else None
        owner = (emp.contact_name if emp and emp.contact_name else "Payroll operator")
        created = run.created_at.isoformat() if run.created_at else None
        if run.status in ("HELD_SHORTFALL", "HELD_BY_HUMAN") or (run.shortfall_usd and run.shortfall_usd > 0
                                                                 and run.status not in ("SETTLED",)):
            is_short = run.status == "HELD_SHORTFALL" or bool(run.shortfall_usd and run.shortfall_usd > 0)
            out.append({
                "id": f"run:{run.id}:held", "type": "shortfall" if is_short else "held_run",
                "severity": "high" if is_short else "medium",
                "employer_id": str(run.employer_id), "employer_name": ename, "run_id": str(run.id),
                "title": f"{ename or 'Employer'} payroll held" + (f" — shortfall USD {money_str(run.shortfall_usd)}"
                                                                  if run.shortfall_usd else ""),
                "detail": run.explanation or run.error or f"Run status is {run.status}.",
                "owner": owner,
                "next_action": ("Fund the employer's own wallet, then start a new run."
                                if is_short else "Review the held run and decide."),
                "evidence_label": f"Run {run.id} assessment", "created_at": created,
            })
        if run.status == "FAILED":
            out.append({
                "id": f"run:{run.id}:failed", "type": "failed_run", "severity": "high",
                "employer_id": str(run.employer_id), "employer_name": ename, "run_id": str(run.id),
                "title": f"{ename or 'Employer'} payroll run failed",
                "detail": run.error or "Run failed; see agent events.",
                "owner": owner, "next_action": "Inspect the run timeline; resume is safe (request ids are stable).",
                "evidence_label": f"Run {run.id} evidence bundle", "created_at": created,
            })
        for line in run.pay_lines:
            if line.status == "FAILED":
                out.append({
                    "id": f"payline:{line.id}:failed", "type": "failed_pay_line", "severity": "high",
                    "employer_id": str(run.employer_id), "employer_name": ename, "run_id": str(run.id),
                    "title": f"Payout to {line.beneficiary_name} failed",
                    "detail": f"{line.currency} {money_str(line.amount)} via {line.method} did not complete.",
                    "owner": owner, "next_action": "Resolve the original attempt before any replacement payout.",
                    "evidence_label": f"Pay line {line.id} (request {line.request_id})",
                    "created_at": created,
                })

    imports = db.scalars(select(SourceImport).options(selectinload(SourceImport.rows))
                         .where(SourceImport.organization_id == org_id,
                                SourceImport.status != "COMMITTED")
                         .order_by(SourceImport.created_at.desc()).limit(200)).all()
    for imp in imports:
        errs = sum(1 for r in imp.rows if r.status == "error")
        dups = sum(1 for r in imp.rows if r.status == "duplicate")
        if not (errs or dups):
            continue
        emp = employers.get(imp.employer_id)
        out.append({
            "id": f"import:{imp.id}:uncommitted", "type": "import_errors", "severity": "medium",
            "employer_id": str(imp.employer_id), "employer_name": emp.name if emp else None, "run_id": None,
            "title": f"Uncommitted import '{imp.filename}' has {errs} error(s), {dups} duplicate(s)",
            "detail": "Import was previewed but not committed; error/duplicate rows would be excluded from any commit.",
            "owner": emp.contact_name if emp and emp.contact_name else "Payroll operator",
            "next_action": "Fix the source file or mapping and re-preview, or commit the valid rows.",
            "evidence_label": f"Import {imp.id} (sha256 {imp.content_hash[:12]}…)",
            "created_at": imp.created_at.isoformat() if imp.created_at else None,
        })

    # Security events are operator-sensitive: owner/admin only.
    sec_events = [] if tenant.role not in EDITOR_ROLES else db.scalars(
        select(ChatSecurityEvent).where(ChatSecurityEvent.organization_id == org_id)
        .order_by(ChatSecurityEvent.created_at.desc()).limit(200)).all()
    for ev in sec_events:
        out.append({
            "id": f"chatsec:{ev.id}", "type": "chat_security", "severity": "high",
            "employer_id": None, "employer_name": None, "run_id": None,
            "title": f"Chat security event: {ev.kind}",
            "detail": ev.detail, "owner": "Platform security",
            "next_action": "Review the conversation and the actor's access.",
            "evidence_label": f"Chat security event {ev.id}",
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        })

    out.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return out[offset:offset + limit]


# ---------------------------------------------------------------------------
# 9. Worker receipt (HMAC token + public receipt)
# ---------------------------------------------------------------------------

RECEIPT_TTL = timedelta(days=30)


def _receipt_key(settings: Settings) -> bytes:
    """Dedicated HMAC key for receipt links, derived from the session secret
    with a distinct label so a receipt token can never be confused with (or
    used to forge) any other signature made with the same secret."""
    return hmac.new(settings.session_secret().encode("utf-8"), b"paysilo/receipt-link/v1",
                    hashlib.sha256).digest()


def _receipt_sig(settings: Settings, pay_line_id: uuid.UUID, expires: int) -> str:
    return hmac.new(_receipt_key(settings), f"{pay_line_id}.{expires}".encode(), hashlib.sha256).hexdigest()[:32]


def receipt_token(settings: Settings, pay_line_id: uuid.UUID, *, now: datetime | None = None) -> str:
    """``<expiry-epoch>.<signature>``; the expiry is covered by the signature."""
    expires = int(((now or datetime.now(UTC)) + RECEIPT_TTL).timestamp())
    return f"{expires}.{_receipt_sig(settings, pay_line_id, expires)}"


def verify_receipt_token(settings: Settings, pay_line_id: uuid.UUID, token: str | None,
                         *, now: datetime | None = None) -> bool:
    if not token or "." not in token:
        return False
    exp_s, _, sig = token.partition(".")
    if not exp_s.isdigit():
        return False
    expires = int(exp_s)
    if not hmac.compare_digest(sig, _receipt_sig(settings, pay_line_id, expires)):
        return False
    return expires >= int((now or datetime.now(UTC)).timestamp())


@router.get("/runs/{run_id}/pay-lines/{line_id}/receipt-token")
def get_receipt_token(run_id: uuid.UUID, line_id: uuid.UUID,
                      tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)) -> dict:
    run = load_run(db, tenant, run_id)
    line = next((p for p in run.pay_lines if p.id == line_id), None)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pay line not found.")
    token = receipt_token(settings, line.id)
    return {"token": token, "path": f"/receipt/{line.id}?token={token}",
            "api_path": f"/receipts/{line.id}?token={token}",
            "expires_at": datetime.fromtimestamp(int(token.split(".")[0]), UTC).isoformat()}


@router.get("/receipts/{pay_line_id}")
def public_receipt(pay_line_id: uuid.UUID, token: str | None = Query(None),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    # Wrong, expired or missing token is a 404, never a hint that the line exists.
    if not verify_receipt_token(settings, pay_line_id, token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found.")
    line = db.get(PayLine, pay_line_id)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found.")
    run = db.scalar(select(PayrollRun).options(
        selectinload(PayrollRun.approvals), selectinload(PayrollRun.employer))
        .where(PayrollRun.id == line.run_id))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found.")
    contractor = db.get(Contractor, line.contractor_id) if line.contractor_id else None
    bank_masked = None
    if contractor is not None:
        raw = contractor.iban or contractor.account_number
        if raw:
            bank_masked = f"****{raw[-4:]}"
    receipt = db.scalar(select(SettlementReceipt).where(SettlementReceipt.run_id == run.id))
    explorer = None
    if receipt and receipt.tx_hash and settings.metal_explorer_url:
        explorer = f"{settings.metal_explorer_url.rstrip('/')}/tx/{receipt.tx_hash}"

    def iso(d: datetime | None) -> str | None:
        return d.isoformat() if d else None

    # Only this line's data and generic run milestones. No agent events (they
    # name other beneficiaries, amounts and account ids).
    timeline = [{"label": "Payroll received by your employer's payroll agency", "at": iso(run.created_at)}]
    approved = [a for a in run.approvals if a.verified and a.decision == "approve"]
    if approved:
        timeline.append({"label": "Payroll approved with a verified Visa TAP signature",
                         "at": iso(min(a.created_at for a in approved if a.created_at))})
    if line.status == "PAID":
        timeline.append({"label": f"Your payout of {line.currency} {money_str(line.amount)} was sent",
                         "at": iso(run.settled_at)})
    if run.settled_at:
        timeline.append({"label": "Payroll run settled", "at": iso(run.settled_at)})
    return {
        "beneficiary_name": line.beneficiary_name,
        "amount": money_str(line.amount), "currency": line.currency, "method": line.method,
        "status": line.status, "employer_name": run.employer.name if run.employer else None,
        "timeline": timeline,
        "settled_at": iso(run.settled_at),
        "bank_masked": bank_masked,
        "verification": {
            "tap_verified": any(a.verified for a in run.approvals),
            "metal_tx_hash": receipt.tx_hash if receipt else None,
            "metal_explorer_url": explorer,
            "mode": "simulator" if settings.demo_mode else "live",
        },
    }
