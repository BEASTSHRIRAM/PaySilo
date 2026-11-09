from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD
from datetime import UTC, datetime, timedelta
import hashlib
import uuid


def test_profile_and_password_routes(demo_client):
    updated = demo_client.patch("/auth/me", json={"full_name": "Demo Changed"})
    assert updated.status_code == 200 and updated.json()["user"]["full_name"] == "Demo Changed"
    assert demo_client.post("/auth/password", json={"current_password": "wrong", "new_password": "NewPassword2026"}).status_code == 400
    assert demo_client.post("/auth/password", json={"current_password": DEMO_PASSWORD, "new_password": "NewPassword2026"}).status_code == 204
    demo_client.post("/auth/logout")
    assert demo_client.post("/auth/login", json={"email": DEMO_EMAIL, "password": "NewPassword2026"}).status_code == 200


def test_invitation_create_list_revoke_accept(demo_client):
    created = demo_client.post("/org/invitations", json={"email": "new.member@example.com", "role": "approver"})
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["invite_path"].startswith("/invite/")
    token = row["invite_path"].split("/")[-1]
    assert len(demo_client.get("/org/invitations").json()) == 1
    lookup = demo_client.get(f"/invitations/{token}")
    assert lookup.status_code == 200 and lookup.json()["existing_user"] is False
    assert demo_client.post(f"/invitations/{token}/accept", json={"full_name": "New Member", "password": "MemberPass2026"}).status_code == 200
    assert "paysilo_session" in demo_client.cookies
    assert any(m["email"] == "new.member@example.com" and m["role"] == "approver" for m in demo_client.get("/org/members").json())
    assert demo_client.post(f"/invitations/{token}/accept", json={"full_name": "New Member", "password": "MemberPass2026"}).status_code == 404
    # Accepting signed this client in as the new approver; switch back to the owner.
    demo_client.post("/auth/logout")
    demo_client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    revoked = demo_client.post("/org/invitations", json={"email": "revoke@example.com", "role": "viewer"}).json()
    revoke_token = revoked["invite_path"].split("/")[-1]
    assert demo_client.post(f"/org/invitations/{revoked['id']}/revoke").status_code == 200
    assert demo_client.get(f"/invitations/{revoke_token}").status_code == 404


def test_invitation_permissions_and_duplicates(demo_client):
    assert demo_client.post("/org/invitations", json={"email": "pending@example.com", "role": "viewer"}).status_code == 201
    assert demo_client.post("/org/invitations", json={"email": "pending@example.com", "role": "viewer"}).status_code == 409
    inv = demo_client.post("/org/invitations", json={"email": "admininvite@example.com", "role": "admin"}).json()
    token = inv["invite_path"].split("/")[-1]
    demo_client.post(f"/invitations/{token}/accept", json={"full_name": "Admin User", "password": "AdminPass2026"})
    # Now signed in as the new admin: an existing member can't be invited again.
    assert demo_client.post("/org/invitations", json={"email": "admininvite@example.com", "role": "viewer"}).status_code == 409
    assert demo_client.post("/org/invitations", json={"email": "notallowed@example.com", "role": "admin"}).status_code == 403
    demo_client.post("/auth/logout")
    assert demo_client.post("/auth/login", json={"email": "admininvite@example.com", "password": "AdminPass2026"}).status_code == 200
    demo_client.post("/auth/logout")
    demo_client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    inv2 = demo_client.post("/org/invitations", json={"email": "viewer@example.com", "role": "viewer"}).json()
    tok2 = inv2["invite_path"].split("/")[-1]
    demo_client.post(f"/invitations/{tok2}/accept", json={"full_name": "Viewer User", "password": "ViewerPass2026"})
    demo_client.post("/auth/logout")
    demo_client.post("/auth/login", json={"email": "viewer@example.com", "password": "ViewerPass2026"})
    assert demo_client.post("/org/invitations", json={"email": "x@example.com", "role": "viewer"}).status_code == 403


def test_last_owner_protected(demo_client):
    owner = demo_client.get("/auth/me").json()["user"]["id"]
    assert demo_client.delete(f"/org/members/{owner}").status_code == 400
    assert demo_client.patch(f"/org/members/{owner}", json={"role": "admin"}).status_code == 400


def test_existing_user_accept_and_expired_invitation(demo_client):
    # Create a user in a separate organization, then invite that existing account.
    demo_client.post("/auth/logout")
    signup = {"organization": {"name": "Existing Account Org", "country": "US", "base_currency": "USD"},
              "admin": {"full_name": "Existing User", "email": "existing@example.com", "password": "ExistingPass2026"}}
    assert demo_client.post("/auth/signup", json=signup).status_code == 201
    demo_client.post("/auth/logout")
    demo_client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    invitation = demo_client.post("/org/invitations", json={"email": "existing@example.com", "role": "viewer"}).json()
    token = invitation["invite_path"].split("/")[-1]
    assert demo_client.get(f"/invitations/{token}").json()["existing_user"] is True
    assert demo_client.post(f"/invitations/{token}/accept", json={"password": "ExistingPass2026"}).status_code == 200
    assert demo_client.get("/auth/me").json()["role"] == "viewer"

    # Use the database fixture to move the expiry into the past.
    from app.db.models import Invitation
    expired_token = "expired-token"
    with demo_client.app.state.db.sessionmaker() as db:
        pending = Invitation(organization_id=uuid.UUID(demo_client.get("/auth/me").json()["organization"]["id"]),
                             email="expired@example.com", role="viewer", token_hash=hashlib.sha256(expired_token.encode()).hexdigest(),
                             status="PENDING", expires_at=datetime.now(UTC) - timedelta(days=1))
        db.add(pending); db.commit()
    assert demo_client.get(f"/invitations/{expired_token}").status_code == 404


def test_team_tenant_isolation(demo_client):
    invite = demo_client.post("/org/invitations", json={"email": "private@example.com", "role": "viewer"}).json()
    demo_client.post("/auth/logout")
    signup = {"organization": {"name": "Another Team Org", "country": "US", "base_currency": "USD"},
              "admin": {"full_name": "Other Owner", "email": "other-owner@example.com", "password": "OtherOwner2026"}}
    assert demo_client.post("/auth/signup", json=signup).status_code == 201
    assert demo_client.get("/org/invitations").json() == []
    assert demo_client.get("/org/members").json()[0]["email"] == "other-owner@example.com"
    assert demo_client.post(f"/org/invitations/{invite['id']}/revoke").status_code == 404
