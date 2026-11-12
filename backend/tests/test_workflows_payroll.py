"""Workflows x payroll: prepare, held-payroll rescue (idempotent with autopilot), gates and hard limits.

A workflow can prepare and notify; it can never approve, sign, pay, purchase, verify a bank change or bypass
the risk gate. These tests drive the real demo stack (runtime, autopilot, notifications outbox).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import (
    Employer,
    Notification,
    Organization,
    PayrollRun,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
)
from app.services import workflows
from app.services.workflows import (
    ACTION_TYPES,
    FORBIDDEN_ACTION_WORDS,
    dispatch_event,
    validate_graph,
)

APP = Path(__file__).resolve().parents[1] / "app"


def _node(nid, ntype, data=None, x=0):
    return {"id": nid, "type": ntype, "position": {"x": x, "y": 120}, "data": data or {}}


def _graph(*steps):
    nodes = [_node(n, t, d, x=40 + i * 220) for i, (n, t, d) in enumerate(steps)]
    edges = [{"id": f"e{i}", "source": steps[i][0], "target": steps[i + 1][0]} for i in range(len(steps) - 1)]
    return {"nodes": nodes, "edges": edges}


def _emp(client, name):
    return {e["name"]: e for e in client.get("/employers").json()}[name]


def _delta_partial(client):
    emp = _emp(client, "Delta Studios BV")
    detail = client.get(f"/employers/{emp['id']}").json()
    run = client.post("/runs", json={"employer_id": emp["id"],
                                     "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    done = client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "PARTIALLY_PAID", done
    return emp, done


def _fund(client, emp, amount):
    r = client.post(f"/employers/{emp['id']}/fund", json={"amount": amount, "reference": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


def _continuations(client, emp, parent_id):
    return [r for r in client.get(f"/runs?employer_id={emp['id']}").json() if r.get("parent_run_id") == parent_id]


def _db_rows(client, model, **filters):
    with client.app.state.db.sessionmaker() as s:
        q = select(model)
        for column, value in filters.items():
            q = q.where(getattr(model, column) == value)
        rows = s.scalars(q).all()
        for row in rows:
            s.expunge(row)
        return rows


def _rescue_runs(client):
    wf = next(w for w in client.get("/workflows").json() if w["name"] == "Held Payroll Rescue")
    return [r for r in _db_rows(client, WorkflowRun, workflow_id=uuid.UUID(wf["id"]))]


def _steps(client, run):
    return {s.node_type: s for s in _db_rows(client, WorkflowRunStep, run_id=run.id)}


# --------------------------------------------------------------- hard limits
def test_action_registry_has_no_money_or_approval_capability():
    assert set(ACTION_TYPES) == {
        "action.draft_onboarding_kit", "action.create_approval_request", "action.send_email",
        "action.create_exception", "action.post_chat_notice", "action.create_funding_reminder",
        "action.prepare_payroll_run", "action.revalidate_held_run"}
    for action in ACTION_TYPES:
        assert not (set(re.split(r"[._\-]", action.lower())) & FORBIDDEN_ACTION_WORDS), action
    for bad in ("approve_run", "approve_kit", "pay_held", "pay_run", "purchase_item", "buy", "sign_run",
                "verify_bank_change", "bypass_risk_gate", "execute_payout", "transfer_funds", "override_hold"):
        errors = validate_graph(_graph(("t", "trigger.employer_added", {}), ("a", f"action.{bad}", {})))
        assert any("forbidden" in e for e in errors), (bad, errors)
    # the one gate type is a wait, not an action
    assert validate_graph(_graph(("t", "trigger.employer_added", {}),
                                 ("g", "gate.wait_for_approval", {"entity": "trigger"}))) == []


def test_no_workflow_endpoint_can_decide_anything(demo_client):
    paths = [r.path for r in demo_client.app.routes if getattr(r, "path", "").startswith("/workflows")]
    assert paths and not [p for p in paths if re.search(r"approv|purchase|pay|sign|verif|decision", p)], paths
    # and the only HTTP verbs on read-only collections are GET
    brief = [r for r in demo_client.app.routes if getattr(r, "path", "") == "/workflows/brief"]
    assert brief and all(r.methods == {"GET"} for r in brief)


def test_gates_resume_only_from_the_real_decision_endpoints():
    callers = {}
    for path in list(APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in ("resolve_approval_for_entity(", "resolve_gate("):
            if name in text:
                callers.setdefault(path.relative_to(APP).as_posix(), set()).add(name)
    # definition + the three decision routes that go through spend_hooks, nothing else
    assert set(callers) == {"services/workflows.py", "api/routes/spend_hooks.py", "api/routes/runs.py",
                            "api/routes/onboarding.py", "api/routes/travel.py"}, callers
    runs_py = (APP / "api/routes/runs.py").read_text(encoding="utf-8")
    decide_src = runs_py[runs_py.index("def decide("):runs_py.index("def retry(")]
    assert "runtime.decide(" in decide_src and decide_src.index("runtime.decide(") < decide_src.index("resolve_gate(")


def test_rescue_never_approves_or_pays(demo_client):
    emp, parent = _delta_partial(demo_client)
    transfers = {c.request_id for c in demo_client.app.state.runtime.recorder.log if c.operation == "create_transfer"}
    _fund(demo_client, emp, "4000")
    cont = _continuations(demo_client, emp, parent["id"])
    assert len(cont) == 1 and cont[0]["status"] == "RESERVED"  # waiting for a signed human decision
    assert {c.request_id for c in demo_client.app.state.runtime.recorder.log
            if c.operation == "create_transfer"} == transfers
    assert not demo_client.get(f"/runs/{cont[0]['id']}").json()["approvals"]


# ----------------------------------------------------------------- rescue
def test_funds_received_rescue_prepares_one_continuation_and_gate_resumes_on_real_approval(demo_client):
    emp, parent = _delta_partial(demo_client)
    _fund(demo_client, emp, "4000")  # AUTO_CONTINUE_HELD is off: the workflow prepares it
    cont = _continuations(demo_client, emp, parent["id"])
    assert len(cont) == 1
    wf_runs = _rescue_runs(demo_client)
    assert len(wf_runs) == 1 and wf_runs[0].status == "WAITING"  # parked at the gate on the continuation
    steps = _steps(demo_client, wf_runs[0])
    rev = steps["action.revalidate_held_run"]
    assert rev.output["action"] == "created" and rev.output["continuation_run_id"] == cont[0]["id"]
    assert rev.output["now_funded"] is True
    assert wf_runs[0].context["gate_entity_id"] == cont[0]["id"]
    # a second deposit never creates a second continuation: the step links to the existing one
    _fund(demo_client, emp, "100")
    assert len(_continuations(demo_client, emp, parent["id"])) == 1
    second = [r for r in _rescue_runs(demo_client) if r.id != wf_runs[0].id]
    assert len(second) == 1
    assert _steps(demo_client, second[0])["action.revalidate_held_run"].output["action"] == "existing"
    # the REAL decision endpoint is what resumes the parked workflow run
    r = demo_client.post(f"/runs/{cont[0]['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    assert _db_rows(demo_client, WorkflowRun, id=wf_runs[0].id)[0].status == "COMPLETED"


def test_rescue_is_idempotent_with_autopilot(demo_client):
    demo_client.app.state.settings.auto_continue_held = True
    emp, parent = _delta_partial(demo_client)
    _fund(demo_client, emp, "4000")  # autopilot creates it first, then funds.received fires the workflow
    cont = _continuations(demo_client, emp, parent["id"])
    assert len(cont) == 1  # exactly one continuation for the parent, whoever got there first
    wf_runs = _rescue_runs(demo_client)
    assert len(wf_runs) == 1
    rev = _steps(demo_client, wf_runs[0])["action.revalidate_held_run"]
    assert rev.output["action"] == "existing" and rev.output["continuation_run_id"] == cont[0]["id"]
    notes = _db_rows(demo_client, Notification, kind="APPROVAL_REQUESTED")
    assert len(notes) == 1  # one approval mail per approver for the single continuation


def test_rescue_still_short_creates_nothing(demo_client):
    emp, parent = _delta_partial(demo_client)
    _fund(demo_client, emp, "100")
    assert _continuations(demo_client, emp, parent["id"]) == []
    wf_runs = _rescue_runs(demo_client)
    assert len(wf_runs) == 1 and wf_runs[0].status == "COMPLETED"
    rev = _steps(demo_client, wf_runs[0])["action.revalidate_held_run"]
    assert rev.output["now_funded"] is False and rev.output["action"] == "skipped"
    assert rev.output["continuation_run_id"] is None


# ---------------------------------------------------------------- prepare
def test_prepare_payroll_run_goes_through_the_run_start_path(demo_client):
    db = demo_client.app.state.db
    runtime = demo_client.app.state.runtime
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        acme = s.scalar(select(Employer).where(Employer.organization_id == org.id,
                                               Employer.name == "Acme Manufacturing GmbH"))
        wf = Workflow(organization_id=org.id, name="prep test", enabled=True,
                      trigger_type="trigger.employer_added",
                      graph=_graph(("t", "trigger.employer_added", {}), ("p", "action.prepare_payroll_run", {})))
        s.add(wf)
        s.commit()
        first = dispatch_event(s, organization_id=org.id, event_type="employer.created", entity_kind="employer",
                               entity_id=str(acme.id), facts={}, runtime=runtime, only_workflow_id=wf.id)
        s.commit()
        assert first and first[0].status == "COMPLETED"
        payroll = s.scalar(select(PayrollRun).where(PayrollRun.employer_id == acme.id))
        assert payroll.status == "RESERVED" and payroll.source == "workflow"  # awaiting a human decision
        again = dispatch_event(s, organization_id=org.id, event_type="trigger.employer_added",
                               entity_kind="employer", entity_id=str(acme.id), facts={}, runtime=runtime,
                               only_workflow_id=wf.id, dedupe_suffix="second")
        s.commit()
        step = s.scalar(select(WorkflowRunStep).where(WorkflowRunStep.run_id == again[0].id,
                                                      WorkflowRunStep.node_type == "action.prepare_payroll_run"))
        assert step.output["existing"] is True and step.output["run_id"] == str(payroll.id)
        assert len(s.scalars(select(PayrollRun).where(PayrollRun.employer_id == acme.id)).all()) == 1


def test_prepare_without_runtime_fails_the_step_instead_of_guessing(demo_client):
    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        acme = s.scalar(select(Employer).where(Employer.organization_id == org.id))
        wf = Workflow(organization_id=org.id, name="prep no runtime", enabled=True,
                      trigger_type="trigger.employer_added",
                      graph=_graph(("t", "trigger.employer_added", {}), ("p", "action.prepare_payroll_run", {})))
        s.add(wf)
        s.commit()
        runs = dispatch_event(s, organization_id=org.id, event_type="employer.created", entity_kind="employer",
                              entity_id=str(acme.id), facts={}, only_workflow_id=wf.id)
        s.commit()
        assert runs[0].status == "FAILED"
        assert s.scalars(select(PayrollRun)).all() == []


# -------------------------------------------------------- funding reminder
def test_funding_reminder_uses_platform_funding_service(demo_client):
    db = demo_client.app.state.db
    runtime = demo_client.app.state.runtime
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        cobalt = s.scalar(select(Employer).where(Employer.organization_id == org.id,
                                                 Employer.name == "Cobalt Retail LLC"))  # unfunded, has a schedule
        wf = Workflow(organization_id=org.id, name="remind test", enabled=True,
                      trigger_type="trigger.employer_added",
                      graph=_graph(("t", "trigger.employer_added", {}),
                                   ("r", "action.create_funding_reminder", {"note": "Top up"})))
        s.add(wf)
        s.commit()
        runs = dispatch_event(s, organization_id=org.id, event_type="employer.created", entity_kind="employer",
                              entity_id=str(cobalt.id), facts={}, runtime=runtime, only_workflow_id=wf.id)
        s.commit()
        assert runs[0].status == "COMPLETED"
        mails = s.scalars(select(Notification).where(Notification.kind.in_(("FUNDING_REMINDER", "FUNDING_OVERDUE")))).all()
        assert len(mails) == 1 and mails[0].recipient == "cleo@cobalt.example" and mails[0].status == "QUEUED"
        assert mails[0].meta["source"] == "workflow"


# ------------------------------------------------------------ tenant isolation
def _other_org_with_schedule(client):
    db = client.app.state.db
    with db.sessionmaker() as s:
        other = Organization(name="Other Agency", slug="other-agency", country="US", base_currency="USD")
        s.add(other)
        s.flush()
        today = datetime.now(UTC)
        graph = _graph(("t", "trigger.schedule", {"cadence": "weekly", "day": today.weekday(), "fan_out": "org"}),
                       ("x", "action.create_exception", {"title": "other org fired"}))
        wf = Workflow(organization_id=other.id, name="Other schedule", enabled=True,
                      trigger_type="trigger.schedule", graph=graph)
        s.add(wf)
        s.flush()
        run = WorkflowRun(workflow_id=wf.id, organization_id=other.id, trigger_event="trigger.schedule",
                          entity_kind="organization", entity_id=str(other.id), dedupe_key=f"other-{other.id}",
                          status="COMPLETED", context={})
        s.add(run)
        s.commit()
        return other.id, run.id


def test_schedule_tick_only_fires_the_callers_own_workflows(demo_client):
    other_id, _run_id = _other_org_with_schedule(demo_client)
    r = demo_client.post("/workflows/schedules/tick")
    assert r.status_code == 200, r.text
    assert [x for x in _db_rows(demo_client, WorkflowRun, organization_id=other_id)
            if x.trigger_event == "trigger.schedule" and x.dedupe_key != f"other-{other_id}"] == []


def test_foreign_workflow_runs_are_404_and_unlisted(demo_client):
    other_id, other_run = _other_org_with_schedule(demo_client)
    assert demo_client.get(f"/workflows/runs/{other_run}").status_code == 404
    assert all(r["organization_id"] != str(other_id) for r in demo_client.get("/workflows/runs").json())
    other_wf = _db_rows(demo_client, Workflow, organization_id=other_id)[0]
    assert demo_client.get(f"/workflows/{other_wf.id}").status_code == 404
    assert demo_client.put(f"/workflows/{other_wf.id}", json={"enabled": False}).status_code == 404
    assert demo_client.delete(f"/workflows/{other_wf.id}").status_code == 404


def test_events_never_fire_another_organizations_workflows(demo_client):
    other_id, _ = _other_org_with_schedule(demo_client)
    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        other_wf = s.scalar(select(Workflow).where(Workflow.organization_id == other_id))
        other_wf.trigger_type = "trigger.employer_added"
        other_wf.graph = _graph(("t", "trigger.employer_added", {}), ("x", "action.create_exception", {"title": "x"}))
        s.commit()
    emp, _con = (_emp(demo_client, "Acme Manufacturing GmbH"), None)
    before = len(_db_rows(demo_client, WorkflowRun, organization_id=other_id))
    demo_client.post("/employers", json={"name": "Isolation Ltd", "country": "DE"})
    assert emp and len(_db_rows(demo_client, WorkflowRun, organization_id=other_id)) == before


@pytest.mark.parametrize("name", sorted(workflows.TRIGGER_TYPES))
def test_every_trigger_is_known_to_the_event_aliases(name):
    assert workflows.normalize_event(name) == name
