"""Workflow engine tests (deterministic node graphs, human gates, platform wiring).

Engine-level tests run against an in-memory database with the real services; payroll-touching actions are
exercised end to end through the demo app (real runtime, real notifications outbox, real autopilot path).
The invariants pinned here: no approve/purchase/pay capability exists in the action registry, a gate resumes
only from the real human decision endpoints, a parent run never gets two continuations, mail goes through the
platform notifications outbox, and every query is tenant-scoped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.db.models import (
    Contractor,
    Employer,
    Membership,
    Notification,
    OnboardingKit,
    Organization,
    PayLine,
    PayrollRun,
    User,
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowTask,
)
from app.services import workflows
from app.services.workflows import (
    build_payroll_brief,
    dispatch_event,
    resolve_approval_for_entity,
    run_due_schedules,
    validate_graph,
)

# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _node(node_id, node_type, data=None, x=0):
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": 120}, "data": data or {}}


def _edge(source, target, handle=None):
    edge = {"id": f"e-{source}-{target}-{handle or ''}", "source": source, "target": target}
    if handle:
        edge["sourceHandle"] = handle
    return edge


def _graph(*steps):
    """Linear graph from (id, type, data) steps."""
    nodes = [_node(nid, ntype, data, x=40 + i * 220) for i, (nid, ntype, data) in enumerate(steps)]
    edges = [_edge(steps[i][0], steps[i + 1][0]) for i in range(len(steps) - 1)]
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Fixtures / stubs
# ---------------------------------------------------------------------------

@pytest.fixture
def env():
    db = Database.from_url("sqlite://")
    db.create_schema()
    with db.sessionmaker() as s:
        org = Organization(name="Vertex Test", slug="vertex-test", country="IN", base_currency="USD")
        s.add(org)
        s.flush()
        owner = User(email="owner@test.example", full_name="Olivia Owner", password_hash="x")
        s.add(owner)
        s.flush()
        s.add(Membership(organization_id=org.id, user_id=owner.id, role="owner"))
        emp = Employer(organization_id=org.id, name="Acme Manufacturing GmbH", country="DE",
                       contact_name="Ana Weber", contact_email="ana@acme.example", status="ACTIVE")
        s.add(emp)
        s.flush()
        contractor = Contractor(organization_id=org.id, employer_id=emp.id, full_name="Lena Fischer",
                                email="lena@acme.example", country="DE", currency="EUR",
                                payout_method="SWIFT", iban="DE89370400440532013000",
                                swift_code="COBADEFFXXX", default_amount=Decimal("3400"))
        s.add(contractor)
        s.commit()
        yield SimpleNamespace(s=s, org=org, owner=owner, emp=emp, contractor=contractor)


def _make_workflow(env, name, graph, enabled=True):
    wf = Workflow(organization_id=env.org.id, name=name, enabled=enabled,
                  trigger_type=workflows.trigger_type_of(graph) or "", graph=graph,
                  created_by_id=env.owner.id)
    env.s.add(wf)
    env.s.commit()
    return wf


def _runtime():
    """The slice of the runtime the engine needs without payroll: settings (receipt links, base URL)."""
    return SimpleNamespace(settings=Settings(database_url="sqlite://", jwt_secret="test-secret-" + "x" * 32))


def _notes(env, **filters):
    env.s.expire_all()
    q = select(Notification).where(Notification.organization_id == env.org.id)
    for column, value in filters.items():
        q = q.where(getattr(Notification, column) == value)
    return env.s.scalars(q).all()


def _steps(env, run):
    env.s.expire_all()
    return env.s.scalars(select(workflows.WorkflowRunStep)
                         .where(workflows.WorkflowRunStep.run_id == run.id)
                         .order_by(workflows.WorkflowRunStep.created_at)).all()


def _held_run(env, employer, *, status="HELD_SHORTFALL", required="6000.00", shortfall="2642.88",
              assessment=None, lines=(("Ingrid Solberg", "6000.00", "HELD"),)):
    run = PayrollRun(organization_id=env.org.id, employer_id=employer.id, status=status,
                     payroll_currency="EUR", required_usd=Decimal(required),
                     shortfall_usd=Decimal(shortfall) if shortfall is not None else None,
                     assessment=assessment or {"required_usd": required, "available_usd": "4357.12",
                                               "reserve_floor_usd": "1000.00",
                                               "shortfall_usd": shortfall})
    env.s.add(run)
    env.s.flush()
    for i, (name, amount, line_status) in enumerate(lines):
        env.s.add(PayLine(run_id=run.id, position=i, beneficiary_name=name, amount=Decimal(amount),
                          currency="EUR", method="SWIFT", status=line_status,
                          request_id=f"test-{run.id}-{i}"))
    env.s.commit()
    return run


# ---------------------------------------------------------------------------
# Dispatch / dedupe / enabled
# ---------------------------------------------------------------------------

def test_dispatch_fires_only_matching_enabled(env):
    _make_workflow(env, "match", _graph(
        ("t", "trigger.contractor_joined", {}),
        ("x", "action.create_exception", {"title": "hi", "detail": "d"})))
    _make_workflow(env, "other-event", _graph(
        ("t", "trigger.employer_added", {}),
        ("x", "action.create_exception", {"title": "no", "detail": "d"})))
    _make_workflow(env, "disabled", _graph(
        ("t", "trigger.contractor_joined", {}),
        ("x", "action.create_exception", {"title": "no", "detail": "d"})), enabled=False)

    runs = dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                          entity_kind="contractor", entity_id=str(env.contractor.id), facts={})
    env.s.commit()
    assert len(runs) == 1
    assert runs[0].status == "COMPLETED"
    assert runs[0].trigger_event == "trigger.contractor_joined"
    tasks = env.s.scalars(select(WorkflowTask)).all()
    assert [t.title for t in tasks] == ["hi"]


def test_dedupe_second_dispatch_creates_nothing(env):
    _make_workflow(env, "onboarding", _graph(
        ("t", "trigger.contractor_joined", {}),
        ("d", "action.draft_onboarding_kit", {})))
    first = dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                           entity_kind="contractor", entity_id=str(env.contractor.id), facts={})
    env.s.commit()
    second = dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                            entity_kind="contractor", entity_id=str(env.contractor.id), facts={})
    env.s.commit()
    assert len(first) == 1 and second == []
    kits = env.s.scalars(select(OnboardingKit)).all()
    assert len(kits) == 1 and kits[0].source == "WORKFLOW"  # one kit drafted despite two dispatches
    assert len(env.s.scalars(select(WorkflowRun)).all()) == 1
    assert first[0].context["kit_id"] == str(kits[0].id)


# ---------------------------------------------------------------------------
# Gate park / resume / reject
# ---------------------------------------------------------------------------

def _gate_workflow(env):
    return _make_workflow(env, "gated onboarding", _graph(
        ("t", "trigger.contractor_joined", {}),
        ("d", "action.draft_onboarding_kit", {}),
        ("a", "action.create_approval_request", {}),
        ("g", "gate.wait_for_approval", {"entity": "kit"})))


def test_wait_gate_parks_and_resume_completes(env):
    _gate_workflow(env)
    runs = dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                          entity_kind="contractor", entity_id=str(env.contractor.id), facts={})
    env.s.commit()
    run = runs[0]
    assert run.status == "WAITING"
    steps = _steps(env, run)
    waiting = [st for st in steps if st.status == "WAITING"]
    assert len(waiting) == 1 and waiting[0].node_type == "gate.wait_for_approval"
    assert waiting[0].finished_at is None and waiting[0].started_at is not None
    approval = env.s.scalar(select(WorkflowApproval))
    assert approval.status == "PENDING" and approval.entity_kind == "kit"

    resumed = resolve_approval_for_entity(
        env.s, organization_id=env.org.id, entity_kind="kit", entity_id=approval.entity_id,
        decision="approved", decided_by=env.owner)
    env.s.commit()
    assert [r.id for r in resumed] == [run.id]
    env.s.refresh(run)
    assert run.status == "COMPLETED"
    steps = _steps(env, run)
    assert any(st.status == "RESUMED" and st.node_type == "gate.wait_for_approval" for st in steps)
    waiting_after = [st for st in steps if st.status == "WAITING"]
    assert waiting_after[0].finished_at is not None  # parked step concluded on resume
    env.s.refresh(approval)
    assert approval.status == "APPROVED" and approval.decided_by_id == env.owner.id


def test_rejection_completes_without_downstream(env):
    _make_workflow(env, "gated email", _graph(
        ("t", "trigger.contractor_joined", {}),
        ("g", "gate.wait_for_approval", {"entity": "trigger"}),
        ("e", "action.send_email", {"template": "approval_requested", "recipient": "decider"})))
    runs = dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                          entity_kind="contractor", entity_id=str(env.contractor.id), facts={})
    env.s.commit()
    run = runs[0]
    assert run.status == "WAITING"
    resolve_approval_for_entity(env.s, organization_id=env.org.id, entity_kind="contractor",
                                entity_id=str(env.contractor.id), decision="rejected",
                                decided_by=env.owner)
    env.s.commit()
    env.s.refresh(run)
    assert run.status == "COMPLETED"
    assert run.context["gate_outcome"] == "rejected"
    assert _notes(env) == []  # downstream email never queued
    assert not [st for st in _steps(env, run) if st.node_type == "action.send_email"]


# ---------------------------------------------------------------------------
# Conditions (branch recorded in step output — FE canvas contract)
# ---------------------------------------------------------------------------

def test_condition_branches_recorded(env):
    graph = {"nodes": [
        _node("t", "trigger.contractor_joined", {}, x=40),
        _node("c", "condition.field_check", {"field": "amount", "operator": "gt", "value": "100"}, x=260),
        _node("big", "action.create_exception", {"title": "big", "detail": ""}, x=480),
        _node("small", "action.create_exception", {"title": "small", "detail": ""}, x=480),
    ], "edges": [_edge("t", "c"), _edge("c", "big", "true"), _edge("c", "small", "false")]}
    _make_workflow(env, "amount check", graph)

    big = dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                         entity_kind="contractor", entity_id=str(env.contractor.id),
                         facts={"amount": "250"})
    other = Contractor(organization_id=env.org.id, employer_id=env.emp.id, full_name="Tomas Rivera",
                       email=None, country="ES", currency="EUR", payout_method="SWIFT",
                       iban="ES9121000418450200051332", swift_code="CAIXESBBXXX",
                       default_amount=Decimal("50"))
    env.s.add(other)
    env.s.commit()
    small = dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                           entity_kind="contractor", entity_id=str(other.id), facts={"amount": "50"})
    env.s.commit()

    big_cond = [st for st in _steps(env, big[0]) if st.node_type == "condition.field_check"][0]
    assert big_cond.output["branch"] == "true"
    assert big_cond.output["field"] == "amount" and big_cond.output["actual"] == "250"
    small_cond = [st for st in _steps(env, small[0]) if st.node_type == "condition.field_check"][0]
    assert small_cond.output["branch"] == "false"
    titles = sorted(t.title for t in env.s.scalars(select(WorkflowTask)).all())
    assert titles == ["big", "small"]


# ---------------------------------------------------------------------------
# Validation (forbidden actions, trigger count, schedule data)
# ---------------------------------------------------------------------------

def test_validate_graph_forbidden_and_missing_trigger():
    forbidden = _graph(("t", "trigger.contractor_joined", {}), ("a", "action.approve_kit", {}))
    errors = validate_graph(forbidden)
    assert any("forbidden" in e for e in errors), errors
    purchase = _graph(("t", "trigger.contractor_joined", {}), ("a", "action.purchase_item", {}))
    assert any("forbidden" in e for e in validate_graph(purchase))
    no_trigger = _graph(("a", "action.create_exception", {"title": "x"}))
    assert any("exactly one trigger" in e for e in validate_graph(no_trigger))
    two_triggers = {"nodes": [_node("t1", "trigger.employer_added"), _node("t2", "trigger.contractor_joined")],
                    "edges": []}
    assert any("exactly one trigger" in e for e in validate_graph(two_triggers))
    bad_edge = _graph(("t", "trigger.employer_added", {}))
    bad_edge["edges"] = [_edge("t", "ghost")]
    assert any("missing node" in e for e in validate_graph(bad_edge))
    bad_gate = _graph(("t", "trigger.employer_added", {}), ("g", "gate.wait_for_approval", {"entity": "nope"}))
    assert any("resolvable entity" in e or "wait gate" in e for e in validate_graph(bad_gate))


def test_validate_schedule_trigger_data():
    missing = _graph(("t", "trigger.schedule", {}), ("x", "action.create_exception", {"title": "x"}))
    errors = validate_graph(missing)
    assert any("cadence" in e for e in errors) and any("day" in e for e in errors)
    bad_day = _graph(("t", "trigger.schedule", {"cadence": "weekly", "day": 9}),
                     ("x", "action.create_exception", {"title": "x"}))
    assert any("0-6" in e for e in validate_graph(bad_day))
    good = _graph(("t", "trigger.schedule", {"cadence": "monthly", "day": 3, "fan_out": "employers"}),
                  ("x", "action.create_exception", {"title": "x"}))
    assert validate_graph(good) == []
    # A condition branch with NO outgoing edge is valid (terminal branch).
    one_branch = {"nodes": [
        _node("t", "trigger.schedule", {"cadence": "weekly", "day": 4, "fan_out": "org"}),
        _node("c", "condition.field_check", {"field": "shortfall", "operator": "gt", "value": "0"}),
        _node("x", "action.create_exception", {"title": "x"}),
    ], "edges": [_edge("t", "c"), _edge("c", "x", "true")]}
    assert validate_graph(one_branch) == []


# ---------------------------------------------------------------------------
# Email action / chat notice
# ---------------------------------------------------------------------------

def test_send_email_queues_through_platform_outbox(env):
    _make_workflow(env, "email deciders", _graph(
        ("t", "trigger.contractor_joined", {}),
        ("e", "action.send_email", {"template": "approval_requested", "recipient": "decider"})))
    runs = dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                          entity_kind="contractor", entity_id=str(env.contractor.id),
                          facts={}, runtime=_runtime())
    env.s.commit()
    run = runs[0]
    assert run.status == "COMPLETED"
    step = [st for st in _steps(env, run) if st.node_type == "action.send_email"][0]
    assert step.status == "OK" and "Queued approval_requested" in step.detail
    rows = _notes(env, kind="SPEND_APPROVAL_REQUESTED")
    assert len(rows) == 1
    assert rows[0].recipient == "owner@test.example"  # decider = org owner
    assert rows[0].status == "QUEUED" and rows[0].organization_id == env.org.id  # recorded, never fake-sent
    # dispatching the same event again (deduped) or re-running the node cannot queue a second copy
    assert dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                          entity_kind="contractor", entity_id=str(env.contractor.id), facts={}) == []
    assert len(_notes(env, kind="SPEND_APPROVAL_REQUESTED")) == 1


def test_send_email_unknown_template_fails_the_run_and_queues_nothing(env):
    wf = _make_workflow(env, "bad template", _graph(
        ("t", "trigger.contractor_joined", {}),
        ("e", "action.send_email", {"template": "wire_money_now", "recipient": "decider"})))
    wf.graph["nodes"][1]["data"]["template"] = "wire_money_now"  # bypass validation: the engine must still refuse
    env.s.commit()
    runs = dispatch_event(env.s, organization_id=env.org.id, event_type="contractor.created",
                          entity_kind="contractor", entity_id=str(env.contractor.id), facts={})
    env.s.commit()
    assert runs[0].status == "FAILED" and _notes(env) == []
    assert any("unknown email template" in e for e in validate_graph(wf.graph))


def test_post_chat_notice_never_fails_without_thread(env):
    _make_workflow(env, "notice", _graph(
        ("t", "trigger.employer_added", {}),
        ("n", "action.post_chat_notice", {"message": "Hello {employer_name}"})))
    runs = dispatch_event(env.s, organization_id=env.org.id, event_type="employer.created",
                          entity_kind="employer", entity_id=str(env.emp.id), facts={})
    env.s.commit()
    assert runs[0].status == "COMPLETED"
    step = [st for st in _steps(env, runs[0]) if st.node_type == "action.post_chat_notice"][0]
    assert step.status == "OK" and "no chat thread" in step.detail


def test_payslip_day_one_private_notification_per_paid_contractor(env):
    run = _held_run(env, env.emp, status="SETTLED", shortfall="0.00",
                    lines=(("Lena Fischer", "3400.00", "PAID"), ("Ghost NoEmail", "100.00", "PAID"),
                           ("Held Hank", "50.00", "HELD")))
    lines = env.s.scalars(select(PayLine).where(PayLine.run_id == run.id).order_by(PayLine.position)).all()
    lines[0].contractor_id = env.contractor.id  # Lena has an email; the ghost line has no contractor
    env.s.commit()
    _make_workflow(env, "payslip", _graph(
        ("t", "trigger.payroll_settled", {}),
        ("e", "action.send_email", {"template": "payslip_receipt", "recipient": "contractor"})))
    runs = dispatch_event(env.s, organization_id=env.org.id, event_type="payroll.settled",
                          entity_kind="payroll_run", entity_id=str(run.id), facts={}, runtime=_runtime())
    env.s.commit()
    assert runs[0].status == "COMPLETED"
    rows = _notes(env, kind="PAYSLIP_RECEIPT")
    assert len(rows) == 1  # only the paid contractor with an email address; HELD lines get nothing
    row = rows[0]
    assert row.recipient == "lena@acme.example" and row.sensitive is True
    assert "token=" not in row.body and "/receipt/" not in row.body  # the private link is redacted at rest
    assert row.secret and f"/receipt/{lines[0].id}?token=" in row.secret["text"]


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

def test_schedule_monthly_once_per_period(env):
    _make_workflow(env, "monthly", _graph(
        ("t", "trigger.schedule", {"cadence": "monthly", "day": 3, "fan_out": "org"}),
        ("x", "action.create_exception", {"title": "monthly tick", "detail": ""})))
    first = run_due_schedules(env.s, now=datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    env.s.commit()
    assert len(first) == 1 and first[0].entity_kind == "organization"
    assert ":2026-11" in first[0].dedupe_key
    second = run_due_schedules(env.s, now=datetime(2026, 11, 3, 18, 0, tzinfo=UTC))
    env.s.commit()
    assert second == []  # same period: no new run
    not_due = run_due_schedules(env.s, now=datetime(2026, 11, 4, 9, 0, tzinfo=UTC))
    env.s.commit()
    assert not_due == []
    next_period = run_due_schedules(env.s, now=datetime(2026, 12, 3, 9, 0, tzinfo=UTC))
    env.s.commit()
    assert len(next_period) == 1 and ":2026-12" in next_period[0].dedupe_key


def test_schedule_fan_out_employers(env):
    emp2 = Employer(organization_id=env.org.id, name="Borealis Labs AB", country="SE",
                    contact_email="bo@borealis.example", status="ACTIVE")
    env.s.add(emp2)
    env.s.commit()
    _make_workflow(env, "friday", _graph(
        ("t", "trigger.schedule", {"cadence": "weekly", "day": 4, "fan_out": "employers"}),
        ("x", "action.create_funding_reminder", {"note": "top up"})))
    # 2026-11-13 is a Friday (weekday() == 4).
    runs = run_due_schedules(env.s, now=datetime(2026, 11, 13, 8, 0, tzinfo=UTC))
    env.s.commit()
    assert len(runs) == 2
    assert {r.entity_id for r in runs} == {str(env.emp.id), str(emp2.id)}
    assert all(r.entity_kind == "employer" for r in runs)
    assert all(":2026-W46" in r.dedupe_key for r in runs)


# ---------------------------------------------------------------------------
# Prepare / revalidate
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Morning Brief (service level)
# ---------------------------------------------------------------------------

def test_brief_held_run_all_hold(env):
    held = _held_run(env, env.emp)
    entries = build_payroll_brief(env.s, organization_id=env.org.id)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["shortfall"] == "2642.88"
    assert entry["total_required"] == "6000.00"
    assert entry["run_status"] == "HELD_SHORTFALL" and entry["run_id"] == str(held.id)
    assert entry["employer_name"] == env.emp.name and entry["settlement_mode"] == "PARTIAL"
    assert entry["will_pay"] == [] and entry["can_approve"] is False
    assert [line["contractor_name"] for line in entry["will_hold"]] == ["Ingrid Solberg"]


def test_brief_partial_run_splits_pay_and_hold_and_skips_settled(env):
    _held_run(env, env.emp, status="PARTIALLY_PAID", shortfall="1200.00",
              lines=(("Lena Fischer", "3400.00", "PAID"), ("Ingrid Solberg", "2600.00", "HELD")))
    entries = build_payroll_brief(env.s, organization_id=env.org.id)
    assert len(entries) == 1
    assert [line["contractor_name"] for line in entries[0]["will_pay"]] == ["Lena Fischer"]
    assert [line["contractor_name"] for line in entries[0]["will_hold"]] == ["Ingrid Solberg"]
    env.s.execute(PayrollRun.__table__.update().values(status="SETTLED"))
    env.s.commit()
    assert build_payroll_brief(env.s, organization_id=env.org.id) == []  # nothing open: no card


def test_brief_is_tenant_scoped(env):
    _held_run(env, env.emp)
    other = Organization(name="Other Org", slug="other-org", country="US", base_currency="USD")
    env.s.add(other)
    env.s.commit()
    assert build_payroll_brief(env.s, organization_id=other.id) == []
    assert len(build_payroll_brief(env.s, organization_id=env.org.id)) == 1


# ---------------------------------------------------------------------------
# API surface (seeded demo org)
# ---------------------------------------------------------------------------

EXPECTED_TEMPLATES = {
    "New Contractor Onboarding", "Kit Completion Notice", "New Employer Welcome",
    "Payroll Held Alert", "Monthly Payroll Day", "Held Payroll Rescue",
    "Friday Funding Reminder", "Payslip Day",
}


def test_api_seed_templates_present_and_valid(demo_client):
    r = demo_client.get("/workflows")
    assert r.status_code == 200, r.text
    rows = r.json()
    names = {row["name"] for row in rows}
    assert EXPECTED_TEMPLATES <= names, names
    for row in rows:
        if row["name"] in EXPECTED_TEMPLATES:
            assert validate_graph(row["graph"]) == [], (row["name"], validate_graph(row["graph"]))
            assert row["enabled"] is True and row["trigger_type"].startswith("trigger.")


def test_api_workflow_crud_and_validation(demo_client):
    bad = {"name": "Bad", "description": None, "enabled": True,
           "graph": _graph(("t", "trigger.employer_added", {}), ("a", "action.approve_run", {}))}
    r = demo_client.post("/workflows", json=bad)
    assert r.status_code == 422, r.text
    assert "forbidden" in r.text
    no_trigger = {"name": "NoTrig", "enabled": True,
                  "graph": _graph(("a", "action.create_exception", {"title": "x"}))}
    r = demo_client.post("/workflows", json=no_trigger)
    assert r.status_code == 422, r.text

    good = {"name": "API Workflow", "description": "crud", "enabled": True,
            "graph": _graph(("t", "trigger.employer_added", {}),
                            ("x", "action.create_exception", {"title": "hi", "detail": ""}))}
    r = demo_client.post("/workflows", json=good)
    assert r.status_code == 201, r.text
    wf = r.json()
    assert wf["trigger_type"] == "trigger.employer_added"
    r = demo_client.get(f"/workflows/{wf['id']}")
    assert r.status_code == 200 and r.json()["name"] == "API Workflow"
    r = demo_client.put(f"/workflows/{wf['id']}", json={"enabled": False, "name": "API Workflow v2"})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["name"] == "API Workflow v2"
    r = demo_client.delete(f"/workflows/{wf['id']}")
    assert r.status_code == 204, r.text
    r = demo_client.get(f"/workflows/{wf['id']}")
    assert r.status_code == 404


def test_api_runs_tasks_brief_tick(demo_client):
    assert demo_client.get("/workflows/runs").status_code == 200
    assert demo_client.get("/workflows/tasks").status_code == 200
    r = demo_client.get("/workflows/brief")
    assert r.status_code == 200 and isinstance(r.json(), list)
    r = demo_client.post("/workflows/schedules/tick")
    assert r.status_code == 200 and "fired" in r.json()


def test_api_brief_lists_open_payroll_decisions(demo_client):
    emp = {e["name"]: e for e in demo_client.get("/employers").json()}["Borealis Labs AB"]
    detail = demo_client.get(f"/employers/{emp['id']}").json()
    run = demo_client.post("/runs", json={"employer_id": emp["id"],
                                          "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]})
    assert run.status_code in (200, 201), run.text
    entries = [e for e in demo_client.get("/workflows/brief").json() if e["run_id"] == run.json()["id"]]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["employer_name"] == "Borealis Labs AB" and entry["run_status"] == "HELD_SHORTFALL"
    assert entry["settlement_mode"] == "ALL_OR_NOTHING"  # fresh demo database: Borealis pays all or nothing
    assert entry["will_pay"] == [] and entry["shortfall"] is not None
    assert [line["contractor_name"] for line in entry["will_hold"]] == ["Ingrid Solberg"]


def test_api_run_detail_step_contract(demo_client):
    # Arrange a condition workflow in the demo org and fire it for a seeded contractor.
    db = demo_client.app.state.db
    graph = {"nodes": [
        _node("t", "trigger.contractor_joined", {}, x=40),
        _node("c", "condition.field_check", {"field": "amount", "operator": "gt", "value": "100"}, x=260),
        _node("x", "action.create_exception", {"title": "big", "detail": ""}, x=480),
    ], "edges": [_edge("t", "c"), _edge("c", "x", "true")]}
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        contractor = s.scalar(select(Contractor).where(Contractor.organization_id == org.id,
                                                        Contractor.full_name == "Lena Fischer"))
        wf = Workflow(organization_id=org.id, name="Detail Contract", enabled=True,
                      trigger_type="trigger.contractor_joined", graph=graph)
        s.add(wf)
        s.commit()
        runs = dispatch_event(s, organization_id=org.id, event_type="contractor.created",
                              entity_kind="contractor", entity_id=str(contractor.id),
                              facts={"amount": "250"})
        s.commit()
        mine = [r for r in runs if r.workflow_id == wf.id]
        assert len(mine) == 1
        run_id = str(mine[0].id)
    r = demo_client.get(f"/workflows/runs/{run_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entity_label"] == "Lena Fischer"
    cond = [st for st in body["steps"] if st["node_type"] == "condition.field_check"][0]
    assert isinstance(cond["output"], dict)  # parsed JSON, not a raw string
    assert cond["output"]["branch"] == "true"
    assert cond["output"]["field"] == "amount" and cond["output"]["operator"] == "gt"
    assert cond["output"]["value"] == "100" and cond["output"]["actual"] == "250"
    for step in body["steps"]:
        assert step["started_at"] is not None and step["finished_at"] is not None
