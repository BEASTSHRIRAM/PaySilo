"""Platform email (outbox lane) — recorded-first, never fake-sent.

Covered here:

- enqueue with no SMTP config records a ``NOT_CONFIGURED`` row whose
  rendered subject/body carry the context snapshot figures verbatim
  (amounts are never recomputed: the approval total deliberately
  differs from the sum of its lines and the total string wins);
- enqueue with SMTP configured records ``QUEUED``;
- drain with a fake transport sends once and only once (idempotent —
  the status flip is the lock), flips the row to ``SENT`` + ``sent_at``;
- drain with a failing transport records ``FAILED`` + ``error_detail``
  and a second drain does NOT retry it (FAILED is terminal);
- drain while unconfigured changes nothing and reports the skip;
- trip itineraries word a BLOCKED component honestly and show a
  purchased component with its provider reference;
- the API scopes the outbox to the caller's organization and the drain
  endpoint is owner/admin only.
"""

from __future__ import annotations

import uuid

from app.config import Settings
from app.db.models import EmailOutbox, Organization
from app.services import email as email_service
from app.services.email import drain_outbox, email_configured, enqueue_email
from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unconfigured() -> Settings:
    return Settings(smtp_host=None, smtp_user=None, smtp_password=None, email_from=None)


def _configured() -> Settings:
    return Settings(smtp_host="smtp.example.test", smtp_port=587, smtp_user=None,
                    smtp_password=None, email_from="no-reply@paysilo.test")


def _approval_context() -> dict:
    # The total deliberately does NOT equal the sum of the lines
    # (10.00 + 20.00 = 30.00): templates must use the snapshot total
    # verbatim and never recompute it.
    return {
        "entity_label": "Onboarding kit for Lena Fischer",
        "employer_name": "Acme Manufacturing GmbH",
        "requested_for": "Lena Fischer",
        "total": "999.00",
        "currency": "USD",
        "lines": [{"label": "Laptop", "amount": "10.00"},
                  {"label": "Monitor", "amount": "20.00"}],
        "link_path": "/dashboard/onboarding",
        "expires_at": "2026-11-19T12:00:00Z",
    }


def _org_id(client) -> uuid.UUID:
    return uuid.UUID(client.get("/auth/me").json()["organization"]["id"])


def _session(client):
    return client.app.state.db.sessionmaker()


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, *, to, subject, body_text, body_html) -> None:
        self.calls.append({"to": to, "subject": subject,
                           "body_text": body_text, "body_html": body_html})


class FailingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, *, to, subject, body_text, body_html) -> None:
        self.calls += 1
        raise RuntimeError("smtp exploded")


def _enqueue_approval(session, settings, org_id, **overrides):
    kwargs = dict(
        organization_id=org_id, template="approval_requested",
        to="decider@example.com", context=_approval_context(),
        related_kind="kit", related_id=uuid.uuid4(),
        related_label="Onboarding kit for Lena Fischer")
    kwargs.update(overrides)
    return enqueue_email(session, settings, **kwargs)


# ---------------------------------------------------------------------------
# Enqueue: NOT_CONFIGURED vs QUEUED, snapshot figures verbatim
# ---------------------------------------------------------------------------

def test_enqueue_not_configured_records_rendered_snapshot_verbatim(demo_client):
    org_id = _org_id(demo_client)
    with _session(demo_client) as s:
        row = _enqueue_approval(s, _unconfigured(), org_id)
        s.commit()
        assert row.status == "NOT_CONFIGURED"
        assert row.sent_at is None
        assert row.error_detail is None
        assert row.to_address == "decider@example.com"
        assert row.template == "approval_requested"
        assert row.related_kind == "kit"
        assert row.related_label == "Onboarding kit for Lena Fischer"
        # Snapshot figures verbatim: the context total (999.00) is used
        # exactly as given, in subject and body — never recomputed from
        # the lines (whose sum, 30.00, must appear nowhere).
        assert "999.00" in row.subject
        assert "USD 999.00" in row.body_text
        assert "USD 999.00" in row.body_html
        assert "30.00" not in row.body_text
        assert "10.00" in row.body_text and "20.00" in row.body_text
        assert "/dashboard/onboarding" in row.body_text
        assert "2026-11-19T12:00:00Z" in row.body_text
        assert "#244855" in row.body_html  # branded layout, no external assets


def test_enqueue_configured_is_queued(demo_client):
    assert email_configured(_configured()) is True
    assert email_configured(_unconfigured()) is False
    org_id = _org_id(demo_client)
    with _session(demo_client) as s:
        row = _enqueue_approval(s, _configured(), org_id)
        s.commit()
        assert row.status == "QUEUED"
        assert row.sent_at is None


# ---------------------------------------------------------------------------
# Drain: sends once, failures are terminal, unconfigured is a no-op
# ---------------------------------------------------------------------------

def test_drain_with_fake_transport_sends_once_idempotent(demo_client):
    org_id = _org_id(demo_client)
    fake = FakeTransport()
    with _session(demo_client) as s:
        row = _enqueue_approval(s, _configured(), org_id)
        s.commit()
        row_id = row.id

        result = drain_outbox(s, _configured(), transport=fake)
        assert result == {"sent": 1, "failed": 0, "processed": 1}
        assert len(fake.calls) == 1
        assert fake.calls[0]["to"] == "decider@example.com"
        assert "999.00" in fake.calls[0]["subject"]

        s.refresh(row)
        assert row.status == "SENT"
        assert row.sent_at is not None

        # Second drain: nothing QUEUED remains — nothing is sent again.
        result2 = drain_outbox(s, _configured(), transport=fake)
        assert result2 == {"sent": 0, "failed": 0, "processed": 0}
        assert len(fake.calls) == 1
        assert s.get(EmailOutbox, row_id).status == "SENT"


def test_drain_with_failing_transport_failed_and_not_retried(demo_client):
    org_id = _org_id(demo_client)
    failing = FailingTransport()
    with _session(demo_client) as s:
        row = _enqueue_approval(s, _configured(), org_id)
        s.commit()

        result = drain_outbox(s, _configured(), transport=failing)
        assert result == {"sent": 0, "failed": 1, "processed": 1}
        s.refresh(row)
        assert row.status == "FAILED"
        assert row.error_detail and "smtp exploded" in row.error_detail
        assert row.sent_at is None

        # FAILED is terminal: a later drain must not retry this row.
        result2 = drain_outbox(s, _configured(), transport=failing)
        assert result2 == {"sent": 0, "failed": 0, "processed": 0}
        assert failing.calls == 1
        s.refresh(row)
        assert row.status == "FAILED"


def test_drain_unconfigured_changes_nothing(demo_client):
    org_id = _org_id(demo_client)
    fake = FakeTransport()
    with _session(demo_client) as s:
        queued = _enqueue_approval(s, _configured(), org_id)
        recorded = _enqueue_approval(s, _unconfigured(), org_id,
                                     to="other@example.com")
        s.commit()
        queued_id, recorded_id = queued.id, recorded.id

        result = drain_outbox(s, _unconfigured(), transport=fake)
        assert result == {"skipped": "not_configured"}
        assert fake.calls == []
        assert s.get(EmailOutbox, queued_id).status == "QUEUED"
        assert s.get(EmailOutbox, recorded_id).status == "NOT_CONFIGURED"


# ---------------------------------------------------------------------------
# Templates: trip honesty + the other templates' exact figures
# ---------------------------------------------------------------------------

def test_trip_itinerary_blocked_component_worded_honestly(demo_client):
    org_id = _org_id(demo_client)
    context = {
        "contractor_name": "Lena Fischer",
        "destination": "Berlin",
        "start_date": "2026-12-01",
        "end_date": "2026-12-05",
        "components": [
            {"label": "Flight to Berlin", "kind": "flight", "amount": "320.00",
             "currency": "USD", "status": "PURCHASED", "provider_ref": "AIR-REF-123"},
            {"label": "Hotel Aurora", "kind": "hotel", "amount": "540.00",
             "currency": "USD", "status": "BLOCKED", "provider_ref": None},
        ],
    }
    with _session(demo_client) as s:
        row = enqueue_email(s, _unconfigured(), organization_id=org_id,
                            template="trip_itinerary", to="lena@example.com",
                            context=context, related_kind="trip",
                            related_id=uuid.uuid4(), related_label="Trip to Berlin")
        s.commit()
        honest = ("Not yet booked — exceeds the current credential limit; "
                  "the team will arrange this separately")
        assert honest in row.body_text
        assert honest in row.body_html

        lines = row.body_text.splitlines()
        flight_line = next(line for line in lines if "Flight to Berlin" in line)
        hotel_line = next(line for line in lines if "Hotel Aurora" in line)
        # Purchased component: shown as booked, with its provider ref.
        assert "Booked" in flight_line and "AIR-REF-123" in flight_line
        # Blocked component: honest wording only — never presented as a
        # booking and with no provider reference.
        assert hotel_line.endswith(honest)
        assert "— Booked" not in hotel_line
        assert "Provider reference" not in hotel_line


def test_kit_receipt_and_held_templates_use_exact_figures(demo_client):
    org_id = _org_id(demo_client)
    with _session(demo_client) as s:
        kit = enqueue_email(
            s, _unconfigured(), organization_id=org_id, template="kit_completed",
            to="lena@example.com",
            context={"contractor_name": "Lena Fischer",
                     "employer_name": "Acme Manufacturing GmbH",
                     "items": [{"label": "Laptop", "amount": "1200.00", "currency": "USD",
                                "provider_ref": "vic-sim-001", "mode": "SIMULATED"}],
                     "total": "USD 1200.00"},
            related_kind="kit", related_id=uuid.uuid4(), related_label="Kit for Lena Fischer")
        receipt = enqueue_email(
            s, _unconfigured(), organization_id=org_id, template="purchase_receipt",
            to="ops@example.com",
            context={"buyer_label": "Acme Manufacturing GmbH", "item_label": "Conference pass",
                     "merchant": "ConfCo", "amount": "59.00", "currency": "USD",
                     "provider_ref": "vic-sim-002", "mode": "SIMULATED",
                     "book_label": "benefits book — Acme"},
            related_kind="benefit", related_id=uuid.uuid4(), related_label="Benefit purchase")
        held = enqueue_email(
            s, _unconfigured(), organization_id=org_id, template="payroll_held_alert",
            to="finance@acme.example",
            context={"employer_name": "Borealis Labs AB", "run_label": "November payroll",
                     "shortfall": "2642.88", "currency": "USD", "required_total": "7642.88"},
            related_kind="run", related_id=uuid.uuid4(), related_label="November payroll")
        s.commit()

        assert "USD 1200.00" in kit.body_text and "vic-sim-001" in kit.body_text
        assert "SIMULATED" in kit.body_text
        assert "USD 59.00" in receipt.body_text and "vic-sim-002" in receipt.body_text
        assert "benefits book — Acme" in receipt.body_text
        assert "USD 2642.88" in held.body_text and "USD 7642.88" in held.body_text
        assert "USD 2642.88" in held.subject
        assert "What happens next" in held.body_text


# ---------------------------------------------------------------------------
# SMTP transport (stdlib only, no network — smtplib is faked)
# ---------------------------------------------------------------------------

def test_smtp_transport_starttls_and_login(monkeypatch):
    instances: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host, self.port = host, port
            self.started_tls = False
            self.login_args = None
            self.message = None
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            self.started_tls = True

        def login(self, user, password):
            self.login_args = (user, password)

        def send_message(self, message):
            self.message = message

    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)
    settings = Settings(smtp_host="smtp.example.test", smtp_port=587,
                        smtp_user="mailer", smtp_password="test-password",
                        email_from="no-reply@paysilo.test")
    transport = email_service.SmtpTransport(settings)
    transport.send(to="lena@example.com", subject="Hello",
                   body_text="Plain body", body_html="<p>Plain body</p>")

    assert len(instances) == 1
    server = instances[0]
    assert (server.host, server.port) == ("smtp.example.test", 587)
    assert server.started_tls is True  # STARTTLS on a non-465 port
    assert server.login_args == ("mailer", "test-password")
    assert server.message["To"] == "lena@example.com"
    assert server.message["From"] == "no-reply@paysilo.test"
    assert server.message["Subject"] == "Hello"


# ---------------------------------------------------------------------------
# API: status, org scoping, drain role gate
# ---------------------------------------------------------------------------

def test_outbox_org_scoping(demo_client):
    org_id = _org_id(demo_client)
    with _session(demo_client) as s:
        other = Organization(name="Other Org", slug="other-org-email-test",
                             country="US", base_currency="USD")
        s.add(other)
        s.commit()
        other_id = other.id
        _enqueue_approval(s, _unconfigured(), org_id, related_label="Mine — kit approval")
        _enqueue_approval(s, _unconfigured(), other_id, related_label="Theirs — kit approval")
        s.commit()

    status = demo_client.get("/emails/status")
    assert status.status_code == 200, status.text
    assert status.json() == {"configured": False, "from_address": None}

    rows = demo_client.get("/emails/outbox")
    assert rows.status_code == 200, rows.text
    body = rows.json()
    labels = [r["related_label"] for r in body]
    assert "Mine — kit approval" in labels
    assert "Theirs — kit approval" not in labels
    assert all(r["organization_id"] == str(org_id) for r in body)
    mine = next(r for r in body if r["related_label"] == "Mine — kit approval")
    assert mine["status"] == "NOT_CONFIGURED"
    assert mine["to_address"] == "decider@example.com"
    assert "smtp" not in str(body).lower() or "password" not in str(body).lower()


def test_drain_route_owner_unconfigured_and_viewer_forbidden(demo_client):
    # Owner may drain; with no SMTP configured it is an honest no-op.
    r = demo_client.post("/emails/outbox/drain")
    assert r.status_code == 200, r.text
    assert r.json() == {"skipped": "not_configured"}

    # Invite a viewer (a non owner/admin role) and sign in as them.
    created = demo_client.post("/org/invitations",
                               json={"email": "email.viewer@example.com", "role": "viewer"})
    assert created.status_code == 201, created.text
    token = created.json()["invite_path"].split("/")[-1]
    accepted = demo_client.post(f"/invitations/{token}/accept",
                                json={"full_name": "Email Viewer", "password": "ViewerPass2026"})
    assert accepted.status_code == 200, accepted.text
    assert demo_client.get("/auth/me").json()["role"] == "viewer"

    # Viewers can read the outbox but can never drain it.
    assert demo_client.get("/emails/outbox").status_code == 200
    forbidden = demo_client.post("/emails/outbox/drain")
    assert forbidden.status_code == 403, forbidden.text

    demo_client.post("/auth/logout")
    assert demo_client.post("/auth/login",
                            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}).status_code == 200
