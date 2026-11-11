"""Funding planner: deterministic math, business days, statuses, reminders, outbox, routes, migration."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select

from app.config import Settings
from app.crew import draft, funding
from app.db import Database
from app.db.models import Employer, Notification, Organization
from app.policy import PAYROLL_POLICY, ContractorPayroll, RefusalError, assess_payroll
from app.services import notify, reminders
from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

MON = date(2026, 11, 2)  # a Monday


@pytest.fixture
def env(demo_client):
    app = demo_client.app
    funding.clear_caches()
    return demo_client, app.state.runtime, app.state.db, app.state.settings


def _emp(db, prefix):
    with db.sessionmaker() as s:
        return s.scalar(select(Employer).where(Employer.name.like(f"{prefix}%"))).id


def _plan(db, runtime, emp_id, today=MON, **fields):
    with db.sessionmaker() as s:
        e = s.get(Employer, emp_id)
        for k, v in fields.items():
            setattr(e, k, v)
        s.commit()
        from sqlalchemy.orm import selectinload
        e = s.scalar(select(Employer).options(selectinload(Employer.contractors)).where(Employer.id == emp_id))
        return funding.plan_employer(s, runtime, e, today)


# --------------------------------------------------------------------------- pure functions
def test_fund_by_skips_weekends():
    assert funding.business_days_before(date(2026, 11, 9), 2) == date(2026, 11, 5)  # Mon -> Thu
    assert funding.business_days_before(date(2026, 11, 10), 2) == date(2026, 11, 6)  # Tue -> Fri
    assert funding.business_days_before(date(2026, 11, 11), 2) == date(2026, 11, 9)  # Wed -> Mon
    assert funding.business_days_before(date(2026, 11, 9), 0) == date(2026, 11, 9)
    assert funding.business_days_between(date(2026, 11, 6), date(2026, 11, 9)) == 1  # Fri -> Mon


@pytest.mark.parametrize("gap,non_usd,expected", [
    ("0", "5000", "0.00"),
    ("1000", "0", "1000.00"),  # USD only: no buffer, already a multiple of 50
    ("1001", "0", "1050.00"),  # rounds UP to the next 50
    ("1000", "1000", "1050.00"),  # 1000 + 3% = 1030 -> 1050
    ("1000", "400", "1050.00"),  # buffer only on the non-USD part: 1012 -> 1050
    ("100", "100000", "150.00"),  # non-USD part capped by the gap: 103 -> 150
    ("1666.67", "9999", "1750.00"),  # 1666.67 + 50.00 = 1716.67 -> 1750
])
def test_recommended_transfer_formula(gap, non_usd, expected):
    assert funding.recommended_transfer(Decimal(gap), Decimal(non_usd)) == Decimal(expected)


def test_next_occurrence_rolls_stale_dates():
    assert funding.next_occurrence(date(2026, 10, 1), "WEEKLY", date(2026, 11, 2)) == date(2026, 11, 5)
    assert funding.next_occurrence(date(2026, 10, 1), "BIWEEKLY", date(2026, 11, 2)) == date(2026, 11, 12)
    assert funding.next_occurrence(date(2026, 10, 31), "MONTHLY", date(2026, 11, 2)) == date(2026, 11, 30)
    assert funding.next_occurrence(date(2026, 10, 1), None, date(2026, 11, 2)) == date(2026, 10, 1)
    assert funding.next_occurrence(date(2026, 11, 9), "WEEKLY", date(2026, 11, 2)) == date(2026, 11, 9)


@pytest.mark.parametrize("kw,expected", [
    (dict(has_lines=False, has_schedule=True, is_short=True, days_until_fund_by=5), "NO_LINES"),
    (dict(has_lines=True, has_schedule=False, is_short=True, days_until_fund_by=None), "NO_SCHEDULE"),
    (dict(has_lines=True, has_schedule=True, is_short=False, days_until_fund_by=-9), "FUNDED"),
    (dict(has_lines=True, has_schedule=True, is_short=True, days_until_fund_by=10), "SHORT"),
    (dict(has_lines=True, has_schedule=True, is_short=True, days_until_fund_by=4), "SHORT"),
    (dict(has_lines=True, has_schedule=True, is_short=True, days_until_fund_by=3), "DUE_SOON"),
    (dict(has_lines=True, has_schedule=True, is_short=True, days_until_fund_by=0), "DUE_SOON"),
    (dict(has_lines=True, has_schedule=True, is_short=True, days_until_fund_by=-1), "OVERDUE"),
])
def test_status_transitions(kw, expected):
    assert funding.classify(**kw) == expected


# --------------------------------------------------------------------------- planner vs the real policy
@pytest.mark.parametrize("prefix,topup", [("Acme", 0), ("Borealis", 0), ("Borealis", 2000), ("Cobalt", 0),
                                          ("Cobalt", 3000), ("Delta", 0)])
def test_plan_matches_assess_payroll(env, prefix, topup):
    _, runtime, db, _ = env
    emp_id = _emp(db, prefix)
    if topup:
        with db.sessionmaker() as s:
            runtime.fund(s, s.get(Employer, emp_id), Decimal(topup), reference=f"t-{prefix}-{topup}")
            s.commit()
    plan = _plan(db, runtime, emp_id, next_pay_date=date(2026, 11, 20))
    with db.sessionmaker() as s:
        e = s.get(Employer, emp_id)
        lines = [ContractorPayroll(c.full_name, c.default_amount, c.currency, c.payout_method)
                 for c in e.contractors if c.is_active and c.default_amount]
        a = assess_payroll(lines, runtime.services.usd_rates, {k: Decimal(v) for k, v in runtime.balances(s, e).items()})
    assert plan.payroll_usd == a.payroll_usd
    assert plan.run_fee_usd == PAYROLL_POLICY.platform_fee_per_run_usd
    assert plan.reserve_floor_usd == PAYROLL_POLICY.reserve_floor_usd
    assert plan.required_in_wallet_usd == a.required_usd + a.reserve_floor_usd
    assert plan.available_usd == a.available_usd
    assert plan.gap_usd == a.shortfall_usd
    assert plan.is_short == (not a.can_run)
    assert (plan.recommended_transfer_usd >= plan.gap_usd)
    assert plan.recommended_transfer_usd % 50 == 0


def test_never_mixes_employers(env):
    _, runtime, db, _ = env
    acme, bor = _emp(db, "Acme"), _emp(db, "Borealis")
    before = _plan(db, runtime, bor, next_pay_date=date(2026, 11, 20))
    with db.sessionmaker() as s:
        runtime.fund(s, s.get(Employer, acme), Decimal("500000"), reference="whale")
        s.commit()
    after = _plan(db, runtime, bor)
    assert after.available_usd == before.available_usd
    assert after.gap_usd == before.gap_usd > 0
    assert after.status == "SHORT"
    assert _plan(db, runtime, acme).status == "FUNDED"


def test_lines_and_needs_amount(env):
    _, runtime, db, _ = env
    emp_id = _emp(db, "Acme")
    with db.sessionmaker() as s:
        e = s.get(Employer, emp_id)
        e.contractors[0].default_amount = None
        s.commit()
    plan = _plan(db, runtime, emp_id, next_pay_date=date(2026, 11, 20))
    assert [ln.name for ln in plan.lines] == ["Tomas Rivera"]
    assert [ln.name for ln in plan.needs_amount] == ["Lena Fischer"]
    assert plan.lines[0].fee == Decimal("12.85") and plan.lines[0].amount_usd == Decimal("2808.00")


def test_status_by_dates_and_missing_pieces(env):
    _, runtime, db, _ = env
    bor = _emp(db, "Borealis")  # short
    assert _plan(db, runtime, bor, next_pay_date=None, pay_frequency=None).status == "NO_SCHEDULE"
    assert _plan(db, runtime, bor, next_pay_date=date(2026, 11, 20), funding_lead_days=2).status == "SHORT"
    assert _plan(db, runtime, bor, next_pay_date=date(2026, 11, 6), funding_lead_days=2).status == "DUE_SOON"
    p = _plan(db, runtime, bor, next_pay_date=date(2026, 10, 30), funding_lead_days=2)  # fund_by Wed 28 Oct, past
    assert p.status == "OVERDUE" and p.fund_by == date(2026, 10, 28)
    with db.sessionmaker() as s:
        for c in s.get(Employer, bor).contractors:
            c.is_active = False
        s.commit()
    assert _plan(db, runtime, bor).status == "NO_LINES"


def test_live_rate_failure_degrades_never_guesses(env):
    _, runtime, db, _ = env

    class Failing:
        def rates_for(self, *a, **k):
            from app.airwallex.rates import LiveRateUnavailable
            raise LiveRateUnavailable("quote failed")

    runtime.services.rate_provider = Failing()
    try:
        plan = _plan(db, runtime, _emp(db, "Acme"), next_pay_date=date(2026, 11, 20))
    finally:
        runtime.services.rate_provider = None
    assert plan.status == "RATE_UNAVAILABLE" and "unavailable" in plan.message and plan.lines == []


def test_live_rates_are_cached(env):
    _, runtime, db, _ = env
    calls = []

    class Counting:
        def rates_for(self, currencies, *, account_id, run_id):
            calls.append(run_id)
            return {"USD": Decimal("1"), "EUR": Decimal("1.10")}

    runtime.services.rate_provider = Counting()
    try:
        emp = _emp(db, "Acme")
        a = _plan(db, runtime, emp, next_pay_date=date(2026, 11, 20))
        _plan(db, runtime, emp)
        assert len(calls) == 1
        assert a.lines[0].amount_usd == Decimal("3740.00")  # live 1.10, not the 1.08 demo table
    finally:
        runtime.services.rate_provider = None
        funding.clear_caches()


def test_wire_details_shape_and_pending(env):
    _, runtime, db, _ = env
    plan = _plan(db, runtime, _emp(db, "Acme"), next_pay_date=date(2026, 11, 20))
    w = plan.wire
    assert w["status"] == "READY" and w["account_number"] and w["bank_name"] and w["swift_code"]
    assert {r["type"] for r in w["routing"]} >= {"ach", "fedwire"}
    assert not any(k in w for k in ("id", "request_id", "airwallex_account_id"))
    import json
    pending = json.loads(open("tests/fixtures/airwallex/global_account_get.json").read())
    with db.sessionmaker() as s:
        raw_ready = funding._wire_from_raw(pending, s.get(Employer, _emp(db, "Acme")))
        assert raw_ready["status"] == "PENDING" and raw_ready["account_number"] is None


# --------------------------------------------------------------------------- routes
def test_plan_routes_and_schedule_validation(env):
    c, _, db, _ = env
    r = c.get("/funding/plan")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["employers"] == 4 and len(body["plans"]) == 4
    by_name = {p["employer_name"].split()[0]: p for p in body["plans"]}
    assert by_name["Acme"]["status"] == "FUNDED" and by_name["Borealis"]["is_short"]
    eid = by_name["Delta"]["employer_id"]
    ok = c.patch(f"/employers/{eid}/schedule", json={"pay_frequency": "WEEKLY", "funding_lead_days": 3})
    assert ok.status_code == 200 and ok.json()["pay_frequency"] == "WEEKLY" and ok.json()["funding_lead_days"] == 3
    assert c.get(f"/employers/{eid}/funding-plan").json()["funding_lead_days"] == 3
    for bad in ({"pay_frequency": "DAILY"}, {"funding_lead_days": 30}, {"funding_lead_days": -1},
                {"next_pay_date": "1999-01-01"}, {"next_pay_date": "not-a-date"}):
        assert c.patch(f"/employers/{eid}/schedule", json=bad).status_code == 422, bad
    cleared = c.patch(f"/employers/{eid}/schedule", json={"next_pay_date": None, "pay_frequency": None})
    assert cleared.json()["status"] == "NO_SCHEDULE" and cleared.json()["pay_date"] is None


def _login(c, email, password):
    c.post("/auth/logout")
    assert c.post("/auth/login", json={"email": email, "password": password}).status_code == 200


def test_viewer_forbidden_and_tenant_isolation(env):
    c, _, db, _ = env
    plans = c.get("/funding/plan").json()["plans"]
    eid = plans[0]["employer_id"]
    # a viewer in the same org: can read, cannot change or send
    inv_r = c.post("/org/invitations", json={"email": "viewer@example.com", "role": "viewer"})
    assert inv_r.status_code == 201, inv_r.text
    inv = inv_r.json()
    token = inv["invite_path"].split("/")[-1]
    c.post("/auth/logout")
    assert c.post(f"/invitations/{token}/accept", json={"full_name": "View Er", "password": "ViewerPass2026"}).status_code == 200
    _login(c, "viewer@example.com", "ViewerPass2026")
    assert c.get("/funding/plan").status_code == 200 and c.get("/notifications").status_code == 200
    assert c.patch(f"/employers/{eid}/schedule", json={"funding_lead_days": 1}).status_code == 403
    assert c.post(f"/employers/{eid}/funding-reminder").status_code == 403
    assert c.post("/funding/reminders/run").status_code == 403
    # an owner of ANOTHER organization sees none of it
    c.post("/auth/logout")
    signup = {"organization": {"name": "Other Agency", "country": "US", "base_currency": "USD"},
              "admin": {"full_name": "Other Owner", "email": "owner@other-agency.example.com", "password": "OtherPass2026"}}
    assert c.post("/auth/signup", json=signup).status_code == 201
    for call in (lambda: c.get(f"/employers/{eid}/funding-plan"),
                 lambda: c.patch(f"/employers/{eid}/schedule", json={"funding_lead_days": 1}),
                 lambda: c.get(f"/employers/{eid}/funding-statement"),
                 lambda: c.post(f"/employers/{eid}/funding-reminder")):
        assert call().status_code == 404
    assert c.get("/funding/plan").json()["plans"] == []
    assert c.get("/notifications").json()["total"] == 0
    assert c.get(f"/notifications?employer_id={eid}").json()["total"] == 0


def test_statement_json_and_csv(env):
    c, _, _, _ = env
    eid = next(p["employer_id"] for p in c.get("/funding/plan").json()["plans"] if p["employer_name"].startswith("Borealis"))
    st = c.get(f"/employers/{eid}/funding-statement").json()
    assert st["employer"].startswith("Borealis") and st["lines"][0]["name"] == "Ingrid Solberg"
    r = c.get(f"/employers/{eid}/funding-statement?format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and ".csv" in r.headers["content-disposition"]
    assert "Ingrid Solberg" in r.text and "Recommended transfer (USD)" in r.text and "Account number" in r.text
    assert c.get(f"/employers/{eid}/funding-statement?format=xml").status_code == 422


def test_csv_neutralises_formulas():
    st = {"employer": "=HYPERLINK(1)", "organization": "o", "generated_on": "2026-01-01", "pay_date": None,
          "fund_by": None, "lines": [{"name": "@bad", "currency": "USD", "method": "LOCAL", "amount": "1.00",
                                      "fee": "0.00", "amount_usd": "1.00", "fee_usd": "0.00"}],
          "needs_amount": [], "payroll_usd": "1", "run_fee_usd": "1", "reserve_floor_usd": "1",
          "required_in_wallet_usd": "1", "available_usd": "1", "gap_usd": "0", "recommended_transfer_usd": "0",
          "wire": None}
    out = funding.statement_csv(st)
    assert "'=HYPERLINK(1)" in out and "'@bad" in out


# --------------------------------------------------------------------------- reminders and outbox
def test_manual_reminder_and_outbox(env):
    c, _, _, _ = env
    plans = {p["employer_name"].split()[0]: p for p in c.get("/funding/plan").json()["plans"]}
    bor = plans["Borealis"]["employer_id"]
    r = c.post(f"/employers/{bor}/funding-reminder")
    assert r.status_code == 201, r.text
    n = r.json()
    assert n["status"] == "SENT" and n["provider"] == "console" and n["simulated"] is True
    assert n["recipient"] == "bo@borealis.example" and "USD" in n["body"] and "Send funds to" in n["body"]
    assert n["kind"] in ("FUNDING_REMINDER", "FUNDING_OVERDUE")
    # funded employer: no reminder, statement still fine
    acme = plans["Acme"]["employer_id"]
    assert c.post(f"/employers/{acme}/funding-reminder").status_code == 409
    assert c.post(f"/employers/{acme}/funding-reminder", json={"kind": "statement"}).json()["kind"] == "FUNDING_STATEMENT"
    lst = c.get("/notifications").json()
    assert lst["total"] == 2 and lst["items"][0]["employer_name"] in ("Acme Manufacturing GmbH",)
    assert c.get(f"/notifications?kind=FUNDING_STATEMENT").json()["total"] == 1
    assert c.get(f"/notifications?employer_id={bor}").json()["total"] == 1
    assert c.get("/notifications?status=FAILED").json()["total"] == 0
    assert c.get("/notifications?limit=1&offset=1").json()["items"][0]["employer_id"] == bor
    assert c.get("/notifications?kind=NOPE").status_code == 422


def test_reminder_needs_contact_email(env):
    c, _, db, _ = env
    bor = _emp(db, "Borealis")
    with db.sessionmaker() as s:
        s.get(Employer, bor).contact_email = None
        s.commit()
    r = c.post(f"/employers/{bor}/funding-reminder")
    assert r.status_code == 409 and "contact email" in r.json()["detail"]


def test_sweep_is_idempotent_and_explains(env):
    c, runtime, db, settings = env
    bor, cob = _emp(db, "Borealis"), _emp(db, "Cobalt")
    with db.sessionmaker() as s:
        b = s.get(Employer, bor)
        b.next_pay_date, b.pay_frequency, b.funding_lead_days = date(2026, 11, 4), "MONTHLY", 2  # fund_by Mon 2 Nov
        k = s.get(Employer, cob)
        k.next_pay_date, k.contact_email = date(2026, 11, 20), None
        s.commit()
        org = s.scalar(select(Organization))
        first = reminders.sweep_organization(s, runtime, settings, org, MON)
        second = reminders.sweep_organization(s, runtime, settings, org, MON)
        by = {r["employer_name"].split()[0]: r for r in first}
        assert by["Borealis"]["action"] == "queued" and by["Borealis"]["status"] == "SENT"
        assert by["Acme"]["action"] == "skipped" and "funded" in by["Acme"]["reason"]
        assert by["Cobalt"]["reason"].startswith("No contact email")
        assert by["Delta"]["action"] == "skipped"
        assert {r["action"] for r in second if r["employer_name"].startswith("Borealis")} == {"skipped"}
        assert [r["reason"] for r in second if r["employer_name"].startswith("Borealis")] == ["Already sent for this step."]
        rows = s.scalars(select(Notification).where(Notification.employer_id == bor)).all()
        assert len(rows) == 1 and rows[0].dedupe_key == f"funding:{bor}:2026-11-04:0"
        # next business day it is overdue -> one new daily email, then deduped again
        tue = date(2026, 11, 3)
        reminders.sweep_organization(s, runtime, settings, org, tue)
        reminders.sweep_organization(s, runtime, settings, org, tue)
        rows = s.scalars(select(Notification).where(Notification.employer_id == bor)).all()
        assert sorted(r.kind for r in rows) == ["FUNDING_OVERDUE", "FUNDING_REMINDER"]


def test_sweep_window_and_route(env):
    c, runtime, db, settings = env
    bor = _emp(db, "Borealis")
    with db.sessionmaker() as s:
        b = s.get(Employer, bor)
        b.next_pay_date = date(2026, 11, 30)
        s.commit()
        org = s.scalar(select(Organization))
        res = reminders.sweep_organization(s, runtime, settings, org, MON)
        assert [r for r in res if r["employer_name"].startswith("Borealis")][0]["reason"].startswith("Not yet")
    run1 = c.post("/funding/reminders/run").json()
    run2 = c.post("/funding/reminders/run").json()
    assert run1["queued"] + run1["skipped"] == 4
    assert run2["queued"] == 0 and run2["skipped"] == 4


def test_sweep_all_is_error_safe(env):
    _, runtime, db, settings = env
    n = reminders.sweep_all(db.sessionmaker, runtime, settings, MON)
    assert isinstance(n, int)
    runtime_broken = type("R", (), {"balances": staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError("x")))})()
    assert reminders.sweep_all(db.sessionmaker, runtime_broken, settings, MON) == 0  # logs, never raises


# --------------------------------------------------------------------------- notify providers
class FakeSmtp:
    sent: list = []
    fail: Exception | None = None

    def __init__(self, host, port, timeout=0):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        self.creds = (user, password)

    def send_message(self, msg):
        if FakeSmtp.fail:
            raise FakeSmtp.fail
        FakeSmtp.sent.append(msg)


def _row(db, org_id):
    with db.sessionmaker() as s:
        org = s.get(Organization, org_id)
        n = notify.queue_email(s, org, "GENERIC", "a@b.test", "Hi", "Body", dedupe_key="k1")
        s.commit()
        return n.id


def test_queue_email_is_idempotent_and_validates(env):
    _, _, db, _ = env
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        a = notify.queue_email(s, org, "GENERIC", "a@b.test", "Hi", "Body", dedupe_key="same")
        b = notify.queue_email(s, org, "GENERIC", "a@b.test", "Hi again", "Body", dedupe_key="same")
        c = notify.queue_email(s, org, "GENERIC", "a@b.test", "x", "y")
        d = notify.queue_email(s, org, "GENERIC", "a@b.test", "x", "y")
        assert a.id == b.id and c.id != d.id  # a missing key is never deduplicated
        with pytest.raises(ValueError):
            notify.queue_email(s, org, "BOGUS", "a@b.test", "x", "y")


def test_console_and_smtp_providers(env):
    _, _, db, settings = env
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        n = notify.queue_email(s, org, "GENERIC", "a@b.test", "Hi", "Body")
        notify.deliver(s, n, settings)
        assert (n.status, n.provider, n.sent_at is not None) == ("SENT", "console", True)
        smtp = notify.SmtpProvider("mail.test", 587, "user", "s3cret", "from@x.test", smtp_factory=FakeSmtp)
        n2 = notify.queue_email(s, org, "GENERIC", "a@b.test", "Hi2", "Body2")
        notify.deliver(s, n2, settings, smtp)
        assert n2.status == "SENT" and n2.provider == "smtp" and FakeSmtp.sent[-1]["To"] == "a@b.test"
        FakeSmtp.fail = __import__("smtplib").SMTPAuthenticationError(535, b"bad creds s3cret")
        n3 = notify.queue_email(s, org, "GENERIC", "a@b.test", "Hi3", "Body3")
        notify.deliver(s, n3, settings, smtp)
        assert n3.status == "FAILED" and "s3cret" not in (n3.error or "") and n3.sent_at is None
        FakeSmtp.fail = OSError("connection refused")
        n4 = notify.queue_email(s, org, "GENERIC", "a@b.test", "Hi4", "Body4")
        notify.deliver(s, n4, settings, smtp)
        assert n4.status == "FAILED" and n4.error
        FakeSmtp.fail = None
    unconfigured = Settings(database_url="sqlite://", jwt_secret="x" * 40, email_provider="smtp")
    with pytest.raises(notify.ProviderError):
        notify.provider_for(unconfigured)
    with db.sessionmaker() as s:
        n5 = notify.queue_email(s, s.scalar(select(Organization)), "GENERIC", "a@b.test", "s", "b")
        notify.deliver(s, n5, unconfigured)  # misconfiguration -> FAILED, not an exception
        assert n5.status == "FAILED" and "SMTP_HOST" in n5.error


# --------------------------------------------------------------------------- draft guard
PLAN = {"employer_name": "Acme GmbH", "contact_name": "Ana", "pay_date": "2026-11-20", "fund_by": "2026-11-18",
        "payroll_usd": "6500.00", "run_fee_usd": "149.00", "reserve_floor_usd": "1000.00",
        "required_in_wallet_usd": "7649.00", "available_usd": "5000.00", "gap_usd": "2649.00",
        "recommended_transfer_usd": "2700.00",
        "lines": [{"name": "Lena", "currency": "EUR", "amount": "3400.00"}],
        "wire": {"status": "READY", "account_name": "Acme Wallet", "account_number": "1234567890",
                 "swift_code": "CMFGUS33", "bank_name": "Some Bank", "reference": "Acme GmbH",
                 "routing": [{"type": "ach", "label": "ACH routing number", "value": "026073150"}]}}


class FakeLLM:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def invoke(self, messages):
        self.calls.append(messages)
        return type("R", (), {"content": self.fn(messages[-1][1])})()


def test_templates_are_exact():
    subject, body = draft.draft_funding_email("FUNDING_REMINDER", PLAN, "Vertex")
    assert "Wed 18 Nov 2026" in subject and "USD 2,700.00" in body and "1234567890" in body and "026073150" in body
    assert "EUR 3,400.00" in body and "everyone is paid in full" in body
    o_subject, o_body = draft.draft_funding_email("FUNDING_OVERDUE", PLAN, "Vertex")
    assert "was due" in o_subject and "USD 2,700.00" in o_body
    _, st = draft.draft_funding_email("FUNDING_STATEMENT", {**PLAN, "gap_usd": "0.00", "recommended_transfer_usd": "0.00"}, "V")
    assert "No transfer is needed" in st


def test_polish_guard_rejects_changed_numbers_accepts_rewording():
    _, body = draft.draft_funding_email("FUNDING_REMINDER", PLAN, "Vertex")
    bad = FakeLLM(lambda t: t.replace("USD 2,700.00", "USD 2,600.00"))
    assert draft.polish_body(body, PLAN, llm=bad) == (body, False)
    bad_acct = FakeLLM(lambda t: t.replace("1234567890", "1234567891"))
    assert draft.polish_body(body, PLAN, llm=bad_acct)[1] is False
    bad_date = FakeLLM(lambda t: t.replace("Wed 18 Nov 2026", "Thu 19 Nov 2026"))
    assert draft.polish_body(body, PLAN, llm=bad_date)[1] is False
    added = FakeLLM(lambda t: t + "\nWe also charge USD 99.00.")
    assert draft.polish_body(body, PLAN, llm=added)[1] is False
    dropped = FakeLLM(lambda t: "Please pay soon.")
    assert draft.polish_body(body, PLAN, llm=dropped)[1] is False
    ok = FakeLLM(lambda t: t.replace("Hello Ana,", "Hi Ana, hope you are well.").replace("Thank you,", "Many thanks,"))
    text, polished = draft.polish_body(body, PLAN, llm=ok)
    assert polished and "Hi Ana" in text and "USD 2,700.00" in text
    assert draft.polish_body(body, PLAN, llm=None) == (body, False)  # no key: template
    boom = FakeLLM(lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    assert draft.polish_body(body, PLAN, llm=boom) == (body, False)


# --------------------------------------------------------------------------- scheduler + chat helper + migration
def test_scheduler_starts_only_when_on(monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.services import reminders as rem

    started = []

    async def fake_loop(*a, **k):
        started.append(1)
        await asyncio.sleep(3600)

    monkeypatch.setattr("app.api.funding_reminder_loop", fake_loop)
    for flag, expected in (("on", 1), ("off", 0)):
        started.clear()
        s = Settings(database_url="sqlite://", jwt_secret="x" * 40, seed_demo=False, funding_reminders=flag)
        with TestClient(create_app(s, Database.from_url("sqlite://"))):
            pass
        assert len(started) == expected
    assert rem.SWEEP_INTERVAL_SECONDS == 1800


def test_get_funding_plan_helper_is_tenant_scoped(env):
    _, runtime, db, _ = env
    with db.sessionmaker() as s:
        org = s.scalar(select(Organization))
        assert len(funding.get_funding_plan(s, runtime, org.id)["plans"]) == 4
        eid = s.scalar(select(Employer.id))
        assert funding.get_funding_plan(s, runtime, org.id, eid)["employer_id"] == str(eid)
        with pytest.raises(LookupError):
            funding.get_funding_plan(s, runtime, uuid.uuid4(), eid)


def test_seed_gives_demo_schedules_without_overwriting(env):
    c, _, db, _ = env
    with db.sessionmaker() as s:
        emps = s.scalars(select(Employer)).all()
        assert all(e.pay_frequency and e.next_pay_date and e.contact_email for e in emps)


def test_migration_adds_funding_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE employers (
            id CHAR(32) PRIMARY KEY, organization_id CHAR(32) NOT NULL, name VARCHAR(160) NOT NULL,
            legal_name VARCHAR(200), country VARCHAR(2) NOT NULL, contact_name VARCHAR(120),
            contact_email VARCHAR(254), funding_currency VARCHAR(3) NOT NULL, airwallex_account_id VARCHAR(64),
            airwallex_global_account_id VARCHAR(64), status VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL);
        INSERT INTO employers VALUES ('a'||hex(randomblob(15)), 'b'||hex(randomblob(15)), 'Old Co', NULL, 'US',
            NULL, NULL, 'USD', NULL, NULL, 'ACTIVE', '2026-01-01 00:00:00');
    """)
    conn.commit()
    conn.close()
    db = Database.from_url(f"sqlite:///{path.as_posix()}")
    db.create_schema()
    cols = {c["name"]: c for c in inspect(db.engine).get_columns("employers")}
    assert {"pay_frequency", "next_pay_date", "funding_lead_days"} <= set(cols)
    assert "notifications" in inspect(db.engine).get_table_names()
    with db.engine.connect() as c:
        from sqlalchemy import text
        assert c.execute(text("SELECT funding_lead_days, pay_frequency FROM employers")).one() == (2, None)
    db.create_schema()  # idempotent
