"""Automation when funds arrive: auto-continuation (never approved by code), approver emails, reminders."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Notification, Organization, PayrollRun
from app.services import autopilot, reminders


@pytest.fixture
def auto(demo_client):
    demo_client.app.state.settings.auto_continue_held = True
    return demo_client


def _delta_partial(client):
    emp = {e["name"]: e for e in client.get("/employers").json()}["Delta Studios BV"]
    detail = client.get(f"/employers/{emp['id']}").json()
    run = client.post("/runs", json={"employer_id": emp["id"],
                                     "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    done = client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "PARTIALLY_PAID", done
    return emp, done


def _fund(client, emp, amount):
    r = client.post(f"/employers/{emp['id']}/fund", json={"amount": amount, "reference": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


def _runs(client, emp):
    return client.get(f"/runs?employer_id={emp['id']}").json()


def _notes(db, kind):
    with db.sessionmaker() as s:
        return s.scalars(select(Notification).where(Notification.kind == kind)).all()


def test_funding_creates_one_continuation_awaiting_approval_and_emails_approvers(auto):
    emp, parent = _delta_partial(auto)
    db = auto.app.state.db
    transfers_before = {c.request_id for c in auto.app.state.runtime.recorder.log if c.operation == "create_transfer"}
    _fund(auto, emp, "4000")
    conts = [r for r in _runs(auto, emp) if r.get("parent_run_id") == parent["id"]]
    assert len(conts) == 1 and conts[0]["status"] == "RESERVED"  # waits for a human; nothing approved
    assert {c.request_id for c in auto.app.state.runtime.recorder.log if c.operation == "create_transfer"} == transfers_before
    assert auto.get(f"/runs/{parent['id']}").json()["continuation_run_id"] == conts[0]["id"]
    asks = _notes(db, "APPROVAL_REQUESTED")
    assert len(asks) == 1 and asks[0].recipient == "demo@paysilo.test" and asks[0].status == "QUEUED"
    assert f"/dashboard/runs/{conts[0]['id']}" in asks[0].body and "Nothing is paid until" in asks[0].body
    assert asks[0].body_html and "<a href=" in asks[0].body_html
    assert len(_notes(db, "FUNDS_RECEIVED")) == 1
    # more funding or a sweep never creates a second continuation for the same parent
    _fund(auto, emp, "500")
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        reminders.sweep_organization(s, auto.app.state.runtime, auto.app.state.settings, org)
    assert len([r for r in _runs(auto, emp) if r.get("parent_run_id") == parent["id"]]) == 1
    assert len(_notes(db, "APPROVAL_REQUESTED")) == 1
    # only a person can approve it, and that still works
    done = auto.post(f"/runs/{conts[0]['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED"


def test_not_enough_funds_creates_nothing(auto):
    emp, parent = _delta_partial(auto)
    _fund(auto, emp, "100")
    assert [r for r in _runs(auto, emp) if r.get("parent_run_id")] == []
    assert _notes(auto.app.state.db, "APPROVAL_REQUESTED") == []


def test_switch_off_and_later_payroll_guard(auto):
    emp, parent = _delta_partial(auto)
    auto.app.state.settings.auto_continue_held = False
    _fund(auto, emp, "4000")
    assert [r for r in _runs(auto, emp) if r.get("parent_run_id")] == []
    auto.app.state.settings.auto_continue_held = True
    # a later payroll already paid the held contractor -> the runtime's guard blocks the automation
    detail = auto.get(f"/employers/{emp['id']}").json()
    pieter = next(c for c in detail["contractors"] if c["full_name"] == "Pieter Jansen")
    later = auto.post("/runs", json={"employer_id": emp["id"], "lines": [{"contractor_id": pieter["id"]}]}).json()
    assert auto.post(f"/runs/{later['id']}/decision", json={"decision": "approve"}).json()["status"] == "SETTLED"
    db, rt, st = auto.app.state.db, auto.app.state.runtime, auto.app.state.settings
    with db.sessionmaker() as s:
        from app.db.models import Employer
        org = s.scalar(select(Organization))
        res = autopilot.auto_continue(s, rt, st, org, s.get(Employer, uuid.UUID(emp["id"])))
        assert res["action"] == "skipped" and "twice" in res["reason"]


def test_held_lines_reminder_after_24h_once_per_day(auto):
    emp, parent = _delta_partial(auto)
    db, rt, st = auto.app.state.db, auto.app.state.runtime, auto.app.state.settings
    from app.db.models import Employer
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        e = s.get(Employer, uuid.UUID(emp["id"]))
        assert autopilot.remind_held_lines(s, rt, st, org, e) == 0  # too fresh
        later = datetime.now(UTC) + timedelta(hours=25)
        assert autopilot.remind_held_lines(s, rt, st, org, e, now=later) == 1
        assert autopilot.remind_held_lines(s, rt, st, org, e, now=later) == 0  # deduped for the day
        assert autopilot.remind_held_lines(s, rt, st, org, e, now=later + timedelta(days=1)) == 1
        s.commit()
    assert {n.kind for n in _notes(db, "HELD_LINES_REMINDER")} == {"HELD_LINES_REMINDER"}


def test_outcome_notices_once_per_run(auto):
    emp, parent = _delta_partial(auto)
    emp2 = {e["name"]: e for e in auto.get("/employers").json()}["Acme Manufacturing GmbH"]
    detail = auto.get(f"/employers/{emp2['id']}").json()
    run = auto.post("/runs", json={"employer_id": emp2["id"],
                                   "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    assert auto.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()["status"] == "SETTLED"
    db, st = auto.app.state.db, auto.app.state.settings
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        assert autopilot.report_outcomes(s, st, org) >= 1
        assert autopilot.report_outcomes(s, st, org) == 0
        s.commit()
    settled = _notes(db, "RUN_SETTLED")
    assert len(settled) == 1 and settled[0].recipient == "demo@paysilo.test"


def test_tenant_scoping_of_approvers(auto):
    db = auto.app.state.db
    with db.sessionmaker() as s:
        from app.db.models import Membership, User
        other = Organization(name="Other", slug="other", country="US")
        s.add(other)
        s.flush()
        u = User(email="x@other.example", full_name="X", password_hash="x")
        s.add(u)
        s.flush()
        s.add(Membership(organization_id=other.id, user_id=u.id, role="owner"))
        s.commit()
        demo = s.scalar(select(Organization).where(Organization.slug == "vertex-payroll"))
        assert [x.email for x in autopilot.approvers(s, demo)] == ["demo@paysilo.test"]
        assert [x.email for x in autopilot.approvers(s, other)] == ["x@other.example"]
