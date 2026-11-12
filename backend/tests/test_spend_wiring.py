"""Wiring: spend routes <-> workflow engine + the platform notifications outbox.

- the seeded Contractor Onboarding workflow parks at its kit gate and the kit DECISION ROUTE resumes it;
- the seeded Employer Welcome workflow queues an EMPLOYER_WELCOME notification for the contact;
- kit/trip creation queues approval requests for the deciders with the exact snapshot figures;
- trip.proposed / benefit.requested dispatches fire matching workflows;
- a completed kit mails the contractor (KIT_COMPLETED) with provider references, exactly once;
- every message is a Notification row (QUEUED when no mail credentials exist) and the template registry
  renders with real workflow contexts; a payslip mail is SENSITIVE (the private link is redacted at rest).
"""

from sqlalchemy import select

from app.db.models import NOTIFICATION_KINDS, Notification

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


def _mails(demo_client, kind, recipient=None):
    rows = _rows(demo_client, "Notification", kind=kind)
    return [n for n in rows if recipient is None or n.recipient == recipient]


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
    return {"id": nid, "type": ntype, "position": {"x": x, "y": 120}, "data": data or {}}


def _custom_workflow(demo_client, name, trigger, action_data=None):
    from app.db.models import Workflow

    db = demo_client.app.state.db
    org_id = _rows(demo_client, "Organization")[0].id
    graph = {"nodes": [_node("t", trigger), _node("a", "action.create_exception",
                                                  action_data or {"title": f"{name} fired"})],
             "edges": [{"id": "e1", "source": "t", "target": "a"}]}
    with db.sessionmaker() as s:
        wf = Workflow(organization_id=org_id, name=name, enabled=True, trigger_type=trigger, graph=graph)
        s.add(wf)
        s.commit()
        return wf.id


# --- workflow gate + welcome ------------------------------------------------

def test_contractor_onboarding_gate_resumes_on_kit_decision(demo_client):
    emp, con = _mk_employer_with_contractor(demo_client, "gate")
    runs = [r for r in _rows(demo_client, "WorkflowRun", entity_kind="contractor")
            if str(r.entity_id) == str(con["id"])]
    assert len(runs) == 1 and runs[0].status == "WAITING"

    kits = demo_client.get("/onboarding-kits/kits", params={"employer_id": emp["id"]}).json()
    assert len(kits) == 1 and kits[0]["source"] == "WORKFLOW" and kits[0]["status"] == "DRAFT"

    r = demo_client.post(f"/onboarding-kits/kits/{kits[0]['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text

    after = _rows(demo_client, "WorkflowRun", id=runs[0].id)[0]
    assert after.status == "COMPLETED"
    approvals = _rows(demo_client, "WorkflowApproval", workflow_run_id=runs[0].id)
    assert approvals and approvals[0].status == "APPROVED"


def test_gate_does_not_resume_without_the_real_decision(demo_client):
    emp, con = _mk_employer_with_contractor(demo_client, "nogate")
    run = next(r for r in _rows(demo_client, "WorkflowRun", entity_kind="contractor")
               if str(r.entity_id) == str(con["id"]))
    assert run.status == "WAITING"
    kit = demo_client.get("/onboarding-kits/kits", params={"employer_id": emp["id"]}).json()[0]
    # reading, listing, ticking schedules or hitting the brief never moves a parked run
    demo_client.get("/workflows/runs")
    demo_client.get("/workflows/brief")
    demo_client.post("/workflows/schedules/tick")
    assert _rows(demo_client, "WorkflowRun", id=run.id)[0].status == "WAITING"
    # a DENIAL is a real decision too: the run completes as rejected, downstream nodes never run
    r = demo_client.post(f"/onboarding-kits/kits/{kit['id']}/decision", json={"decision": "deny"})
    assert r.status_code == 200, r.text
    after = _rows(demo_client, "WorkflowRun", id=run.id)[0]
    assert after.status == "COMPLETED" and after.context["gate_outcome"] == "rejected"


def test_employer_welcome_workflow_queues_welcome_notification(demo_client):
    emp, _con = _mk_employer_with_contractor(demo_client, "welcome")
    runs = [r for r in _rows(demo_client, "WorkflowRun", entity_kind="employer")
            if str(r.entity_id) == str(emp["id"])]
    assert runs and all(r.status == "COMPLETED" for r in runs)
    rows = _mails(demo_client, "EMPLOYER_WELCOME", "ops-welcome@example.com")
    assert len(rows) == 1
    assert rows[0].status == "QUEUED"  # no mail credentials in tests: queued, never fake-sent
    assert "Wiring welcome Ltd" in rows[0].body


# --- decider notifications ----------------------------------------------------

def test_kit_create_queues_decider_approval_mail(demo_client):
    acme = _employer(demo_client)
    lena = _contractor(demo_client, acme["id"])
    before = len(_mails(demo_client, "SPEND_APPROVAL_REQUESTED"))
    r = demo_client.post("/onboarding-kits/kits", json={
        "employer_id": acme["id"], "contractor_id": lena["id"],
        "items": [{"kind": "GIFT_CARD", "label": "Welcome gift card", "merchant": "amazon.com",
                   "amount": "25.00", "currency": "USD"}]})
    assert r.status_code == 201, r.text
    rows = _mails(demo_client, "SPEND_APPROVAL_REQUESTED")
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.recipient == DEMO_EMAIL  # the demo owner is the org's decider
    assert row.status == "QUEUED"
    assert "25.00" in row.body and "Lena Fischer" in row.body
    assert row.body_html and row.meta["template"] == "approval_requested"


def test_trip_create_dispatches_and_queues_decider_mail(demo_client):
    wf_id = _custom_workflow(demo_client, "Wiring trip watcher", "trigger.trip_proposed")
    acme = _employer(demo_client)
    lena = _contractor(demo_client, acme["id"])
    r = demo_client.post("/travel/trips", json={
        "employer_id": acme["id"], "contractor_id": lena["id"], "destination": "Berlin",
        "start_date": "2026-12-01", "end_date": "2026-12-03", "purpose": "Client workshop",
        "components": [{"kind": "TRANSPORT", "label": "Airport transfer", "merchant": "taxico.example",
                        "amount": "60.00", "currency": "USD"}]})
    assert r.status_code == 201, r.text
    runs = _rows(demo_client, "WorkflowRun", workflow_id=wf_id)
    assert len(runs) == 1 and runs[0].status == "COMPLETED"
    rows = [n for n in _mails(demo_client, "SPEND_APPROVAL_REQUESTED") if "Berlin" in n.body]
    assert len(rows) == 1


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


# --- completion mail ---------------------------------------------------------

def test_kit_completion_mails_contractor_once(demo_client):
    emp, con = _mk_employer_with_contractor(demo_client, "complete")
    r = demo_client.post("/onboarding-kits/kits", json={
        "employer_id": emp["id"], "contractor_id": con["id"],
        "items": [{"kind": "GIFT_CARD", "label": "Welcome gift card", "merchant": "amazon.com",
                   "amount": "25.00", "currency": "USD"}]})
    assert r.status_code == 201, r.text
    kit = r.json()
    r = demo_client.put(f"/onboarding-kits/budgets/{emp['id']}", json={
        "currency": "USD", "monthly_cap": "500.00", "enabled": True})
    assert r.status_code == 200, r.text
    r = demo_client.post(f"/onboarding-kits/kits/{kit['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    r = demo_client.post(f"/onboarding-kits/kits/{kit['id']}/purchase-all")
    assert r.status_code == 200, r.text
    assert r.json()["kit"]["status"] == "COMPLETED"
    again = demo_client.post(f"/onboarding-kits/kits/{kit['id']}/purchase-all")  # idempotent: nothing re-bought
    assert again.status_code == 200
    rows = _mails(demo_client, "KIT_COMPLETED", "worker-complete@example.com")
    assert len(rows) == 1
    assert "25.00" in rows[0].body and "Welcome gift card" in rows[0].body


# --- notification contract ----------------------------------------------------

def test_new_kinds_are_registered_and_templates_render():
    from app.services import email_templates as tpl
    from app.services.spend_mail import TEMPLATE_NAMES

    assert set(TEMPLATE_NAMES) == {"approval_requested", "kit_completed", "trip_itinerary", "purchase_receipt",
                                   "employer_welcome", "payslip_receipt", "payroll_held_alert"}
    for _template, (_fn, kind, _sensitive) in tpl.SPEND_TEMPLATES.items():
        assert kind in NOTIFICATION_KINDS  # the Postgres CHECK widening reads the same tuple
    subject, text, html = tpl.employer_welcome({"employer_name": "Acme"})
    assert "Acme" in subject and "silo" in text and html.startswith("<!doctype html>")
    subject, text, _html = tpl.payslip_receipt({
        "contractor_name": "Lena Fischer", "employer_name": "Acme", "amount": "3400.00", "currency": "EUR",
        "receipt_link": "https://app.example/receipt/abc?token=x"})
    assert "3400.00" in subject and "/receipt/abc" in text
    assert tpl.SPEND_TEMPLATES["payslip_receipt"][2] is True  # carries a private link -> SENSITIVE


def test_trip_itinerary_lists_blocked_components_honestly():
    from app.services import email_templates as tpl

    _subject, text, _html = tpl.trip_itinerary({
        "contractor_name": "Lena Fischer", "destination": "Berlin", "start_date": "2026-12-05",
        "end_date": "2026-12-06",
        "components": [
            {"label": "Flight", "kind": "TRANSPORT", "amount": "450.00", "currency": "USD", "status": "BLOCKED"},
            {"label": "Hotel night", "kind": "LODGING", "amount": "180.00", "currency": "USD",
             "status": "PURCHASED", "provider_ref": "vic_sim_abc"}]})
    assert "exceeds the current credential limit" in text and "Not yet booked" in text
    assert "Booked (provider reference vic_sim_abc)" in text


def test_payslip_mail_is_redacted_in_the_dashboard(demo_client):
    from app.services import spend_mail

    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        org = s.scalar(select(Notification.organization_id))
        from app.db.models import Organization

        org = s.get(Organization, org) or s.scalar(select(Organization))
        row = spend_mail.queue_template(
            s, org, "payslip_receipt", "lena@acme.example",
            {"contractor_name": "Lena", "employer_name": "Acme", "amount": "10.00", "currency": "EUR",
             "receipt_link": "https://app.example/receipt/zzz?token=secret-token"})
        s.commit()
        assert row.sensitive is True and "secret-token" not in row.body
    listed = demo_client.get("/notifications").json()
    items = listed["items"] if isinstance(listed, dict) else listed
    assert "secret-token" not in str(items)
