"""Onboarding guide: per-user state, auto-start rules, isolation, roles, derived checklist."""

from fastapi.testclient import TestClient

from app.api.routes.tour import TOUR_VERSION
from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

SIGNUP = {
    "organization": {"name": "Northwind Staffing", "country": "gb", "base_currency": "usd"},
    "admin": {"full_name": "Nadia Khan", "email": "nadia@northwind.io", "password": "Northwind2026"},
}


def _signup(c: TestClient) -> dict:
    r = c.post("/auth/signup", json=SIGNUP)
    assert r.status_code == 201, r.text
    return r.json()


def _items(c: TestClient) -> dict[str, bool]:
    return {i["key"]: i["done"] for i in c.get("/me/onboarding/checklist").json()["items"]}


def test_requires_session(client):
    assert client.get("/me/onboarding").status_code == 401
    assert client.put("/me/onboarding", json={"status": "SKIPPED"}).status_code == 401
    assert client.get("/me/onboarding/checklist").status_code == 401


def test_new_signup_auto_starts_once_then_never_again(client):
    _signup(client)
    st = client.get("/me/onboarding").json()
    assert st["status"] == "NOT_STARTED" and st["auto_start"] is True
    assert st["first_name"] == "Nadia" and st["organization_name"] == "Northwind Staffing"
    # Reading never creates progress; starting it does.
    assert client.get("/me/onboarding").json()["auto_start"] is True
    st = client.put("/me/onboarding", json={"status": "IN_PROGRESS", "last_step": "employers"}).json()
    assert st["status"] == "IN_PROGRESS" and st["auto_start"] is False and st["last_step"] == "employers"
    st = client.put("/me/onboarding", json={"status": "COMPLETED", "completed_chapters": ["setup", "bogus", "setup"]}).json()
    assert st["status"] == "COMPLETED" and st["auto_start"] is False
    assert st["completed_chapters"] == ["setup"] and st["outdated"] is False
    assert client.get("/me/onboarding").json()["auto_start"] is False


def test_skip_is_remembered_and_validation(client):
    _signup(client)
    assert client.put("/me/onboarding", json={"status": "SKIPPED"}).json()["auto_start"] is False
    assert client.put("/me/onboarding", json={"status": "NOPE"}).status_code == 422
    st = client.put("/me/onboarding", json={"checklist_dismissed": True}).json()
    assert st["checklist_dismissed"] is True and st["status"] == "SKIPPED"


def test_outdated_tour_is_offered_not_forced(client, monkeypatch):
    _signup(client)
    client.put("/me/onboarding", json={"status": "COMPLETED"})
    from app.api.routes import tour
    monkeypatch.setattr(tour, "TOUR_VERSION", TOUR_VERSION + 1)
    st = client.get("/me/onboarding").json()
    assert st["outdated"] is True and st["auto_start"] is False


def test_demo_account_never_auto_starts(demo_client):
    st = demo_client.get("/me/onboarding").json()
    assert st["status"] == "SKIPPED" and st["auto_start"] is False and st["is_demo"] is True


def test_state_is_isolated_per_user_and_org(client):
    _signup(client)
    client.put("/me/onboarding", json={"status": "COMPLETED", "checklist_dismissed": True})
    client.post("/auth/logout")
    client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    # Demo's row is untouched by the other org's writes.
    st = client.get("/me/onboarding").json()
    assert st["status"] == "SKIPPED" and st["checklist_dismissed"] is False
    client.put("/me/onboarding", json={"status": "IN_PROGRESS"})
    client.post("/auth/logout")
    client.post("/auth/login", json={"email": "nadia@northwind.io", "password": "Northwind2026"})
    assert client.get("/me/onboarding").json()["status"] == "COMPLETED"


def test_invited_member_gets_their_own_first_session(demo_client):
    inv = demo_client.post("/org/invitations", json={"email": "ap@example.com", "role": "approver"}).json()
    token = inv["invite_path"].split("/")[-1]
    r = demo_client.post(f"/invitations/{token}/accept", json={"full_name": "Ada Approver", "password": "ApproverPass2026"})
    assert r.status_code == 200
    st = demo_client.get("/me/onboarding").json()
    # A brand-new member in the (demo) org: demo orgs never auto-start, a real org would.
    assert st["role"] == "approver" and st["first_name"] == "Ada" and st["is_demo"] is True


def test_invited_member_in_real_org_auto_starts(client):
    _signup(client)
    inv = client.post("/org/invitations", json={"email": "ap@example.com", "role": "viewer"}).json()
    client.put("/me/onboarding", json={"status": "COMPLETED"})
    token = inv["invite_path"].split("/")[-1]
    client.post(f"/invitations/{token}/accept", json={"full_name": "Vic Viewer", "password": "ViewerPass2026"})
    st = client.get("/me/onboarding").json()
    assert st["role"] == "viewer" and st["status"] == "NOT_STARTED" and st["auto_start"] is True
    checklist = client.get("/me/onboarding/checklist").json()
    assert checklist["visible"] is False and checklist["items"] == []


def test_checklist_ticks_only_from_real_data(client):
    _signup(client)
    first = client.get("/me/onboarding/checklist").json()
    assert first["visible"] and first["done_count"] == 0 and first["total"] == 5 and not first["all_done"]
    assert not any(_items(client).values())
    emp = client.post("/employers", json={"name": "Acme Ltd", "country": "gb"}).json()
    assert _items(client)["employer"] is True and _items(client)["fund"] is False
    r = client.post(f"/employers/{emp['id']}/contractors", json={
        "full_name": "Lena Fischer", "country": "DE", "currency": "EUR", "payout_method": "LOCAL",
        "iban": "DE89370400440532013000", "default_amount": "1000"})
    assert r.status_code == 201, r.text
    assert _items(client)["contractors"] is True
    assert client.post(f"/employers/{emp['id']}/fund", json={"amount": "5000"}).status_code == 200
    items = _items(client)
    assert items["fund"] is True and items["payroll"] is False and items["approver"] is False
    client.post("/org/invitations", json={"email": "boss@example.com", "role": "approver"})
    assert _items(client)["approver"] is True
    assert client.get("/me/onboarding/checklist").json()["done_count"] == 4
    assert _items(client)["schedule"] is False and _items(client)["email"] is False


def test_checklist_is_tenant_scoped(client):
    # The demo org is rich; a fresh org must not inherit any of it.
    client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    demo = client.get("/me/onboarding/checklist").json()
    assert demo["done_count"] >= 3
    client.post("/auth/logout")
    _signup(client)
    assert client.get("/me/onboarding/checklist").json()["done_count"] == 0
