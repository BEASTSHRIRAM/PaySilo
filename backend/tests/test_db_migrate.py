"""Additive migration: a database created by the v2 schema boots under v3 models."""

import sqlite3

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.api import create_app
from app.config import Settings
from app.db import Database
from app.db.migrate import add_column_ddl
from app.db.models import TapKey
from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD


def _old_v2_db(path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tap_keys (
            key_id VARCHAR(80) NOT NULL PRIMARY KEY,
            agent_id VARCHAR(80) NOT NULL,
            public_key_pem TEXT NOT NULL,
            revoked_at DATETIME,
            created_at DATETIME NOT NULL
        );
        INSERT INTO tap_keys VALUES ('old-key', 'agent', 'pem', NULL, '2026-01-01 00:00:00');
        CREATE TABLE chat_threads (id CHAR(32) PRIMARY KEY);
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id CHAR(32) NOT NULL,
            role VARCHAR(16) NOT NULL, content TEXT NOT NULL, created_at DATETIME NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return f"sqlite:///{path.as_posix()}"


def test_create_schema_adds_missing_columns_to_existing_tables(tmp_path):
    url = _old_v2_db(tmp_path / "old.db")
    db = Database.from_url(url)
    db.create_schema()
    cols = {c["name"]: c for c in inspect(db.engine).get_columns("tap_keys")}
    assert {"algorithm", "retired_at", "expires_at"} <= set(cols)
    assert not cols["algorithm"]["nullable"]
    assert "trace" in {c["name"] for c in inspect(db.engine).get_columns("chat_messages")}
    with db.sessionmaker() as s:  # the pre-existing row got the server default
        assert s.get(TapKey, "old-key").algorithm == "ed25519"
    db.create_schema()  # idempotent


def test_app_boots_on_a_v2_database(tmp_path):
    url = _old_v2_db(tmp_path / "old.db")
    settings = Settings(database_url=url, jwt_secret="mig-secret-" + "x" * 32)
    with TestClient(create_app(settings, Database.from_url(url))) as c:
        assert c.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}).status_code == 200
        keys = c.get("/.well-known/tap-keys")
        assert keys.status_code == 200 and all(k["algorithm"] == "ed25519" for k in keys.json()["keys"])


def test_add_column_ddl_not_null_gets_default_on_postgres_dialect():
    from sqlalchemy.dialects import postgresql

    class _E:  # minimal engine stand-in: only .dialect is used
        dialect = postgresql.dialect()

    ddl = add_column_ddl(_E(), "tap_keys", TapKey.__table__.c.algorithm)
    assert ddl == 'ALTER TABLE tap_keys ADD COLUMN algorithm VARCHAR(20) DEFAULT \'ed25519\' NOT NULL'
