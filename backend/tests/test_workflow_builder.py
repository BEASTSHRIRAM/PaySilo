"""Workflow builder: validator rules, /validate + registry + schedule endpoints, schedule tick, Silo drafting.

The LLM path of ``draft_workflow`` is exercised with STUBBED model clients only (a live model run is unverified).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.chat import service as chat_service
from app.chat import tools as chat_tools
from app.db.models import Employer, Membership, Organization, User, Workflow, WorkflowRun
from app.services import workflow_draft
from app.services.workflow_templates import build_payroll_schedule_graph, layout_graph
from app.services.workflow_validation import connection_problem, validate_graph_detailed
from app.services.workflows import run_due_schedules, validate_graph
from test_workflows import _edge, _graph, _make_workflow, _node, env  # noqa: F401  (env is a fixture)


def _g(nodes, edges):
    return {"nodes": nodes, "edges": edges}


def _msgs(graph, **kw):
    return [e["message"] for e in validate_graph_detailed(graph, **kw)]


# ---------------------------------------------------------------------------
# Validator rules
# ---------------------------------------------------------------------------

def test_trigger_to_trigger_is_refused_with_a_friendly_message():
    g = _g([_node("a", "trigger.employer_added"), _node("b", "trigger.payroll_held")], [_edge("a", "b")])
    errors = validate_graph_detailed(g)
    assert any(e["code"] == "connection" and "can't connect to another trigger" in e["message"] for e in errors)
    assert any(e["code"] == "trigger_count" for e in errors)  # two triggers is its own error


def test_nothing_may_connect_into_a_trigger():
    g = _g([_node("t", "trigger.employer_added"), _node("x", "action.create_exception", {"title": "x"})],
           [_edge("x", "t")])
    assert any("Nothing can connect into a trigger" in m for m in _msgs(g))


def test_cycle_self_loop_and_duplicate_edge():
    nodes = [_node("t", "trigger.employer_added"), _node("a", "action.create_exception", {"title": "a"}),
             _node("b", "action.create_exception", {"title": "b"})]
    cyc = _g(nodes, [_edge("t", "a"), _edge("a", "b"), _edge("b", "a")])
    assert any("form a loop" in m for m in _msgs(cyc))
    assert any("connect to itself" in m for m in _msgs(_g(nodes, [_edge("t", "a"), _edge("a", "a")])))
    dup = _g(nodes, [_edge("t", "a"), {"id": "again", "source": "t", "target": "a"}])
    assert any("already connected" in m for m in _msgs(dup))


def test_condition_handles_and_missing_nodes():
    nodes = [_node("t", "trigger.payroll_held"), _node("c", "condition.field_check", {"field": "amount"}),
             _node("a", "action.create_exception", {"title": "a"})]
    ok = _g(nodes, [_edge("t", "c"), _edge("c", "a", "true")])
    assert _msgs(ok) == []
    bad = _g(nodes, [_edge("t", "c"), _edge("c", "a", "maybe")])
    assert any("'yes' (true) and 'no' (false)" in m for m in _msgs(bad))
    assert any("missing node 'ghost'" in m for m in _msgs(_g(nodes, [_edge("t", "ghost")])))


def test_errors_carry_node_id_and_field():
    g = _g([_node("t", "trigger.employer_added"), _node("mail", "action.send_email", {})], [_edge("t", "mail")])
    err = next(e for e in validate_graph_detailed(g) if e["field"] == "template")
    assert err["node_id"] == "mail" and err["code"] == "required"
    unknown = validate_graph_detailed(_g([_node("t", "trigger.employer_added"), _node("z", "action.teleport")], []))
    assert any(e["node_id"] == "z" and e["code"] == "unknown_type" for e in unknown)
    forbidden = validate_graph_detailed(_g([_node("t", "trigger.employer_added"), _node("p", "action.pay_contractor")], []))
    assert any(e["node_id"] == "p" and e["code"] == "forbidden" for e in forbidden)


def test_required_config_for_exception_and_notice():
    g = _g([_node("t", "trigger.employer_added"), _node("e", "action.create_exception", {}),
            _node("n", "action.post_chat_notice", {})], [_edge("t", "e"), _edge("e", "n")])
    fields = {(e["node_id"], e["field"]) for e in validate_graph_detailed(g)}
    assert ("e", "title") in fields and ("n", "message") in fields


def test_size_limits():
    nodes = [_node("t", "trigger.employer_added")] + [
        _node(f"n{i}", "action.create_exception", {"title": "x"}) for i in range(70)]
    assert any("limit is 60" in m for m in _msgs(_g(nodes, [])))
    long_text = _g([_node("t", "trigger.employer_added"), _node("n", "action.post_chat_notice", {"message": "x" * 2500})],
                   [_edge("t", "n")])
    assert any("longer than 2000" in m for m in _msgs(long_text))


def test_scheduled_payroll_must_name_an_employer():
    sched_no_scope = {"cadence": "monthly", "day": 3, "time": "09:00", "timezone": "Asia/Kolkata"}
    g = _graph(("t", "trigger.schedule", sched_no_scope), ("p", "action.prepare_payroll_run", {}))
    assert any("scoped to one employer" in m for m in _msgs(g))
    ok = _graph(("t", "trigger.schedule", {**sched_no_scope, "employer_id": str(uuid.uuid4())}),
                ("p", "action.prepare_payroll_run", {}))
    assert _msgs(ok) == []
    fan = _graph(("t", "trigger.schedule", {**sched_no_scope, "fan_out": "employers"}),
                 ("p", "action.prepare_payroll_run", {}))
    assert _msgs(fan) == []


def test_schedule_fields_and_foreign_employer_are_checked():
    known, stranger = str(uuid.uuid4()), str(uuid.uuid4())
    base = {"cadence": "monthly", "day": 3, "employer_id": stranger}
    g = _graph(("t", "trigger.schedule", base), ("x", "action.create_exception", {"title": "x"}))
    assert any("not an employer of this organisation" in m for m in _msgs(g, employer_ids={known}))
    assert _msgs(g) == []  # no org context -> the id is only format-checked
    for patch, word in (({"timezone": "Mars/Base"}, "Unknown timezone"), ({"time": "9am"}, "Time of day"),
                        ({"weekend_rule": "whenever"}, "Weekend rule"), ({"day": 40}, "1-31"),
                        ({"cadence": "once", "date": "2026-02-30"}, "date")):
        bad = _graph(("t", "trigger.schedule", {**base, "employer_id": known, **patch}),
                     ("x", "action.create_exception", {"title": "x"}))
        assert any(word in m for m in _msgs(bad, employer_ids={known})), patch


def test_connection_problem_helper_matches_the_toast_wording():
    assert "another trigger" in connection_problem("trigger.schedule", "trigger.funds_received")
    assert connection_problem("action.send_email", "gate.wait_for_approval") is None


def test_layout_is_left_to_right_and_branches_stack():
    g = build_payroll_schedule_graph(employer_id=str(uuid.uuid4()), employer_name="Acme", day=3)
    pos = {n["id"]: n["position"] for n in g["nodes"]}
    assert g["layout"] == "lr"
    for e in g["edges"]:
        assert pos[e["source"]]["x"] < pos[e["target"]]["x"], e
    assert pos["email-approvers"]["y"] < pos["reminder"]["y"]  # 'true' branch above 'false'
    assert validate_graph(g) == []
    # a vertical legacy graph is re-laid horizontally
    legacy = {"nodes": [{**n, "position": {"x": 240, "y": 60 + i * 140}} for i, n in enumerate(g["nodes"])],
              "edges": g["edges"]}
    again = layout_graph(legacy)
    assert {n["position"]["x"] for n in again["nodes"]} != {240}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def _first_employer(demo_client):
    return demo_client.get("/employers").json()[0]


def test_node_registry_endpoint(demo_client):
    body = demo_client.get("/workflows/node-registry").json()
    types = {n["type"] for n in body["nodes"]}
    assert "trigger.schedule" in types and "gate.wait_for_approval" in types
    assert not [t for t in types if t.startswith("action.") and re.search(r"approve|pay_|purchase", t)]
    cond = next(n for n in body["nodes"] if n["type"] == "condition.field_check")
    assert cond["outputs"] == ["true", "false"] and next(n for n in body["nodes"] if n["kind"] == "trigger")["inputs"] == []
    assert any("Exactly one trigger" in r for r in body["rules"])


def test_validate_endpoint_precise_errors_and_no_side_effects(demo_client):
    before = len(demo_client.get("/workflows").json())
    bad = _g([_node("a", "trigger.employer_added"), _node("b", "trigger.payroll_held"),
              _node("m", "action.send_email", {"template": "nope"})], [_edge("a", "b")])
    r = demo_client.post("/workflows/validate", json={"graph": bad}).json()
    assert r["valid"] is False
    assert any(e["node_id"] == "m" and e["field"] == "template" for e in r["errors"])
    assert any(e["code"] == "connection" for e in r["errors"])
    good = _g([_node("a", "trigger.employer_added")], [])
    ok = demo_client.post("/workflows/validate", json={"graph": {"name": "x", "graph": good}}).json()  # envelope
    assert ok["valid"] is True and ok["trigger_type"] == "trigger.employer_added"
    assert ok["graph"]["layout"] == "lr" or ok["graph"]["nodes"][0]["position"]
    assert len(demo_client.get("/workflows").json()) == before
    assert demo_client.post("/workflows/validate", json={"graph": "nope"}).json()["valid"] is False


def test_validate_rejects_another_organisations_employer(demo_client):
    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        other = Organization(name="Other Org", slug="other-org", country="US", base_currency="USD")
        s.add(other)
        s.flush()
        foreign = Employer(organization_id=other.id, name="Foreign Co", country="US", contact_email="f@x.example",
                           status="ACTIVE")
        s.add(foreign)
        s.commit()
        foreign_id = str(foreign.id)
    g = _graph(("t", "trigger.schedule", {"cadence": "monthly", "day": 3, "employer_id": foreign_id}),
               ("p", "action.prepare_payroll_run", {}))
    r = demo_client.post("/workflows/validate", json={"graph": g}).json()
    assert not r["valid"] and any(e["code"] == "unknown_employer" for e in r["errors"])
    assert demo_client.post("/workflows", json={"name": "x", "graph": g}).status_code == 422
    assert demo_client.post("/workflows/employer-schedules", json={"employer_id": foreign_id, "day": 3}).status_code == 404


def test_schedule_preview_endpoint(demo_client):
    cfg = {"cadence": "monthly", "day": 31, "time": "09:00", "timezone": "Asia/Kolkata", "weekend_rule": "as_is"}
    r = demo_client.post("/workflows/schedule-preview", json={"config": cfg}).json()
    assert r["valid"] and len(r["next_runs"]) == 3 and r["timezone"] == "Asia/Kolkata"
    assert all(row["local"].endswith("09:00") for row in r["next_runs"])
    bad = demo_client.post("/workflows/schedule-preview", json={"config": {**cfg, "timezone": "Mars/Base"}}).json()
    assert bad["valid"] is False and bad["field"] == "timezone"


def test_employer_schedule_wizard_confirm_duplicates_and_calendar(demo_client):
    emp = _first_employer(demo_client)
    body = {"employer_id": emp["id"], "day": 3, "time": "09:00", "timezone": "Asia/Kolkata"}
    created = demo_client.post("/workflows/employer-schedules", json=body)
    assert created.status_code == 201
    wf = created.json()
    assert wf["enabled"] is False  # no confirm -> never enabled
    assert wf["trigger_type"] == "trigger.schedule"
    types = [n["type"] for n in wf["graph"]["nodes"]]
    assert "action.prepare_payroll_run" in types and "gate.wait_for_approval" in types
    assert not [t for t in types if t.startswith("action.") and re.search(r"approve|pay_|purchase", t)]
    assert demo_client.get(f"/workflows/employer-schedules?employer_id={emp['id']}").json() == []
    confirmed = demo_client.post("/workflows/employer-schedules", json={**body, "enabled": True, "day": 4}).json()
    assert confirmed["enabled"] is True
    dups = demo_client.get(f"/workflows/employer-schedules?employer_id={emp['id']}").json()
    assert [d["id"] for d in dups] == [confirmed["id"]]
    assert demo_client.post("/workflows/employer-schedules", json={"employer_id": emp["id"]}).status_code == 422
    occ = demo_client.get("/workflows/schedule-occurrences?start=2027-01-01&end=2027-03-31").json()
    mine = [o for o in occ if o["workflow_id"] == confirmed["id"]]
    assert [o["date"] for o in mine] == ["2027-01-04", "2027-02-04", "2027-03-04"]
    assert all(o["employer_name"] == emp["name"] for o in mine)  # read-only chips; disabled ones are not listed
    assert not [o for o in occ if o["workflow_id"] == wf["id"]]


# ---------------------------------------------------------------------------
# Schedule tick: per organisation, idempotent, catch-up, tenant-safe
# ---------------------------------------------------------------------------

def _sched_wf(env, employer, *, day=3, time="09:00", name="sched", org=None, saved_at=datetime(2026, 1, 1)):
    g = _graph(("t", "trigger.schedule", {"cadence": "monthly", "day": day, "time": time,
                                          "timezone": "Asia/Kolkata", "employer_id": str(employer.id)}),
               ("x", "action.create_exception", {"title": "Payroll day for {employer_name}"}))
    wf = _make_workflow(env, name, g)
    if org is not None:
        wf.organization_id = org.id
    wf.updated_at = saved_at
    env.s.commit()
    return wf


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_tick_fires_once_at_the_scheduled_time_and_is_idempotent(env):
    wf = _sched_wf(env, env.emp)  # 2026-12-03 09:00 IST = 03:30 UTC
    assert run_due_schedules(env.s, now=utc(2026, 12, 3, 3, 29)) == []
    runs = run_due_schedules(env.s, now=utc(2026, 12, 3, 3, 31))
    env.s.commit()
    assert len(runs) == 1 and runs[0].entity_kind == "employer" and runs[0].entity_id == str(env.emp.id)
    assert runs[0].dedupe_key.endswith(":2026-12-03T03:30Z") and runs[0].status == "COMPLETED"
    for later in (utc(2026, 12, 3, 3, 31), utc(2026, 12, 3, 9, 0), utc(2026, 12, 4, 3, 0)):
        assert run_due_schedules(env.s, now=later) == []
    assert len(env.s.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == wf.id)).all()) == 1
    nxt = run_due_schedules(env.s, now=utc(2027, 1, 3, 4, 0))  # next month is a new occurrence
    assert len(nxt) == 1


def test_tick_catches_up_one_missed_occurrence_within_24h_only(env):
    _sched_wf(env, env.emp)
    # server was down at the slot; it wakes 20h later: exactly one catch-up run
    assert len(run_due_schedules(env.s, now=utc(2026, 12, 3, 23, 30))) == 1
    assert run_due_schedules(env.s, now=utc(2026, 12, 3, 23, 45)) == []  # never a second fire
    env.s.commit()
    other = _sched_wf(env, env.emp, day=5, name="late")  # 2026-12-05 03:30 UTC; woken 25h later
    assert run_due_schedules(env.s, now=utc(2026, 12, 6, 4, 30)) == []
    assert env.s.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == other.id)).all() == []


def test_schedule_saved_after_its_slot_does_not_fire_retroactively(env):
    _sched_wf(env, env.emp, saved_at=datetime(2026, 12, 3, 5, 0))  # saved at 10:30 IST, after the 09:00 slot
    assert run_due_schedules(env.s, now=utc(2026, 12, 3, 6, 0)) == []


def test_two_employers_two_workflows_each_scoped(env):
    emp2 = Employer(organization_id=env.org.id, name="Harborview Ltd", country="GB",
                    contact_email="h@harbor.example", status="ACTIVE")
    env.s.add(emp2)
    env.s.commit()
    _sched_wf(env, env.emp, day=3, name="A on the 3rd")
    _sched_wf(env, emp2, day=4, name="H on the 4th")
    day3 = run_due_schedules(env.s, now=utc(2026, 12, 3, 4, 0))
    assert [r.entity_id for r in day3] == [str(env.emp.id)]
    day4 = run_due_schedules(env.s, now=utc(2026, 12, 4, 4, 0))
    assert [r.entity_id for r in day4] == [str(emp2.id)]


def test_tick_is_tenant_safe(env):
    other = Organization(name="Other", slug="other-t", country="US", base_currency="USD")
    env.s.add(other)
    env.s.flush()
    # a workflow of ANOTHER org pointing at this org's employer must never fire
    _sched_wf(env, env.emp, name="sneaky", org=other)
    assert run_due_schedules(env.s, now=utc(2026, 12, 3, 4, 0)) == []
    # the per-org tick only touches that org
    mine = _sched_wf(env, env.emp, name="mine")
    own_emp = Employer(organization_id=other.id, name="Own Co", country="US", contact_email="o@o.example",
                       status="ACTIVE")
    env.s.add(own_emp)
    env.s.commit()
    theirs = _sched_wf(env, own_emp, name="theirs", org=other)
    runs = run_due_schedules(env.s, now=utc(2026, 12, 3, 4, 0), organization_id=env.org.id)
    assert [r.workflow_id for r in runs] == [mine.id]
    runs2 = run_due_schedules(env.s, now=utc(2026, 12, 3, 4, 0))
    assert [r.workflow_id for r in runs2] == [theirs.id]


def test_disabled_schedule_never_fires(env):
    wf = _sched_wf(env, env.emp)
    wf.enabled = False
    env.s.commit()
    assert run_due_schedules(env.s, now=utc(2026, 12, 3, 4, 0)) == []


# ---------------------------------------------------------------------------
# draft_workflow (STUBBED model client)
# ---------------------------------------------------------------------------

@pytest.fixture
def ctx_factory(demo_client):
    db = demo_client.app.state.db
    runtime = demo_client.app.state.runtime
    sessions = []

    def make(role=None):
        s = db.sessionmaker()
        sessions.append(s)
        membership = s.scalars(select(Membership).where(Membership.role == "owner")).first()
        org = s.get(Organization, membership.organization_id)
        user = s.get(User, membership.user_id)
        return chat_tools.ChatContext(db=s, runtime=runtime, organization_id=org.id, organization_name=org.name,
                                      user_id=user.id, user_email=user.email, role=role or membership.role,
                                      demo_mode=True)
    yield make
    chat_tools.DRAFT_MODEL_FACTORY = None
    for s in sessions:
        s.close()


class StubModel:
    """Returns the queued replies in order; records every message list it was invoked with."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def invoke(self, messages):
        self.calls.append(list(messages))
        return SimpleNamespace(content=self.replies[min(len(self.calls) - 1, len(self.replies) - 1)])


def _good_reply(ctx, **extra):
    emp = ctx.db.scalars(select(Employer).where(Employer.organization_id == ctx.organization_id)).first()
    graph = build_payroll_schedule_graph(employer_id=str(emp.id), employer_name=emp.name, day=3)
    return json.dumps({"name": "Acme payroll day", "description": "d", "graph": graph, **extra}), emp


def _wf_count(ctx):
    return len(ctx.db.scalars(select(Workflow).where(Workflow.organization_id == ctx.organization_id)).all())


def test_draft_valid_json_saves_a_disabled_flagged_workflow_and_links_to_it(ctx_factory):
    ctx = ctx_factory()
    reply, _ = _good_reply(ctx, enabled=True)  # the model asks for enabled: ignored
    stub = StubModel(reply)
    chat_tools.DRAFT_MODEL_FACTORY = lambda c: stub
    before = _wf_count(ctx)
    result = chat_tools.draft_workflow(ctx, "monthly payroll for Acme on day 3")
    assert result.data["drafted"] is True and result.data["enabled"] is False and len(stub.calls) == 1
    wf = ctx.db.get(Workflow, uuid.UUID(result.data["workflow_id"]))
    assert wf.enabled is False and wf.drafted_by_agent is True and wf.organization_id == ctx.organization_id
    assert wf.graph["layout"] == "lr"
    assert f"/dashboard/workflows/{wf.id}" in result.text and "can't enable" in result.text
    assert _wf_count(ctx) == before + 1
    # the registry and the employer list (ids + names only) reached the model
    system = stub.calls[0][0].content
    assert "trigger.schedule" in system and "EMPLOYERS" in system and "iban" not in system.lower()


def test_draft_invalid_then_valid_retries_with_the_validator_errors(ctx_factory):
    ctx = ctx_factory()
    bad = json.dumps({"name": "x", "graph": _g([_node("a", "trigger.employer_added"), _node("b", "trigger.payroll_held")],
                                               [_edge("a", "b")])})
    good, _ = _good_reply(ctx)
    stub = StubModel(bad, good)
    chat_tools.DRAFT_MODEL_FACTORY = lambda c: stub
    result = chat_tools.draft_workflow(ctx, "something")
    assert result.data["drafted"] is True and result.data["attempts"] == 2 and len(stub.calls) == 2
    feedback = stub.calls[1][-1].content
    assert "can't connect to another trigger" in feedback and "exactly one trigger" in feedback


def test_draft_always_invalid_fails_after_two_retries_and_saves_nothing(ctx_factory):
    ctx = ctx_factory()
    stub = StubModel(json.dumps({"graph": _g([_node("p", "action.pay_contractor")], [])}), "not json at all")
    chat_tools.DRAFT_MODEL_FACTORY = lambda c: stub
    before = _wf_count(ctx)
    result = chat_tools.draft_workflow(ctx, "pay everyone")
    assert result.data["drafted"] is False and len(stub.calls) == 3  # first try + 2 retries
    assert "couldn't produce a valid workflow" in result.text and "nothing was saved" in result.text
    assert _wf_count(ctx) == before


def test_draft_rejects_a_foreign_employer_id_from_the_model(ctx_factory):
    ctx = ctx_factory()
    graph = build_payroll_schedule_graph(employer_id=str(uuid.uuid4()), employer_name="Ghost", day=3)
    chat_tools.DRAFT_MODEL_FACTORY = lambda c: StubModel(json.dumps({"name": "x", "graph": graph}))
    before = _wf_count(ctx)
    result = chat_tools.draft_workflow(ctx, "payroll for Ghost")
    assert result.data["drafted"] is False and _wf_count(ctx) == before


def test_draft_keyless_matcher_builds_a_monthly_payroll_schedule(ctx_factory):
    ctx = ctx_factory()
    chat_tools.DRAFT_MODEL_FACTORY = lambda c: None
    result = chat_tools.draft_workflow(ctx, "monthly payroll for Acme Manufacturing GmbH on day 3")
    assert result.data["drafted"] is True and result.data["source"] == "matcher"
    wf = ctx.db.get(Workflow, uuid.UUID(result.data["workflow_id"]))
    trig = next(n for n in wf.graph["nodes"] if n["type"] == "trigger.schedule")
    assert trig["data"]["day"] == 3 and trig["data"]["employer_id"] and wf.enabled is False and wf.drafted_by_agent


def test_draft_keyless_says_it_needs_the_api_key_for_anything_else(ctx_factory):
    ctx = ctx_factory()
    chat_tools.DRAFT_MODEL_FACTORY = lambda c: None
    before = _wf_count(ctx)
    result = chat_tools.draft_workflow(ctx, "when a contractor joins, email their manager a poem")
    assert result.data["drafted"] is False and "API key" in result.text and _wf_count(ctx) == before
    ask = chat_tools.draft_workflow(ctx, "monthly payroll on day 3")  # no employer named: asks, never guesses
    assert ask.data["drafted"] is False and "which employer" in ask.text


def test_draft_requires_owner_or_admin(ctx_factory):
    ctx = ctx_factory(role="viewer")
    chat_tools.DRAFT_MODEL_FACTORY = lambda c: StubModel("{}")
    assert chat_tools.draft_workflow(ctx, "monthly payroll for Acme on day 3").data["drafted"] is False


def test_agent_has_no_way_to_enable_run_approve_or_delete_workflows():
    names = set(chat_tools.TOOL_FUNCTIONS)
    assert "draft_workflow" in names
    assert not [n for n in names if re.search(r"enable|activate|run_workflow|delete|remove|approve_|decide", n)]
    assert not [n for n in names if "workflow" in n and n not in ("draft_workflow", "read_workflow_runs")]
    import inspect

    source = inspect.getsource(workflow_draft)
    assert "enabled=False" in source and "enabled=True" not in source
    assert "execute_run" not in source and "dispatch_event" not in source and "resolve_approval" not in source


def test_fallback_router_sends_creation_requests_to_draft_workflow(ctx_factory):
    ctx = ctx_factory()
    steps, detail, _ = chat_service.fallback_plan(ctx, "Create a workflow: monthly payroll for Acme on day 3", {})
    assert steps[0][0] == "draft_workflow" and "DISABLED" in detail
    steps, _, _ = chat_service.fallback_plan(ctx, "show workflow runs", {})
    assert steps[0][0] == "read_workflow_runs"
