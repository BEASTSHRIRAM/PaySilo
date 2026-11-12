"""Coordinator wiring: spend routes <-> workflow engine + email outbox.

Proves the glue the parallel lanes could not test against each other:

- the seeded Contractor Onboarding workflow parks at its kit gate and the
  kit DECISION ROUTE resumes it (resolve_approval_for_entity wiring);
- the seeded Employer Welcome workflow completes when the employer has a
  contact email, recording an employer_welcome outbox row (NOT_CONFIGURED);
- API kit/trip creation enqueues approval_requested for the deciders with
  the exact snapshot figures;
- trip.proposed / benefit.requested dispatches fire matching workflows;
- a completed kit emails the contractor (kit_completed) with provider refs;
- the email related-kind vocabulary accepts the workflow engine's kinds
  (payroll_run / employer / contractor) and both coordinator templates
  render with their real workflow contexts.
"""

import uuid

ACME = "Acme Manufacturing GmbH"
DEMO_EMAIL = "demo@paysilo.test"


def _rows(demo_client, model_name, **filters):
    from app.db import models

    model = getattr(models, model_name)
    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        q = s.query(model)
        for column, value in filters.items():
            q = q.filter(getattr(model, column) == value)
        rows = q.all()
        for row in rows:
            s.expunge(row)
        return rows


def _employer(demo_client, name=ACME):
    rows = demo_client.get("/employers").json()
    return next(e for e in rows if e["name"] == name)


def _contractor(demo_client, employer_id, name="Lena Fischer"):
    detail = demo_client.get(f"/employers/{employer_id}").json()
    return next(c for c in detail["contractors"] if c["full_name"] == name)


def _mk_employer_with_contractor(demo_client, suffix):
    emp = demo_client.post("/employers", json={
        "name": f"Wiring {suffix} Ltd", "country": "de",
        "contact_name": "Ops Lead", "contact_email": f"ops-{suffix}@example.com"}).json()
    con = demo_client.post(f"/employers/{emp['id']}/contractors", json={
        "full_name": f"Worker {suffix}", "email": f"worker-{suffix}@example.com",
        "country": "DE", "currency": "EUR", "payout_method": "SWIFT",
        "iban": "DE89370400440532013000", "swift_code": "COBADEFFXXX",
        "default_amount": "1000"})
    assert con.status_code == 201, con.text
    return emp, con.json()


def _node(nid, ntype, data=None, x=0):
    return {"id": nid, "type": ntype, "position": {"x": x, "y": 120},
            "data": data or {}}


def _custom_workflow(demo_client, name, trigger, action_data=None):
    from app.db.models import Workflow

    db = demo_client.app.state.db
    org_id = _rows(demo_client, "Organization")[0].id
    graph = {"nodes": [_node("t", trigger), _node("a", "action.create_exception",
                                                  action_data or {"title": f"{name} fired"})],
             "edges": [{"id": "e1", "source": "t", "target": "a"}]}
    with db.sessionmaker() as s:
        wf = Workflow(organization_id=org_id, name=name, enabled=True,
                      trigger_type=trigger, graph=graph)
        s.add(wf)
        s.commit()
        return wf.id


# --- workflow gate + welcome ------------------------------------------------

def test_contractor_onboarding_gate_resumes_on_kit_decision(demo_client):
    emp, con = _mk_employer_with_contractor(demo_client, "gate")
    runs = [r for r in _rows(demo_client, "WorkflowRun", entity_kind="contractor")
            if str(r.entity_id) == str(con["id"])]
    assert len(runs) == 1 and runs[0].status == "WAITING"

    kits = demo_client.get("/onboarding/kits", params={"employer_id": emp["id"]}).json()
    assert len(kits) == 1 and kits[0]["source"] == "WORKFLOW" and kits[0]["status"] == "DRAFT"

    r = demo_client.post(f"/onboarding/kits/{kits[0]['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text

    after = _rows(demo_client, "WorkflowRun", id=runs[0].id)[0]
    assert after.status == "COMPLETED"
    approvals = _rows(demo_client, "WorkflowApproval", workflow_run_id=runs[0].id)
    assert approvals and approvals[0].status == "APPROVED"


def test_employer_welcome_workflow_emails_contact(demo_client):
    emp, _con = _mk_employer_with_contractor(demo_client, "welcome")
    runs = [r for r in _rows(demo_client, "WorkflowRun", entity_kind="employer")
            if str(r.entity_id) == str(emp["id"])]
    assert runs and all(r.status == "COMPLETED" for r in runs)
    rows = [o for o in _rows(demo_client, "EmailOutbox", template="employer_welcome")
            if o.to_address == "ops-welcome@example.com"]
    assert len(rows) == 1
    assert rows[0].status == "NOT_CONFIGURED"  # no SMTP in tests — recorded, never fake-sent
    assert "Wiring welcome Ltd" in rows[0].body_text


# --- decider notifications ----------------------------------------------------

def test_kit_create_enqueues_decider_approval_email(demo_client):
    acme = _employer(demo_client)
    lena = _contractor(demo_client, acme["id"])
    before = len(_rows(demo_client, "EmailOutbox", template="approval_requested"))
    r = demo_client.post("/onboarding/kits", json={
        "employer_id": acme["id"], "contractor_id": lena["id"],
        "items": [{"kind": "GIFT_CARD", "label": "Welcome gift card", "merchant": "Amazon",
                   "amount": "25.00", "currency": "USD"}]})
    assert r.status_code == 201, r.text
    rows = _rows(demo_client, "EmailOutbox", template="approval_requested", related_kind="kit")
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.to_address == DEMO_EMAIL  # the demo owner is the org's decider
    assert row.status == "NOT_CONFIGURED"
    assert "25.00" in row.body_text and "Lena Fischer" in row.body_text


def test_trip_create_dispatches_and_enqueues_decider_email(demo_client):
    wf_id = _custom_workflow(demo_client, "Wiring trip watcher", "trigger.trip_proposed")
    acme = _employer(demo_client)
    lena = _contractor(demo_client, acme["id"])
    r = demo_client.post("/travel/trips", json={
        "employer_id": acme["id"], "contractor_id": lena["id"], "destination": "Berlin",
        "start_date": "2026-12-01", "end_date": "2026-12-03", "purpose": "Client workshop",
        "components": [{"kind": "TRANSPORT", "label": "Airport transfer", "merchant": "TaxiCo",
                        "amount": "60.00", "currency": "USD"}]})
    assert r.status_code == 201, r.text
    runs = _rows(demo_client, "WorkflowRun", workflow_id=wf_id)
    assert len(runs) == 1 and runs[0].status == "COMPLETED"
    rows = [o for o in _rows(demo_client, "EmailOutbox", template="approval_requested",
                             related_kind="trip") if str(o.related_id) == r.json()["id"]]
    assert len(rows) == 1 and "Berlin" in rows[0].body_text


def test_benefit_create_dispatches_workflow(demo_client):
    wf_id = _custom_workflow(demo_client, "Wiring benefit watcher", "trigger.benefit_requested")
    acme = _employer(demo_client)
    lena = _contractor(demo_client, acme["id"])
    r = demo_client.post("/benefits/requests", json={
        "employer_id": acme["id"], "contractor_id": lena["id"], "merchant": "Coursera",
        "item": "Python course", "amount": "75.00", "currency": "USD"})
    assert r.status_code == 201, r.text
    runs = _rows(demo_client, "WorkflowRun", workflow_id=wf_id)
    assert len(runs) == 1 and runs[0].status == "COMPLETED"


# --- completion email ---------------------------------------------------------

def test_kit_completion_emails_contractor(demo_client):
    emp, con = _mk_employer_with_contractor(demo_client, "complete")
    r = demo_client.post("/onboarding/kits", json={
        "employer_id": emp["id"], "contractor_id": con["id"],
        "items": [{"kind": "GIFT_CARD", "label": "Welcome gift card", "merchant": "Amazon",
                   "amount": "25.00", "currency": "USD"}]})
    assert r.status_code == 201, r.text
    kit = r.json()
    # A budget is required before purchases: enable one for this employer.
    r = demo_client.put(f"/onboarding/budgets/{emp['id']}", json={
        "currency": "USD", "monthly_cap": "500.00", "enabled": True})
    assert r.status_code == 200, r.text
    r = demo_client.post(f"/onboarding/kits/{kit['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    r = demo_client.post(f"/onboarding/kits/{kit['id']}/purchase-all")
    assert r.status_code == 200, r.text
    assert r.json()["kit"]["status"] == "COMPLETED"
    rows = [o for o in _rows(demo_client, "EmailOutbox", template="kit_completed")
            if o.to_address == "worker-complete@example.com"]
    assert len(rows) == 1
    assert "25.00" in rows[0].body_text and "Welcome gift card" in rows[0].body_text


# --- email platform contract ----------------------------------------------------

def test_email_vocabulary_and_coordinator_templates(demo_client):
    from app.services.email import enqueue_email, render_template

    db = demo_client.app.state.db
    settings = demo_client.app.state.settings
    org_id = _rows(demo_client, "Organization")[0].id
    with db.sessionmaker() as s:
        for kind in ("payroll_run", "contractor", "employer"):
            row = enqueue_email(s, settings, organization_id=org_id,
                                template="purchase_receipt", to="a@example.com",
                                context={"buyer_label": "Acme", "item_label": "Seat",
                                         "merchant": "GitHub", "amount": "12.00",
                                         "currency": "USD"},
                                related_kind=kind, related_id=uuid.uuid4())
            assert row.status == "NOT_CONFIGURED"
        s.commit()

    subject, text, _html = render_template("employer_welcome", {"employer_name": "Acme"})
    assert "Acme" in subject and "silo" in text
    subject, text, _html = render_template("payslip_receipt", {
        "contractor_name": "Lena Fischer", "employer_name": "Acme",
        "amount": "3400.00", "currency": "EUR", "receipt_link": "/receipt/abc?token=x"})
    assert "3,400.00" in subject or "3400.00" in subject
    assert "/receipt/abc" in text
