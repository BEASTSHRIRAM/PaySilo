"""The configurable shared demo login for the login page's demo button."""


def test_configured_demo_login_is_served_and_cannot_change_its_password(client):
    settings = client.app.state.settings
    from app.api.deps import get_settings
    from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

    shared = settings.model_copy(update={"demo_login_email": DEMO_EMAIL, "demo_login_password": DEMO_PASSWORD})
    client.app.dependency_overrides[get_settings] = lambda: shared
    client.app.state.settings = shared
    try:
        creds = client.get("/auth/demo-credentials").json()
        assert creds["email"] == DEMO_EMAIL and creds["password"] == DEMO_PASSWORD
        assert client.post("/auth/login", json={"email": creds["email"], "password": creds["password"]}).status_code == 200
        r = client.post("/auth/password", json={"current_password": DEMO_PASSWORD, "new_password": "Changed12345"})
        assert r.status_code == 403
        client.post("/auth/logout")
        assert client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}).status_code == 200
    finally:
        client.app.dependency_overrides.pop(get_settings, None)
        client.app.state.settings = settings
