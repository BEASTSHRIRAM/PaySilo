"""VIC spend categories in chat: onboarding kits + travel (propose/read only).

Covers the CHAT lane contract against the BE services built in parallel
(``app.services.onboarding.create_kit`` / ``app.services.travel.create_trip``
+ ``OnboardingKit`` / ``KitItem`` / ``OnboardingBudget`` and ``TripRequest``
/ ``TripComponent`` / ``TravelBudget`` in ``app.db.models``):

- ``propose_onboarding_kit`` creates a DRAFT kit (source CHAT) with the
  standard items; an unknown contractor is refused with no row created;
- ``propose_trip`` creates a REQUESTED trip, and its reply states the
  per-transaction limit block for an over-limit component;
- ``read_onboarding`` / ``read_travel`` return created data and are
  org-scoped: another organization's employer name does not resolve;
- the registry contains no approve/purchase tool for kits or trips;
- the deterministic fallback routes kit/trip phrasings to the tools;
- pending questions are answered by the read tools from real state;
- ``read_workflow_runs`` is registered and degrades gracefully when the
  Workflows module is not installed.

Seeded demo names (``app.services.seed``): Acme Manufacturing GmbH with
active contractor Lena Fischer.

No LLM keys exist in the test environment, so chat turns here exercise
the deterministic fallback over the same tools the LLM would call. BE
model imports live inside helpers/tests so this module collects even
before the BE lane lands; the BE-dependent tests then fail on import in
an unmerged tree (they are run by the coordinator post-merge) rather
than being weakened or skipped.
"""

import json
from decimal import Decimal

ACME = "Acme Manufacturing GmbH"
LENA = "Lena Fischer"


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


def _rows(demo_client, model_name: str, **filters):
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


def _kit_items(demo_client, kit):
    """KitItem rows for a kit, whichever FK name the BE model uses."""
    from app.db.models import KitItem

    for fk in ("kit_id", "onboarding_kit_id"):
        if hasattr(KitItem, fk):
            return _rows(demo_client, "KitItem", **{fk: kit.id})
    rel = getattr(kit, "items", None)
    return list(rel) if rel is not None else []


def _trip_components(demo_client, trip):
    from app.db.models import TripComponent

    for fk in ("trip_id", "trip_request_id", "request_id"):
        if hasattr(TripComponent, fk):
            return _rows(demo_client, "TripComponent", **{fk: trip.id})
    rel = getattr(trip, "components", None)
    return list(rel) if rel is not None else []


# --- propose_onboarding_kit -------------------------------------------------

def test_propose_onboarding_kit_creates_draft_with_standard_items(demo_client):
    events = _chat(demo_client, f"Propose an onboarding kit for {LENA} at {ACME}")
    text = _text(events)

    assert any(kind == "tool_start" and d["tool"] == "propose_onboarding_kit"
               for kind, d in events)
    result = next(d for kind, d in events
                  if kind == "tool_result" and d["tool"] == "propose_onboarding_kit")
    assert result["data"]["created"] is True
    assert result["data"]["status"] == "DRAFT"
    assert result["data"]["total"] == "102.00"
    # The reply carries the law: whole-kit human approval, per-item purchase after.
    assert "approve the whole kit" in text
    assert "Onboarding screen" in text
    assert "cannot approve or buy" in text
    assert "Visa VIC" in text and "SIMULATED" in text

    # The demo seed carries its own DRAFT kit (manual source); the chat
    # propose must add exactly one more, marked as chat-sourced.
    kits = [k for k in _rows(demo_client, "OnboardingKit", status="DRAFT")
            if getattr(k, "source", None) == "CHAT"]
    assert len(kits) == 1
    kit = kits[0]
    if hasattr(kit, "source"):
        assert kit.source == "CHAT"
    items = _kit_items(demo_client, kit)
    assert len(items) == 3
    total = sum(Decimal(str(i.amount)) for i in items)
    assert total == Decimal("102.00")
    by_kind = {i.kind: i for i in items}
    assert Decimal(str(by_kind["BACKGROUND_CHECK"].amount)) == Decimal("40.00")
    assert by_kind["BACKGROUND_CHECK"].merchant == "checkr.com"
    assert Decimal(str(by_kind["SAAS_SEAT"].amount)) == Decimal("12.00")
    assert by_kind["SAAS_SEAT"].merchant == "github.com"
    assert Decimal(str(by_kind["GIFT_CARD"].amount)) == Decimal("50.00")
    assert by_kind["GIFT_CARD"].merchant == "amazon.com"

    from app.db.models import Employer

    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        employer = s.get(Employer, kit.employer_id)
        assert employer is not None and employer.name == ACME


def test_propose_onboarding_kit_custom_items_spec(demo_client):
    events = _chat(
        demo_client,
        f"Propose an onboarding kit for {LENA} at {ACME}: "
        "Background verification: $40; GitHub seat: $12; Welcome gift card: $50")
    result = next(d for kind, d in events
                  if kind == "tool_result" and d["tool"] == "propose_onboarding_kit")
    assert result["data"]["created"] is True
    assert result["data"]["total"] == "102.00"
    kinds = {i["kind"] for i in result["data"]["items"]}
    assert kinds == {"BACKGROUND_CHECK", "SAAS_SEAT", "GIFT_CARD"}


def test_propose_kit_for_unknown_contractor_refused_no_row(demo_client):
    before = len(_rows(demo_client, "OnboardingKit"))
    events = _chat(demo_client, f"Propose an onboarding kit for Zara Nobody at {ACME}")
    text = _text(events)
    assert any(kind == "tool_start" and d["tool"] == "propose_onboarding_kit"
               for kind, d in events)
    assert "cannot find a worker named 'Zara Nobody'" in text
    assert "Nothing has been purchased" in text
    assert len(_rows(demo_client, "OnboardingKit")) == before


# --- propose_trip -------------------------------------------------------------

def test_propose_trip_creates_requested_trip_and_states_limit_block(demo_client):
    events = _chat(
        demo_client,
        f"Plan a trip for {LENA} to Berlin with a $450 flight")
    text = _text(events)

    assert any(kind == "tool_start" and d["tool"] == "propose_trip" for kind, d in events)
    result = next(d for kind, d in events if kind == "tool_result" and d["tool"] == "propose_trip")
    assert result["data"]["created"] is True
    assert result["data"]["status"] == "REQUESTED"
    assert result["data"]["destination"] == "Berlin"
    # Honest limit note: over-limit components are execution-blocked even when approved.
    assert "per-transaction limit" in text
    assert "USD 200" in text and "SIMULATED" in text
    assert "execution-blocked even when approved" in text
    assert "Travel screen" in text

    trips = _rows(demo_client, "TripRequest", status="REQUESTED")
    assert len(trips) == 1
    trip = trips[0]
    assert trip.destination == "Berlin"
    components = _trip_components(demo_client, trip)
    transport = next(c for c in components if c.kind == "TRANSPORT")
    assert Decimal(str(transport.amount)) == Decimal("450.00")


def test_propose_trip_defaults_are_draft_figures(demo_client):
    events = _chat(demo_client, f"Create a trip for {LENA} to Berlin")
    text = _text(events)
    result = next(d for kind, d in events if kind == "tool_result" and d["tool"] == "propose_trip")
    assert result["data"]["created"] is True
    assert result["data"]["used_defaults"] is True
    assert result["data"]["total"] == "340.00"  # 60 + 180 + 100
    assert "draft figures to edit in the Travel screen" in text


# --- read tools, scoping, pending --------------------------------------------

def test_read_onboarding_and_travel_return_created_data(demo_client):
    _chat(demo_client, f"Propose an onboarding kit for {LENA} at {ACME}")
    _chat(demo_client, f"Plan a trip for {LENA} to Berlin with a $450 flight")

    events = _chat(demo_client, f"What is the onboarding status for {ACME}?")
    text = _text(events)
    assert any(kind == "tool_start" and d["tool"] == "read_onboarding" for kind, d in events)
    assert LENA in text and "DRAFT" in text and "102.00" in text
    assert "proposed in chat" in text  # the kit's source, shown factually
    result = next(d for kind, d in events if kind == "tool_result" and d["tool"] == "read_onboarding")
    acme = next(e for e in result["data"]["onboarding"] if e["employer_name"] == ACME)
    assert acme["kits"] and acme["kits"][0]["total"] == "102.00"

    events = _chat(demo_client, f"What is the travel status for {ACME}?")
    text = _text(events)
    assert any(kind == "tool_start" and d["tool"] == "read_travel" for kind, d in events)
    assert "Berlin" in text and "REQUESTED" in text and "450.00" in text
    result = next(d for kind, d in events if kind == "tool_result" and d["tool"] == "read_travel")
    acme = next(e for e in result["data"]["travel"] if e["employer_name"] == ACME)
    assert acme["trips"] and acme["trips"][0]["destination"] == "Berlin"


def test_pending_onboarding_question_answers_from_real_state(demo_client):
    _chat(demo_client, f"Propose an onboarding kit for {LENA} at {ACME}")
    events = _chat(demo_client, f"What's pending for {LENA}'s onboarding?")
    text = _text(events)
    assert any(kind == "tool_start" and d["tool"] == "read_onboarding" for kind, d in events)
    assert "Pending approval" in text
    assert LENA in text and "102.00" in text


def test_pending_trip_question_answers_from_real_state(demo_client):
    _chat(demo_client, f"Plan a trip for {LENA} to Berlin with a $450 flight")
    events = _chat(demo_client, "What's pending on the Berlin trip?")
    text = _text(events)
    assert any(kind == "tool_start" and d["tool"] == "read_travel" for kind, d in events)
    assert "Pending approval" in text and "Berlin" in text


def test_spend_reads_do_not_resolve_another_orgs_employer(demo_client):
    """Org isolation: an employer that exists only in ANOTHER org is invisible."""
    import uuid as uuid_mod

    from sqlalchemy import select

    from app.chat import tools as chat_tools
    from app.db.models import Employer, Membership, Organization, User

    db = demo_client.app.state.db
    runtime = demo_client.app.state.runtime
    with db.sessionmaker() as s:
        other_org = Organization(name="Other Org", slug="other-org-spend",
                                 country="GB", base_currency="USD")
        s.add(other_org)
        s.flush()
        s.add(Employer(organization_id=other_org.id, name="Northwind Only Ltd",
                       country="GB", status="ACTIVE"))
        s.commit()

        membership = s.scalars(select(Membership)).first()
        org = s.get(Organization, membership.organization_id)
        user = s.get(User, membership.user_id)
        ctx = chat_tools.ChatContext(
            db=s, runtime=runtime, organization_id=org.id, organization_name=org.name,
            user_id=user.id, user_email=user.email, role=membership.role, demo_mode=True)
        assert ctx.organization_id != other_org.id

        read_kits = chat_tools.read_onboarding(ctx, employer="Northwind Only Ltd")
        assert read_kits.data.get("found") is False
        read_trips = chat_tools.read_travel(ctx, employer="Northwind Only Ltd")
        assert read_trips.data.get("found") is False
        # And a proposal naming that employer is refused without creating a row.
        result = chat_tools.propose_onboarding_kit(
            ctx, employer="Northwind Only Ltd", contractor=LENA)
        assert result.data.get("created") is False
        assert uuid_mod.UUID(str(org.id))  # sanity: ctx is the demo org


# --- registry: NO approve/purchase tool for kits or trips ----------------------

def test_tool_registry_has_no_kit_or_trip_approve_or_purchase_tool(demo_client):
    from app.chat import tools as chat_tools

    spend_tools = {name for name in chat_tools.TOOL_FUNCTIONS
                   if any(w in name for w in ("onboarding", "kit", "trip", "travel"))}
    assert spend_tools == {"read_onboarding", "propose_onboarding_kit",
                           "read_travel", "propose_trip"}
    forbidden = {
        "approve_onboarding_kit", "approve_kit", "purchase_kit", "buy_kit",
        "purchase_kit_item", "buy_kit_item", "execute_kit_purchase",
        "approve_trip", "approve_trip_request", "purchase_trip", "buy_trip",
        "book_trip", "execute_trip_purchase", "purchase_trip_component",
    }
    assert not (forbidden & set(chat_tools.TOOL_FUNCTIONS))
    assert not any(
        any(w in name for w in ("kit", "trip", "onboarding", "travel"))
        and any(verb in name for verb in ("approve", "buy", "purchase", "book", "execute"))
        for name in chat_tools.TOOL_FUNCTIONS)
    for tool_name in ("propose_onboarding_kit", "propose_trip"):
        description = chat_tools.TOOL_DESCRIPTIONS[tool_name]
        assert "does NOT approve" in description


# --- workflow runs ---------------------------------------------------------------

def test_read_workflow_runs_registered_and_graceful(demo_client):
    from app.chat import tools as chat_tools

    assert "read_workflow_runs" in chat_tools.TOOL_FUNCTIONS
    assert "read_workflow_runs" in chat_tools.TOOL_LABELS
    assert "read_workflow_runs" in chat_tools.TOOL_DESCRIPTIONS

    events = _chat(demo_client, "Show me the recent workflow runs")
    assert any(kind == "tool_start" and d["tool"] == "read_workflow_runs"
               for kind, d in events)
    text = _text(events)
    try:
        from app.db.models import WorkflowRun  # noqa: F401
    except ImportError:
        # Workflows lane not merged in this tree: graceful unavailable, no crash.
        assert "not available" in text
        result = next(d for kind, d in events
                      if kind == "tool_result" and d["tool"] == "read_workflow_runs")
        assert result["data"]["available"] is False
        assert result["data"]["workflow_runs"] == []
    else:
        # Merged tree: an empty table answers honestly from a real query.
        assert "No workflow runs yet" in text
