import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["PAYSILO_ENV_FILE"] = ""  # never read a developer's .env in tests
os.environ["APP_ENV"] = "demo"
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests never call a real LLM
os.environ.pop("DATABASE_URL", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402


@pytest.fixture
def client():
    settings = Settings(database_url="sqlite://", jwt_secret="test-secret-" + "x" * 32, seed_demo=True)
    app = create_app(settings, Database.from_url("sqlite://"))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def demo_client(client):
    from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

    r = client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return client
