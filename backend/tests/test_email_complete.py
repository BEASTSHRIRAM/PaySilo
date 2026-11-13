"""Complete transactional email coverage: house style, layout, OTP security, and the wired events."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_app
from app.config import Settings
from app.db import Database
from app.db.models import EmailOtp, Notification, User
from app.services import email_custom, email_style
from app.services import email_templates as tpl
from app.services.email_layout import tidy
from app.services.otp import OtpLimiter


# ------------------------------------------------------------------ style
@pytest.mark.parametrize("key", sorted(tpl.TEMPLATES))
def test_every_template_follows_the_house_style(key):
    spec = tpl.TEMPLATES[key]
    subject, text, html = spec.build(dict(spec.sample), "Vertex Payroll Services")
    assert email_style.subject_problems(subject) == []
    assert email_style.text_problems(text) == []
    assert "Thank you,\n" in text and text.startswith("Hello")
    assert html.startswith("<!doctype html>") and "<img" not in html and "<script" not in html
    assert "PaySilo" in html and "max-width:560px" in html
    if not key.startswith("funding") and key not in ("verify_email_otp", "password_reset_otp", "password_changed"):
        assert "uses PaySilo to pay contractors" in text
    for url in re.findall(r'href="([^"]+)"', html):
        assert url.startswith(("http://", "https://"))


def test_button_text_contrast_is_aa():
    def lum(hex_):
        rgb = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    a, b = lum("#EC6A52"), lum("#0C1D23")
    assert (max(a, b) + 0.05) / (min(a, b) + 0.05) >= 4.5


def test_style_checker_catches_violations():
    assert email_style.hyphen_problems("a sign-off - and more")
    assert not email_style.hyphen_problems("see https://x.example/a-b on 2026-11-13 ana-1@x.io")
    assert email_style.char_problems("em — dash!")
    assert email_style.subject_problems("URGENT payment")
    assert tidy("one-time — account-holder!") == "one time, account holder."


def test_draft_polish_guard_rejects_style_breaks():
    from app.crew import draft

    plan = tpl.TEMPLATES["funding_reminder"].sample["plan"]
    _, body = draft.draft_funding_email("FUNDING_REMINDER", plan, "Vertex")
    llm = type("L", (), {"invoke": lambda self, m: type("R", (), {"content": body + "\nSee you - soon!"})()})()
    assert draft.polish_body(body, plan, llm=llm) == (body, False)


def test_custom_message_is_escaped_capped_and_has_no_links():
    ctx = {"subject": "Hi {employer_name} http://evil.example", "employer_name": "<b>Acme</b>",
           "body": "Click https://evil.example now <script>alert(1)</script> {secret} {total}\n\n" + "x" * 900,
           "total": "USD 5.00", "link_url": "https://app.example/dashboard/runs/1"}
    subject, text, html = tpl.custom_message(ctx, "Vertex")
    assert "evil.example" not in text + html + subject
    assert "<script>" not in html and "<b>" not in html
    assert "{secret}" not in text and "USD 5.00" in text
    assert len(subject) <= 60 and text.count("x") <= email_custom.MAX_BODY
    assert "https://app.example/dashboard/runs/1" in html


# ------------------------------------------------------------------ OTP
@pytest.fixture
def vclient(monkeypatch):
    monkeypatch.setattr("app.services.notify.dispatch_async", lambda *a, **k: None)
    settings = Settings(database_url="sqlite://", jwt_secret="test-secret-" + "x" * 32, seed_demo=True,
                        email_verification="on", brevo_smtp_user="u", brevo_smtp_key="k", email_from="a@b.example")
    app = create_app(settings, Database.from_url("sqlite://"))
    with TestClient(app) as c:
        yield c


SIGNUP = {"organization": {"name": "Otp Org", "country": "US", "base_currency": "USD"},
          "admin": {"full_name": "Pat Doe", "email": "pat@otp.example", "password": "Passw0rdOk1"}}


def _db(client):
    return client.app.state.db.sessionmaker()


def _code(client, email, kind="EMAIL_OTP"):
    with _db(client) as s:
        n = s.scalars(select(Notification).where(Notification.recipient == email, Notification.kind == kind)
                      .order_by(Notification.created_at.desc())).first()
        return re.search(r"Your code: (\d{6})", n.secret["text"]).group(1)


def _signup(client, body=SIGNUP):
    r = client.post("/auth/signup", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_on_without_a_mailer_fails_closed():
    with pytest.raises(RuntimeError, match="EMAIL_VERIFICATION=on"):
        create_app(Settings(database_url="sqlite://", jwt_secret="x" * 40, email_verification="on"),
                   Database.from_url("sqlite://"))


def test_auto_without_a_mailer_keeps_signup_as_today(client):
    r = client.post("/auth/signup", json=SIGNUP)
    assert r.status_code == 201 and "verification_required" not in r.json() and r.json()["user"]["email"]
    assert client.get("/auth/me").status_code == 200
    with client.app.state.db.sessionmaker() as s:
        assert s.scalar(select(User).where(User.email == "pat@otp.example")).email_verified_at is not None
        assert s.scalars(select(EmailOtp)).all() == []


def test_signup_requires_a_code_and_no_session_until_verified(vclient):
    data = _signup(vclient)
    assert data["verification_required"] is True and data["email"] == "pat@otp.example"
    assert vclient.get("/auth/me").status_code == 401
    r = vclient.post("/auth/login", json={"email": "pat@otp.example", "password": "Passw0rdOk1"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "EMAIL_NOT_VERIFIED"
    code = _code(vclient, "pat@otp.example")
    ok = vclient.post("/auth/verify-email", json={"email": "pat@otp.example", "code": code})
    assert ok.status_code == 200 and vclient.get("/auth/me").status_code == 200
    again = vclient.post("/auth/verify-email", json={"email": "pat@otp.example", "code": code})
    assert again.status_code == 400  # a code works once
    with _db(vclient) as s:
        welcomes = s.scalars(select(Notification).where(Notification.kind == "ACCOUNT_WELCOME")).all()
        assert len(welcomes) == 1 and welcomes[0].recipient == "pat@otp.example"


def test_code_is_stored_as_hash_and_hidden_everywhere(vclient):
    _signup(vclient)
    code = _code(vclient, "pat@otp.example")
    with _db(vclient) as s:
        row = s.scalars(select(EmailOtp)).one()
        assert code not in row.code_hash and len(row.code_hash) == 64
        note = s.scalars(select(Notification).where(Notification.kind == "EMAIL_OTP")).one()
        assert note.sensitive and code not in note.body and note.body_html is None
    vclient.post("/auth/verify-email", json={"email": "pat@otp.example", "code": code})
    listed = vclient.get("/notifications").text
    assert code not in listed


def test_wrong_attempts_lock_the_code_and_expiry_is_enforced(vclient):
    _signup(vclient)
    code = _code(vclient, "pat@otp.example")
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(4):
        r = vclient.post("/auth/verify-email", json={"email": "pat@otp.example", "code": wrong})
        assert r.json()["detail"]["code"] == "OTP_INVALID"
    r = vclient.post("/auth/verify-email", json={"email": "pat@otp.example", "code": wrong})
    assert r.json()["detail"]["code"] == "OTP_LOCKED"
    r = vclient.post("/auth/verify-email", json={"email": "pat@otp.example", "code": code})
    assert r.json()["detail"]["code"] == "OTP_LOCKED"  # even the right code is locked
    with _db(vclient) as s:
        row = s.scalars(select(EmailOtp)).one()
        row.attempts, row.expires_at = 0, datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    r = vclient.post("/auth/verify-email", json={"email": "pat@otp.example", "code": code})
    assert r.json()["detail"]["code"] == "OTP_EXPIRED"


def test_resend_cooldown_and_hourly_limit():
    ticks = [1000.0]
    limiter = OtpLimiter(clock=lambda: ticks[0])
    limiter.check_and_record_send("a@x.io", "1.1.1.1", "VERIFY_EMAIL")
    ticks[0] += 10
    from app.services.otp import OtpError
    with pytest.raises(OtpError) as e:
        limiter.check_and_record_send("a@x.io", "1.1.1.1", "VERIFY_EMAIL")
    assert e.value.code == "RESEND_COOLDOWN"
    for _ in range(4):
        ticks[0] += 61
        limiter.check_and_record_send("a@x.io", "1.1.1.1", "VERIFY_EMAIL")
    ticks[0] += 61
    with pytest.raises(OtpError) as e:
        limiter.check_and_record_send("a@x.io", "1.1.1.1", "VERIFY_EMAIL")
    assert e.value.code == "RATE_LIMITED"
    other = OtpLimiter(clock=lambda: ticks[0])  # per client address limit across different emails
    for i in range(5):
        other.check_and_record_send(f"u{i}@x.io", "9.9.9.9", "VERIFY_EMAIL")
    with pytest.raises(OtpError):
        other.check_and_record_send("new@x.io", "9.9.9.9", "VERIFY_EMAIL")


def test_resend_endpoint_is_uniform_and_cooled_down(vclient):
    _signup(vclient)
    first = vclient.post("/auth/resend-otp", json={"email": "pat@otp.example"})
    assert first.status_code == 429 and first.json()["detail"]["code"] == "RESEND_COOLDOWN"  # signup just sent one
    unknown = vclient.post("/auth/resend-otp", json={"email": "ghost@otp.example"})
    assert unknown.status_code == 200 and unknown.json()["sent"] is True


def test_password_reset_flow_is_enumeration_safe(vclient):
    _signup(vclient)
    vclient.post("/auth/verify-email", json={"email": "pat@otp.example", "code": _code(vclient, "pat@otp.example")})
    vclient.post("/auth/logout")
    known = vclient.post("/auth/forgot-password", json={"email": "pat@otp.example"})
    unknown = vclient.post("/auth/forgot-password", json={"email": "ghost@otp.example"})
    assert known.status_code == unknown.status_code == 200 and known.json() == unknown.json()
    with _db(vclient) as s:
        assert s.scalars(select(Notification).where(Notification.recipient == "ghost@otp.example")).all() == []
    code = _code(vclient, "pat@otp.example")
    weak = vclient.post("/auth/reset-password", json={"email": "pat@otp.example", "code": code, "new_password": "short"})
    assert weak.status_code == 422
    ok = vclient.post("/auth/reset-password", json={"email": "pat@otp.example", "code": code,
                                                     "new_password": "BrandNew2Pass"})
    assert ok.status_code == 200
    assert vclient.post("/auth/login", json={"email": "pat@otp.example", "password": "Passw0rdOk1"}).status_code == 401
    assert vclient.post("/auth/login", json={"email": "pat@otp.example", "password": "BrandNew2Pass"}).status_code == 200
    with _db(vclient) as s:
        changed = s.scalars(select(Notification).where(Notification.kind == "PASSWORD_CHANGED")).all()
        assert len(changed) == 1 and changed[0].recipient == "pat@otp.example"


def test_existing_users_are_unaffected_by_verification(vclient):
    r = vclient.post("/auth/login", json={"email": "demo@paysilo.test", "password": "SiloDemo2026"})
    assert r.status_code == 200


def test_migration_marks_existing_users_verified():
    from sqlalchemy import create_engine, text

    from app.db.migrate import migrate

    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id CHAR(32) PRIMARY KEY, email VARCHAR(254), full_name VARCHAR(120), "
                       "password_hash VARCHAR(100), is_active BOOLEAN, last_login_at DATETIME, created_at DATETIME)"))
        c.execute(text("INSERT INTO users VALUES ('a','old@x.io','Old','h',1,NULL,'2026-01-01 00:00:00')"))
    migrate(eng)
    with eng.begin() as c:
        assert c.execute(text("SELECT email_verified_at FROM users")).scalar() is not None


# ------------------------------------------------------------------ wired events
def _mails(client, kind, to=None):
    with client.app.state.db.sessionmaker() as s:
        q = select(Notification).where(Notification.kind == kind)
        if to:
            q = q.where(Notification.recipient == to)
        return list(s.scalars(q))


def _employer(client, name="Mail Co", contact="ops@mailco.example"):
    r = client.post("/employers", json={"name": name, "contact_name": "Ops", "contact_email": contact,
                                                "country": "DE", "funding_currency": "USD"})
    assert r.status_code == 201, r.text
    return r.json()


def test_employer_welcome_exactly_once(demo_client):
    _employer(demo_client)
    assert len(_mails(demo_client, "EMPLOYER_WELCOME", "ops@mailco.example")) == 1


def test_team_events_mail_the_right_person(demo_client):
    inv = demo_client.post("/org/invitations", json={"email": "sam@mail.example", "role": "viewer"}).json()
    token = inv["invite_path"].rsplit("/", 1)[1]
    assert len(_mails(demo_client, "INVITE", "sam@mail.example")) == 1
    assert _mails(demo_client, "INVITE")[0].sensitive and token not in _mails(demo_client, "INVITE")[0].body
    demo_client.post("/auth/logout")
    r = demo_client.post(f"/invitations/{token}/accept", json={"full_name": "Sam Okafor", "password": "Passw0rdOk1"})
    assert r.status_code == 200, r.text
    got = _mails(demo_client, "INVITE_ACCEPTED")
    assert len(got) == 1 and got[0].recipient == "demo@paysilo.test" and "Sam Okafor" in got[0].body
    demo_client.post("/auth/logout")
    demo_client.post("/auth/login", json={"email": "demo@paysilo.test", "password": "SiloDemo2026"})
    sam = next(m for m in demo_client.get("/org/members").json() if m["email"] == "sam@mail.example")
    assert demo_client.patch(f"/org/members/{sam['id']}", json={"role": "approver"}).status_code == 200
    changed = _mails(demo_client, "ROLE_CHANGED")
    assert len(changed) == 1 and changed[0].recipient == "sam@mail.example" and "approver" in changed[0].body
    assert demo_client.delete(f"/org/members/{sam['id']}").status_code == 204
    removed = _mails(demo_client, "MEMBER_REMOVED")
    assert len(removed) == 1 and removed[0].recipient == "sam@mail.example"


def _prepare_run(client, name="Acme"):
    emp = next(e for e in client.get("/employers").json() if e["name"].startswith(name))
    detail = client.get(f"/employers/{emp['id']}").json()
    r = client.post("/runs", json={"employer_id": emp["id"],
                                   "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]})
    assert r.status_code == 201, r.text
    return r.json()


def test_partial_run_mails_who_is_held_and_what_to_fund(demo_client):
    run = _prepare_run(demo_client, "Delta")
    assert run["status"] == "RESERVED"
    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "PARTIALLY_PAID"
    partial = _mails(demo_client, "RUN_PARTIAL")
    assert len(partial) == 1 and partial[0].recipient == "demo@paysilo.test"
    assert "Still needed" in partial[0].body and "held" in partial[0].body
    from app.services import payroll_mail  # a second announce never duplicates
    with demo_client.app.state.db.sessionmaker() as s:
        from app.db.models import Organization, PayrollRun
        org = s.scalar(select(Organization).where(Organization.is_demo.is_(True)))
        r = s.get(PayrollRun, __import__("uuid").UUID(run["id"]))
        assert payroll_mail.announce_run(s, demo_client.app.state.settings, org, r, None) == 0


def test_prepared_run_emails_each_approver_once_and_decision_mails_follow(demo_client):
    run = _prepare_run(demo_client)
    assert run["status"] == "RESERVED"
    asks = _mails(demo_client, "APPROVAL_REQUESTED")
    assert len(asks) == 1 and asks[0].recipient == "demo@paysilo.test"
    assert f"/dashboard/runs/{run['id']}" in asks[0].body and "Nothing is paid until" in asks[0].body
    r = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "hold"})
    assert r.status_code == 200
    rejected = _mails(demo_client, "RUN_REJECTED")
    assert len(rejected) == 1 and rejected[0].recipient == "demo@paysilo.test"
    assert len(_mails(demo_client, "APPROVAL_REQUESTED")) == 1  # no duplicate


def test_settled_run_mails_approvers_and_sensitive_contractor_notices(demo_client):
    run = _prepare_run(demo_client)
    assert run["status"] == "RESERVED"
    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] in ("SETTLED", "PARTIALLY_PAID")
    if done["status"] == "SETTLED":
        assert len(_mails(demo_client, "RUN_SETTLED")) == 1
    for n in _mails(demo_client, "PAYSLIP_RECEIPT"):
        assert n.sensitive and "token=" not in n.body and "token=" in n.secret["text"]
        assert "Download proof PDF" in n.secret["text"]


def test_approval_reminders_cap_at_three(demo_client):
    from app.services import payroll_mail
    from app.db.models import Organization, PayrollRun

    run = _prepare_run(demo_client)
    assert run["status"] == "RESERVED"
    settings = demo_client.app.state.settings
    with demo_client.app.state.db.sessionmaker() as s:
        org = s.scalar(select(Organization).where(Organization.is_demo.is_(True)))
        later = datetime.now(UTC) + timedelta(hours=settings.approval_reminder_hours * 10)
        assert payroll_mail.remind_waiting_approvals(s, settings, org, later) == 1
        assert payroll_mail.remind_waiting_approvals(s, settings, org, later) == 0  # deduped
        s.commit()
        again = datetime.now(UTC) + timedelta(hours=settings.approval_reminder_hours * 20)
        assert payroll_mail.remind_waiting_approvals(s, settings, org, again) == 0  # still reminder number 3
    rem = _mails(demo_client, "APPROVAL_REMINDER")
    assert len(rem) == 1 and "3 of 3" in rem[0].body


def test_template_preview_and_test_send(demo_client, monkeypatch):
    items = demo_client.get("/notifications/templates").json()
    assert items["can_send_test"] is False and len(items["items"]) == len(tpl.TEMPLATES)
    assert all(i["subject"] and i["html"] and i["text"] for i in items["items"])
    r = demo_client.post("/notifications/templates/run_settled/test")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "EMAIL_NOT_CONFIGURED"
    assert demo_client.post("/notifications/templates/nope/test").status_code == 404


def test_test_send_goes_only_to_the_signed_in_user_and_is_rate_limited(vclient):
    vclient.post("/auth/login", json={"email": "demo@paysilo.test", "password": "SiloDemo2026"})
    from app.api.routes import notifications as nr

    nr._test_sends.clear()
    for _ in range(nr.TEST_LIMIT):
        r = vclient.post("/notifications/templates/payslip_receipt/test")
        assert r.status_code == 200 and r.json()["recipient"] == "demo@paysilo.test"
    assert vclient.post("/notifications/templates/payslip_receipt/test").status_code == 429
    rows = _mails(vclient, "PAYSLIP_RECEIPT")
    assert {r.recipient for r in rows} == {"demo@paysilo.test"} and all(r.sensitive for r in rows)


def test_purchase_and_workflow_mails_are_registered_and_tenant_scoped(demo_client):
    from app.services import spend_mail

    assert "custom_message" in spend_mail.TEMPLATE_NAMES
    assert set(tpl.SPEND_TEMPLATES) >= {"approval_requested", "payslip_receipt", "custom_message"}
