"""Risk gate: BANK_UNVERIFIED and RECENT_BANK_CHANGE_LARGE_PAYMENT hold lines; code decides, nobody can override."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import risk
from app.db.models import Contractor, Notification, PayLine
from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
NEW_IBAN = "NL91ABNA0417164300"


# ------------------------------------------------------------------ pure rules
def _line(**kw):
    base = {"name": "X", "amount": "1000.00", "currency": "EUR", "bank_status": "VERIFIED"}
    base.update(kw)
    return base


def test_line_without_facts_is_not_judged():
    assert risk.evaluate_line({"name": "X", "amount": "99999", "currency": "EUR"}, Decimal("1.1"), NOW) is None


def test_unverified_statuses_are_held():
    for status in ("PENDING_VERIFICATION", "UNKNOWN", "anything-else"):
        assert risk.evaluate_line(_line(bank_status=status), Decimal("1"), NOW) == risk.BANK_UNVERIFIED


def test_verified_without_recent_change_is_payable():
    assert risk.evaluate_line(_line(amount="90000"), Decimal("1.1"), NOW) is None
    old = (NOW - timedelta(days=8)).isoformat()
    assert risk.evaluate_line(_line(amount="90000", bank_changed_at=old, bank_second_verified="no"),
                              Decimal("1.1"), NOW) is None


def test_recent_change_large_vs_history():
    changed = (NOW - timedelta(days=2)).isoformat()
    common = dict(bank_changed_at=changed, bank_second_verified="no", prev_paid="1000.00")
    assert risk.evaluate_line(_line(amount="2000.00", **common), Decimal("1"), NOW) is None  # exactly 2x is fine
    assert risk.evaluate_line(_line(amount="2000.01", **common), Decimal("1"), NOW) == risk.RECENT_BANK_CHANGE_LARGE_PAYMENT


def test_recent_change_no_history_uses_usd_5000():
    changed = (NOW - timedelta(days=1)).isoformat()
    common = dict(bank_changed_at=changed, bank_second_verified="no")
    assert risk.evaluate_line(_line(amount="4600", **common), Decimal("1.10"), NOW) == risk.RECENT_BANK_CHANGE_LARGE_PAYMENT  # USD 5,060
    assert risk.evaluate_line(_line(amount="4545.45", **common), Decimal("1.10"), NOW) is None
    assert risk.evaluate_line(_line(amount="5000.00", currency="USD", **common), Decimal("1"), NOW) is None
    assert risk.evaluate_line(_line(amount="5000.01", currency="USD", **common), Decimal("1"), NOW) == risk.RECENT_BANK_CHANGE_LARGE_PAYMENT
    # unknown rate on a recently changed account: fail closed
    assert risk.evaluate_line(_line(amount="10", **common), None, NOW) == risk.RECENT_BANK_CHANGE_LARGE_PAYMENT


def test_second_person_verification_releases_large_payment():
    changed = (NOW - timedelta(days=2)).isoformat()
    line = _line(amount="9000", bank_changed_at=changed, bank_second_verified="yes", prev_paid="100")
    assert risk.evaluate_line(line, Decimal("1"), NOW) is None


def test_unverified_beats_everything_and_split_keeps_order():
    lines = [_line(name="A"), _line(name="B", bank_status="PENDING_VERIFICATION"), _line(name="C")]
    payable, held = risk.split_lines(lines, lambda: {"EUR": Decimal("1.1")}, NOW)
    assert [l["name"] for l in payable] == ["A", "C"]
    assert held[0]["name"] == "B" and held[0]["hold_reason"] == risk.BANK_UNVERIFIED


# ------------------------------------------------------------------ end to end
def _employers(c):
    return {e["name"]: e for e in c.get("/employers").json()}


def _contractor(c, employer, name):
    emp = _employers(c)[employer]
    detail = c.get(f"/employers/{emp['id']}").json()
    return emp, next(x for x in detail["contractors"] if x["full_name"] == name)


def _login(c, email, password):
    c.post("/auth/logout")
    assert c.post("/auth/login", json={"email": email, "password": password}).status_code == 200


def _second_approver(demo_client, email="second@example.com"):
    inv = demo_client.post("/org/invitations", json={"email": email, "role": "admin"}).json()
    token = inv["invite_path"].split("/")[-1]
    assert demo_client.post(f"/invitations/{token}/accept",
                            json={"full_name": "Second User", "password": "SecondPass2026"}).status_code == 200
    _login(demo_client, DEMO_EMAIL, DEMO_PASSWORD)
    return email, "SecondPass2026"


def _change_bank(c, emp, con, iban=NEW_IBAN):
    r = c.patch(f"/employers/{emp['id']}/contractors/{con['id']}", json={"iban": iban, "swift_code": "ABNANL2AXXX"})
    assert r.status_code == 200, r.text


def _run(c, emp, ids, amount=None):
    lines = [{"contractor_id": i, **({"amount": amount} if amount else {})} for i in ids]
    r = c.post("/runs", json={"employer_id": emp["id"], "lines": lines})
    assert r.status_code == 201, r.text
    return r.json()


def test_unverified_line_is_held_in_the_preview_and_never_paid_even_if_approved(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    _, tomas = _contractor(c, "Acme Manufacturing GmbH", "Tomas Rivera")
    _change_bank(c, emp, lena)  # PENDING_VERIFICATION
    run = _run(c, emp, [lena["id"], tomas["id"]])
    assert run["status"] == "RESERVED"
    # preview before approval: the gate's decision is visible and bound into the approval
    pending = run["pending"]
    assert [l["name"] for l in pending["pay_lines"]] == ["Tomas Rivera"]
    assert pending["held_lines"][0]["name"] == "Lena Fischer" and pending["held_lines"][0]["hold_reason"] == "BANK_UNVERIFIED"
    assert run["assessment"]["risk_held_lines"][0]["hold_reason"] == "BANK_UNVERIFIED"
    held_row = next(l for l in run["pay_lines"] if l["beneficiary_name"] == "Lena Fischer")
    done = c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "PARTIALLY_PAID"
    by_name = {l["beneficiary_name"]: l for l in done["pay_lines"]}
    assert by_name["Tomas Rivera"]["status"] == "PAID"
    assert by_name["Lena Fischer"]["status"] == "HELD" and by_name["Lena Fischer"]["hold_reason"] == "BANK_UNVERIFIED"
    assert "second person" in by_name["Lena Fischer"]["hold_message"]
    transfers = [x for x in c.app.state.runtime.recorder.log if x.operation == "create_transfer"]
    assert len(transfers) == 1  # only Tomas
    assert held_row["amount"] == by_name["Lena Fischer"]["amount"]


def test_all_lines_unverified_refuses_and_pays_nobody(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    _change_bank(c, emp, lena)
    run = _run(c, emp, [lena["id"]])
    assert run["status"] == "REFUSED" and "second person" in run["error"]
    assert run["pay_lines"][0]["status"] == "HELD" and run["pay_lines"][0]["hold_reason"] == "BANK_UNVERIFIED"
    assert not [x for x in c.app.state.runtime.recorder.log if x.operation == "create_transfer"]


def test_pay_held_continuation_after_verification(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    _, tomas = _contractor(c, "Acme Manufacturing GmbH", "Tomas Rivera")
    _change_bank(c, emp, lena)
    run = _run(c, emp, [lena["id"], tomas["id"]])
    done = c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    # still unverified: the continuation is refused with a plain explanation
    blocked = c.post(f"/runs/{done['id']}/pay-held")
    assert blocked.status_code == 409 and "second person" in blocked.json()["detail"]
    # a second person verifies after the callback
    email, pw = _second_approver(c)
    _login(c, email, pw)
    assert c.post(f"/employers/{emp['id']}/contractors/{lena['id']}/verify-bank",
                  json={"callback_confirmed": True}).status_code == 200
    _login(c, DEMO_EMAIL, DEMO_PASSWORD)
    cont = c.post(f"/runs/{done['id']}/pay-held")
    assert cont.status_code == 201, cont.text
    cont = cont.json()
    assert cont["status"] == "RESERVED"
    # verified by a second person: even a recent change does not hold her (callback confirmed)
    assert [l["name"] for l in cont["pending"]["pay_lines"]] == ["Lena Fischer"]
    final = c.post(f"/runs/{cont['id']}/decision", json={"decision": "approve"}).json()
    assert final["status"] == "SETTLED"


def test_recent_change_large_payment_held_until_second_person_verifies(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    db = c.app.state.db
    with db.sessionmaker() as s:  # verified earlier by a (different) user, but only on the same day: simulate
        row = s.get(Contractor, uuid.UUID(lena["id"]))
        row.bank_verification_status = "VERIFIED"
        row.bank_details_changed_at = datetime.now(UTC) - timedelta(days=1)
        row.bank_verified_by_id = None  # verified without a recorded second person (e.g. migrated/legacy)
        s.commit()
    run = _run(c, emp, [lena["id"]], "5000")  # EUR 5,000 is above USD 5,000 and there is no payment history
    assert run["status"] == "REFUSED"
    assert run["pay_lines"][0]["hold_reason"] == "RECENT_BANK_CHANGE_LARGE_PAYMENT"
    with db.sessionmaker() as s:  # now a different user verified it after the change
        from app.db.models import User
        other = s.scalar(select(User).where(User.email == DEMO_EMAIL))
        row = s.get(Contractor, uuid.UUID(lena["id"]))
        row.bank_verified_by_id, row.bank_change_requested_by_id = other.id, None
        row.bank_verified_at = datetime.now(UTC)
        s.commit()
    run2 = _run(c, emp, [lena["id"]], "5000")
    assert run2["status"] == "RESERVED" and not run2.get("assessment", {}).get("risk_held_lines")


def test_recent_change_small_payment_with_history_is_allowed(demo_client):
    c = demo_client
    emp, tomas = _contractor(c, "Acme Manufacturing GmbH", "Tomas Rivera")
    first = _run(c, emp, [tomas["id"]])
    assert c.post(f"/runs/{first['id']}/decision", json={"decision": "approve"}).json()["status"] == "SETTLED"
    with c.app.state.db.sessionmaker() as s:
        row = s.get(Contractor, uuid.UUID(tomas["id"]))
        row.bank_details_changed_at = datetime.now(UTC) - timedelta(days=1)
        row.bank_verified_by_id = None
        s.commit()
    again = _run(c, emp, [tomas["id"]])  # same amount as last time: not above 2x
    assert again["status"] == "RESERVED"


def test_bank_changed_between_preview_and_approval_blocks_approval(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    run = _run(c, emp, [lena["id"]])
    assert run["status"] == "RESERVED"
    _change_bank(c, emp, lena)  # details change after the preview
    r = c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert not [x for x in c.app.state.runtime.recorder.log if x.operation == "create_transfer"]
    # a hold is still possible
    assert c.post(f"/runs/{run['id']}/decision", json={"decision": "hold"}).status_code == 200


def test_client_supplied_risk_facts_cannot_spoof_the_gate(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    _change_bank(c, emp, lena)
    rt = c.app.state.runtime
    line = rt.contractor_line(  # a forged "VERIFIED" fact in the line is overwritten from the database
        next(x for x in rt.db.sessionmaker().__enter__().scalars(select(Contractor)) if str(x.id) == lena["id"]),
        Decimal("100"))
    line["bank_status"] = "VERIFIED"
    out = rt._attach_risk_facts([line], {lena["id"]: {"bank_status": "PENDING_VERIFICATION"}})
    assert out[0]["bank_status"] == "PENDING_VERIFICATION"
    unknown = rt._attach_risk_facts([{**line, "contractor_id": str(uuid.uuid4())}], {})
    assert unknown[0]["bank_status"] == "UNKNOWN"


def test_tenant_isolation_of_risk_facts(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    _change_bank(c, emp, lena)
    c.post("/auth/logout")
    signup = {"organization": {"name": "Other Org", "country": "gb", "base_currency": "usd", "size_band": "11-50",
                              "industry": "Staffing"},
              "admin": {"full_name": "Olive Other", "email": "olive@other.io", "password": "OtherOrg2026"}}
    assert c.post("/auth/signup", json=signup).status_code == 201
    other_emp = c.post("/employers", json={"name": "Other Client", "country": "GB"}).json()
    r = c.post("/runs", json={"employer_id": other_emp["id"], "lines": [{"contractor_id": lena["id"]}]})
    assert r.status_code in (404, 422)  # a foreign contractor id never resolves
    assert c.post(f"/employers/{emp['id']}/contractors/{lena['id']}/verify-bank",
                  json={"callback_confirmed": True}).status_code == 404


def test_seeded_demo_bank_change_is_not_on_a_delta_contractor(demo_client):
    emp, pieter = _contractor(demo_client, "Delta Studios BV", "Pieter Jansen")
    assert pieter["bank_verification_status"] == "VERIFIED"
    _, sam = _contractor(demo_client, "Cobalt Retail LLC", "Sam Lee")
    assert sam["bank_verification_status"] == "PENDING_VERIFICATION"


# ------------------------------------------------------------------ notifications wiring
def _notes(c, **where):
    with c.app.state.db.sessionmaker() as s:
        return [n for n in s.scalars(select(Notification)) if all(getattr(n, k) == v for k, v in where.items())]


def test_bank_change_queues_callback_requests_without_bank_details(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    _change_bank(c, emp, lena)
    rows = _notes(c, kind="BANK_CHANGE_CALLBACK")
    assert {n.recipient for n in rows} == {DEMO_EMAIL, "ana.weber@acme.example"}  # approvers + employer contact
    for n in rows:
        assert "****4300" in n.body and "NL91" not in n.body and "DE89" not in n.body and not n.sensitive
    _change_bank(c, emp, lena, iban="NL02ABNA0123456789")  # a second change is a new request
    assert len(_notes(c, kind="BANK_CHANGE_CALLBACK")) == 4


def test_onboarding_link_email_is_sensitive_and_never_listed(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    r = c.patch(f"/employers/{emp['id']}/contractors/{lena['id']}", json={"email": "lena@example.com"})
    assert r.status_code == 200, r.text
    link = c.post(f"/employers/{emp['id']}/contractors/{lena['id']}/onboarding-link")
    assert link.status_code == 201
    token = link.json()["invite_path"].split("/")[-1]
    rows = _notes(c, kind="ONBOARDING_LINK")
    assert len(rows) == 1 and rows[0].recipient == "lena@example.com" and rows[0].sensitive
    assert token not in rows[0].body and (rows[0].body_html is None)
    assert token in rows[0].secret["text"]  # only in the outgoing payload until delivered
    listing = c.get("/notifications").text
    assert token not in listing and '"sensitive":true' in listing.replace(" ", "")
    assert token not in json.dumps(c.get(f"/notifications?kind=ONBOARDING_LINK").json())


def test_sensitive_secret_is_used_for_delivery_then_erased(demo_client):
    from app.services import notify

    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    c.patch(f"/employers/{emp['id']}/contractors/{lena['id']}", json={"email": "lena@example.com"})
    token = c.post(f"/employers/{emp['id']}/contractors/{lena['id']}/onboarding-link").json()["invite_path"].split("/")[-1]

    class Capture:
        name, configured = "capture", True
        sent: list = []

        def send(self, *, to, subject, text, html=None):
            self.sent.append((to, text, html))

    mailer = Capture()
    assert notify.deliver_pending(c.app.state.db.sessionmaker, mailer) >= 1
    mine = [m for m in mailer.sent if m[0] == "lena@example.com"]
    assert mine and token in mine[0][1] and token in mine[0][2]  # the real link goes out
    row = _notes(c, kind="ONBOARDING_LINK")[0]
    assert row.status == "SENT" and row.secret is None and token not in row.body


def test_org_invitation_email_is_sensitive_and_token_not_listed(demo_client):
    c = demo_client
    inv = c.post("/org/invitations", json={"email": "newbie@example.com", "role": "viewer"}).json()
    token = inv["invite_path"].split("/")[-1]  # returned once to the inviter, as before
    rows = _notes(c, kind="INVITE")
    assert len(rows) == 1 and rows[0].recipient == "newbie@example.com" and rows[0].sensitive
    assert token not in rows[0].body and token in rows[0].secret["text"]
    assert token not in c.get("/notifications").text
    assert token not in c.get("/org/invitations").text  # the invitations list never shows tokens either


def test_viewer_cannot_read_sensitive_bodies(demo_client):
    c = demo_client
    inv = c.post("/org/invitations", json={"email": "viewer@example.com", "role": "viewer"}).json()
    token = inv["invite_path"].split("/")[-1]
    assert c.post(f"/invitations/{token}/accept", json={"full_name": "View Er", "password": "ViewerPass2026"}).status_code == 200
    _login(c, "viewer@example.com", "ViewerPass2026")
    text = c.get("/notifications").text
    assert token not in text
    assert "private link" in text  # the redaction placeholder


def test_run_outcome_notice_queued_at_completion_once_per_recipient(demo_client):
    c = demo_client
    emp, tomas = _contractor(c, "Acme Manufacturing GmbH", "Tomas Rivera")
    run = _run(c, emp, [tomas["id"]])
    assert _notes(c, kind="RUN_SETTLED") == []  # nothing before the run completes
    assert c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()["status"] == "SETTLED"
    rows = _notes(c, kind="RUN_SETTLED")
    assert len(rows) == 1 and rows[0].recipient == DEMO_EMAIL and f"/dashboard/runs/{run['id']}" in rows[0].body
    from app.services import autopilot
    with c.app.state.db.sessionmaker() as s:  # the sweep shares the dedupe key: still one
        org = s.scalar(select(__import__("app.db.models", fromlist=["Organization"]).Organization))
        autopilot.report_outcomes(s, c.app.state.settings, org)
        s.commit()
    assert len(_notes(c, kind="RUN_SETTLED")) == 1


def test_failed_run_queues_payment_failed_once(demo_client):
    c = demo_client
    emp, tomas = _contractor(c, "Acme Manufacturing GmbH", "Tomas Rivera")
    run = _run(c, emp, [tomas["id"]])
    rt = c.app.state.runtime
    from app.services import autopilot
    with c.app.state.db.sessionmaker() as s:
        from app.db.models import Organization, PayrollRun
        pr = s.get(PayrollRun, uuid.UUID(run["id"]))
        pr.status, pr.error = "FAILED", "boom"
        org = s.get(Organization, pr.organization_id)
        assert autopilot.report_run_outcome(s, c.app.state.settings, org, pr) == 1
        assert autopilot.report_run_outcome(s, c.app.state.settings, org, pr) == 0
        s.commit()
    assert len(_notes(c, kind="PAYMENT_FAILED")) == 1
    assert rt is not None


def test_pay_line_table_records_reason_only_for_held(demo_client):
    c = demo_client
    emp, lena = _contractor(c, "Acme Manufacturing GmbH", "Lena Fischer")
    _, tomas = _contractor(c, "Acme Manufacturing GmbH", "Tomas Rivera")
    _change_bank(c, emp, lena)
    run = _run(c, emp, [lena["id"], tomas["id"]])
    c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"})
    with c.app.state.db.sessionmaker() as s:
        lines = s.scalars(select(PayLine).where(PayLine.run_id == uuid.UUID(run["id"]))).all()
    assert {(l.beneficiary_name, l.status, l.hold_reason) for l in lines} == {
        ("Tomas Rivera", "PAID", None), ("Lena Fischer", "HELD", "BANK_UNVERIFIED")}


def test_anonymous_client_unaffected(client):
    assert isinstance(client, TestClient)
