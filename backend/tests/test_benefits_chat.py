"""Silo Benefits in chat: benefit-aware, but with NO power to buy.

Covers the CHAT lane contract against the benefits service being built in
parallel (``app.services.benefits`` + ``BenefitBudget`` / ``BenefitRequest``
/ ``BenefitPurchase`` in ``app.db.models``):

- the deterministic fallback routes a plain-English benefit request to
  ``propose_benefit_purchase``, which only creates a REQUESTED
  BenefitRequest that awaits a human approver in the Benefits screen;
- the tool registry contains no tool that approves or purchases benefits
  (the architectural rule of this product: money moves only on human
  action in the UI/API);
- ``read_benefits`` reports the seeded budget honestly (Visa VIC,
  SIMULATED mode, US$200.00 per-transaction test-card limit);
- proposing a benefit for an unknown worker is refused and creates no row.

Seeded expectations (from the parallel benefits lane's demo seed): Acme
Manufacturing GmbH has a USD budget with a 500.00 monthly cap and 59.00
already spent this period from a seeded purchase, leaving 441.00.
Lena Fischer is an active Acme contractor (see ``app.services.seed``).

No LLM keys exist in the test environment, so chat turns here exercise
the deterministic fallback over the same tools the LLM would call.
"""

import json
from decimal import Decimal

ACME = "Acme Manufacturing GmbH"


def _events(response) -> list[tuple[str, dict]]:
    out = []
    event = None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: ") and event:
            out.append((event, json.loads(line[len("data: "):])))
            event = None
    return out


def _chat(c, message: str, **kwargs) -> list[tuple[str, dict]]:
    r = c.post("/chat", json={"message": message, **kwargs})
    assert r.status_code == 200, r.text
    return _events(r)


def _text(events) -> str:
    return "".join(d.get("text", "") for kind, d in events if kind == "token")


def _benefit_requests(demo_client, **filters):
    """All BenefitRequest rows (optionally filtered by column equality)."""
    from app.db.models import BenefitRequest

    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        q = s.query(BenefitRequest)
        for column, value in filters.items():
            q = q.filter(getattr(BenefitRequest, column) == value)
        rows = q.all()
        for row in rows:
            s.expunge(row)
        return rows


# (a) Fallback request -> REQUESTED row, reply awaits human approval ------

def test_fallback_benefit_request_creates_requested_row_awaiting_human(demo_client):
    events = _chat(
        demo_client,
        "Request a benefit for Lena Fischer: a 149 USD Python course on Coursera")
    text = _text(events)

    # Routed through the same tool path the LLM uses.
    assert any(kind == "tool_start" and d["tool"] == "propose_benefit_purchase"
               for kind, d in events)
    assert any(kind == "tool_result" and d["tool"] == "propose_benefit_purchase"
               and d["data"].get("created") is True for kind, d in events)

    # The reply is explicit: created, awaiting a human, nothing purchased.
    assert "awaiting human approval" in text
    assert "Benefits screen" in text
    assert "Nothing has been purchased" in text

    # Exactly the requested row exists, in REQUESTED — not approved/bought.
    rows = [r for r in _benefit_requests(demo_client, merchant="Coursera")
            if "Python course" in (r.item or "")]
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "REQUESTED"
    assert row.contractor_name == "Lena Fischer"
    assert Decimal(str(row.amount)) == Decimal("149.00")
    assert row.currency == "USD"
    assert row.requested_by_email  # stamped from the chat session's user

    from app.db.models import Employer

    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        employer = s.get(Employer, row.employer_id)
        assert employer is not None and employer.name == ACME


# (b) The registry has NO benefit approve/purchase tool --------------------

def test_tool_registry_has_no_benefit_approve_or_purchase_tool(demo_client):
    from app.chat import tools as chat_tools

    benefit_tools = {name for name in chat_tools.TOOL_FUNCTIONS if "benefit" in name}
    assert benefit_tools == {"read_benefits", "propose_benefit_purchase"}

    forbidden = {
        "approve_benefit", "approve_benefits", "approve_benefit_request",
        "approve_benefit_purchase", "purchase_benefit", "buy_benefit",
        "purchase_benefit_request", "buy_benefit_request",
        "execute_benefit_purchase", "deny_benefit", "deny_benefit_request",
    }
    assert not (forbidden & set(chat_tools.TOOL_FUNCTIONS))
    # No registered tool name couples approving/buying with benefits at all.
    assert not any(
        "benefit" in name and any(verb in name for verb in ("approve", "buy", "purchase", "execute"))
        and name != "propose_benefit_purchase"
        for name in chat_tools.TOOL_FUNCTIONS)
    # The propose tool's own LLM description must not blur the line either.
    description = chat_tools.TOOL_DESCRIPTIONS["propose_benefit_purchase"]
    assert "does NOT approve" in description and "does NOT purchase" in description


# (c) read_benefits returns the seeded budget honestly ---------------------

def test_read_benefits_returns_seeded_acme_budget(demo_client):
    events = _chat(demo_client, "What is the benefits budget for Acme?")
    text = _text(events)

    assert any(kind == "tool_start" and d["tool"] == "read_benefits" for kind, d in events)
    assert ACME in text
    assert "500.00" in text  # monthly cap, from the seeded budget
    assert "59.00" in text   # spent this period, from the seeded purchase
    assert "441.00" in text  # remaining = cap - spent
    # Honest rails: Visa VIC, SIMULATED in the test runtime, $200 test-card cap.
    assert "Visa VIC" in text and "SIMULATED" in text
    assert "US$200.00" in text

    result = next(d for kind, d in events if kind == "tool_result" and d["tool"] == "read_benefits")
    acme = next(b for b in result["data"]["benefits"] if b["employer_name"] == ACME)
    budget = next(b for b in acme["budgets"] if b["currency"] == "USD")
    assert budget["monthly_cap"] == "500.00"
    assert budget["spent_this_period"] == "59.00"
    assert budget["remaining"] == "441.00"
    assert budget["enabled"] is True


def test_benefit_status_phrasing_also_reaches_read_benefits(demo_client):
    events = _chat(demo_client, "benefit status")
    assert any(kind == "tool_start" and d["tool"] == "read_benefits" for kind, d in events)
    assert "Visa VIC" in _text(events)


# (d) Unknown worker: refused, no row created -------------------------------

def test_propose_benefit_for_unknown_worker_is_refused_without_a_row(demo_client):
    before = len(_benefit_requests(demo_client))

    events = _chat(
        demo_client,
        "Request a benefit for Zara Nobody: a 149 USD Python course on Coursera")
    text = _text(events)

    assert any(kind == "tool_start" and d["tool"] == "propose_benefit_purchase"
               for kind, d in events)
    assert "cannot find a worker named 'Zara Nobody'" in text
    assert "Nothing has been purchased" in text
    # The employer's real workers are listed to help the user correct it.
    assert "Lena Fischer" in text

    assert len(_benefit_requests(demo_client)) == before
    assert _benefit_requests(demo_client, contractor_name="Zara Nobody") == []
