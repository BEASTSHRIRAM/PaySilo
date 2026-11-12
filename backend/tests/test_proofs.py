"""Proof-of-payment PDFs, public verification and the 'where is my money' trace."""

from __future__ import annotations

import io
import re
import uuid

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.db.models import Organization, PayLine, PayrollRun, ProofAccess
from app.services.payment_trace import payment_trace
from tests.test_bedata import ACME, _hold_borealis, _pay_line_id, _settle_acme

LENA_IBAN = "DE89370400440532013000"


def _flat(pdf: bytes) -> str:
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    return " ".join(text.split())


def _verify_token_from_pdf(pdf: bytes) -> str:
    m = re.search(r"/verify/([lr]\.[0-9a-f-]{36}\.[0-9a-f]{64})", _flat(pdf).replace(" ", ""))
    assert m, "verify URL not found in PDF text"
    return m.group(1)


def test_proof_pdf_for_paid_line(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    r = demo_client.get(f"/runs/{done['id']}/lines/{line_id}/proof.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"]
    flat = _flat(r.content)
    for needle in ("Proof of payment", "Vertex Payroll Services", ACME, "Lena Fischer", "EUR 3400.00",
                   "SWIFT", "****3000", "12.85", "trf_", "Airwallex reports PAID", "not available to PaySilo",
                   "0-2 business days", "What to tell the contractor", "HMAC-SHA256", "VERIFIED"):
        assert needle in flat, needle
    assert "NOT PAID" not in flat
    # masking: no full IBAN or account number anywhere
    assert LENA_IBAN not in flat.replace(" ", "") and LENA_IBAN.encode() not in r.content
    assert "DE8937040044" not in flat.replace(" ", "")


def test_verify_valid_and_tampered_invalid(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    pdf = demo_client.get(f"/runs/{done['id']}/lines/{line_id}/proof.pdf").content
    token = _verify_token_from_pdf(pdf)
    demo_client.post("/auth/logout")  # public: no session needed
    ok = demo_client.get(f"/verify/{token}").json()
    assert ok["valid"] is True and ok["kind"] == "line"
    f = ok["facts"]
    assert f["line"]["contractor"] == "Lena Fischer" and f["line"]["bank_masked"] == "****3000"
    assert f["state"] == "PAID" and LENA_IBAN not in str(ok)
    bad = token[:-1] + ("0" if token[-1] != "0" else "1")
    bad_resp = demo_client.get(f"/verify/{bad}").json()
    assert bad_resp["valid"] is False and "facts" not in bad_resp
    other = token.replace(token.split(".")[1], str(uuid.uuid4()))
    assert demo_client.get(f"/verify/{other}").json()["valid"] is False
    assert demo_client.get("/verify/garbage").json()["valid"] is False


def test_record_change_after_issue_invalidates(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    token = _verify_token_from_pdf(demo_client.get(f"/runs/{done['id']}/lines/{line_id}/proof.pdf").content)
    with demo_client.app.state.db.sessionmaker() as s:
        line = s.get(PayLine, uuid.UUID(line_id))
        line.amount = line.amount + 1
        s.commit()
    assert demo_client.get(f"/verify/{token}").json()["valid"] is False


def test_statement_pdf_and_verify(demo_client):
    done = _settle_acme(demo_client)
    r = demo_client.get(f"/runs/{done['id']}/statement.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    flat = _flat(r.content)
    for needle in ("Payment statement", "Lena Fischer", "Tomas Rivera", "EUR 2600.00", "****3000", "****1332",
                   "2 of 2 lines PAID"):
        assert needle in flat, needle
    nospace = flat.replace(" ", "")
    assert "DE89370400440532013000" not in nospace and "ES9121000418450200051332" not in nospace
    v = demo_client.get(f"/verify/{_verify_token_from_pdf(r.content)}").json()
    assert v["valid"] and v["kind"] == "statement" and len(v["facts"]["lines"]) == 2


def test_held_line_is_a_notice_never_a_proof(demo_client):
    run = _hold_borealis(demo_client)
    line_id = _pay_line_id(demo_client, run["id"], 0)
    r = demo_client.get(f"/runs/{run['id']}/lines/{line_id}/proof.pdf")
    assert r.status_code == 200
    flat = _flat(r.content)
    assert "Payment status notice" in flat and "NOT PAID" in flat
    assert "Proof of payment" not in flat and "reports this payout" not in flat
    assert "No money has been sent to you" in flat
    assert "payment-status-notice" in r.headers["content-disposition"]
    v = demo_client.get(f"/verify/{_verify_token_from_pdf(r.content)}").json()
    assert v["valid"] and v["facts"]["state"] == "HELD" and v["facts"]["document"] == "notice"


def test_failed_line_notice_uses_failure_code(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    with demo_client.app.state.db.sessionmaker() as s:
        line = s.get(PayLine, uuid.UUID(line_id))
        line.status = "FAILED"
        s.get(PayrollRun, line.run_id).error = "Airwallex HTTP 400 code=invalid_argument failure_reason=90301"
        s.commit()
    r = demo_client.get(f"/runs/{done['id']}/lines/{line_id}/proof.pdf")
    flat = _flat(r.content)
    assert "NOT PAID" in flat and "90301" in flat and "name did not match" in flat
    assert "Proof of payment" not in flat
    tr = demo_client.get(f"/runs/{done['id']}/lines/{line_id}/trace").json()
    assert tr["state"] == "FAILED" and tr["failure_code"] == "90301"


def test_trace_endpoint_paid_and_held(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    tr = demo_client.get(f"/runs/{done['id']}/lines/{line_id}/trace").json()
    assert tr["state"] == "PAID" and tr["paid"] is True and tr["transfer_id"].startswith("trf_")
    assert "business days" in tr["expected_window"] and "not available to PaySilo" in tr["diagnosis"]
    run = _hold_borealis(demo_client)
    held = demo_client.get(f"/runs/{run['id']}/lines/{_pay_line_id(demo_client, run['id'], 0)}/trace").json()
    assert held["state"] == "HELD" and held["paid"] is False and "wallet" in held["diagnosis"]


class _L:
    def __init__(self, status="PENDING", transfer=None, method="SWIFT"):
        self.status, self.airwallex_transfer_id, self.method = status, transfer, method


class _R:
    def __init__(self, status="RESERVED", error=None):
        self.status, self.error = status, error


@pytest.mark.parametrize("line,run,kw,state", [
    (_L(), _R(), {}, "SCHEDULED"),
    (_L(transfer="trf_1"), _R("APPROVED"), {}, "SENT"),
    (_L("PAID", "trf_1"), _R("SETTLED"), {}, "PAID"),
    (_L("FAILED"), _R("FAILED"), {}, "FAILED"),
    (_L("HELD"), _R("HELD_BY_HUMAN"), {}, "HELD"),
    (_L("PAID", "trf_1"), _R("SETTLED"), {"transfer_status": "RETURNED"}, "RETURNED"),
    (_L("FAILED"), _R("FAILED"), {"failure_code": "90802"}, "RETURNED"),
])
def test_payment_trace_states(line, run, kw, state):
    t = payment_trace(line, run, **kw)
    assert t["state"] == state and t["headline"] and t["diagnosis"] and t["next_step"]
    assert t["paid"] is (state == "PAID")


@pytest.mark.parametrize("code,phrase", [("90301", "name"), ("90302", "currency"), ("90401", "invalid"),
                                          ("90802", "returned")])
def test_payment_trace_failure_codes(code, phrase):
    t = payment_trace(_L("FAILED"), _R("FAILED", f"failure_reason={code}"))
    if code == "90802":
        assert t["state"] == "RETURNED"
    assert t["failure_code"] == code and phrase in t["diagnosis"].lower()


def test_tenant_isolation_and_auth(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    url = f"/runs/{done['id']}/lines/{line_id}"
    demo_client.post("/auth/logout")
    assert demo_client.get(f"{url}/proof.pdf").status_code == 401
    demo_client.post("/auth/signup", json={
        "organization": {"name": "Third Org", "country": "GB", "base_currency": "USD"},
        "admin": {"full_name": "Tess Third", "email": "tess@third.io", "password": "Third2026x"}})
    for path in (f"{url}/proof.pdf", f"{url}/trace", f"/runs/{done['id']}/statement.pdf"):
        assert demo_client.get(path).status_code == 404, path
    assert demo_client.get(f"/runs/{uuid.uuid4()}/lines/{uuid.uuid4()}/proof.pdf").status_code == 404


def test_line_must_belong_to_run(demo_client):
    done = _settle_acme(demo_client)
    borealis = _hold_borealis(demo_client)
    foreign_line = _pay_line_id(demo_client, borealis["id"], 0)
    assert demo_client.get(f"/runs/{done['id']}/lines/{foreign_line}/proof.pdf").status_code == 404


def test_audit_row_written_per_generation(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    demo_client.get(f"/runs/{done['id']}/lines/{line_id}/proof.pdf")
    demo_client.get(f"/runs/{done['id']}/statement.pdf")
    with demo_client.app.state.db.sessionmaker() as s:
        rows = s.scalars(select(ProofAccess).order_by(ProofAccess.id)).all()
    assert [r.kind for r in rows] == ["proof", "statement"]
    assert all(r.user_id and len(r.document_digest) == 64 for r in rows)


def test_public_receipt_pdf_needs_receipt_token(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    token = demo_client.get(f"/runs/{done['id']}/pay-lines/{line_id}/receipt-token").json()["token"]
    demo_client.post("/auth/logout")
    ok = demo_client.get(f"/receipts/{line_id}/proof.pdf?token={token}")
    assert ok.status_code == 200 and ok.content.startswith(b"%PDF") and "Proof of payment" in _flat(ok.content)
    assert demo_client.get(f"/receipts/{line_id}/proof.pdf?token={'0' * 32}").status_code == 404
    assert demo_client.get(f"/receipts/{line_id}/proof.pdf").status_code == 404
    with demo_client.app.state.db.sessionmaker() as s:
        row = s.scalars(select(ProofAccess)).one()
    assert row.user_id is None and row.channel == "receipt_link"


def test_chat_tool_registered_and_read_only(demo_client):
    from app.chat import tools as chat_tools
    assert "where_is_my_money" in chat_tools.TOOL_FUNCTIONS
    _settle_acme(demo_client)
    db, runtime = demo_client.app.state.db, demo_client.app.state.runtime
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization).where(Organization.name == "Vertex Payroll Services"))
        ctx = chat_tools.ChatContext(db=s, runtime=runtime, organization_id=org.id, organization_name=org.name,
                                     user_id=uuid.uuid4(), user_email="x@y.z", role="viewer", demo_mode=True)
        res = chat_tools.TOOL_FUNCTIONS["where_is_my_money"](ctx, contractor="Lena", employer=ACME)
    assert "Lena Fischer" in res.text and "PAID" in res.text
