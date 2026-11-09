"""Postgres checkpointer wiring, tested without a Postgres server."""

import pytest

from app.agents import checkpoint
from app.db.session import normalise_url


@pytest.mark.parametrize("url", [
    "postgresql+psycopg2://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db",
    "postgres://u:p@h:5432/db", "postgresql://u:p@h:5432/db"])
def test_all_postgres_url_flavours_become_a_plain_dsn(url):
    assert checkpoint.is_postgres_url(url)
    assert checkpoint._postgres_dsn(url) == "postgresql://u:p@h:5432/db"


def test_sqlalchemy_url_normalises_psycopg2_to_psycopg3():
    assert normalise_url("postgresql+psycopg2://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_rls_sql_covers_every_checkpoint_table_idempotently():
    sql = checkpoint.checkpoint_rls_sql()
    assert len(sql) == 4
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
        assert any(f'public."{table}" ENABLE ROW LEVEL SECURITY' in s and "IF EXISTS" in s for s in sql)


def test_build_checkpointer_uses_pool_runs_setup_then_enables_rls(monkeypatch):
    calls: list[str] = []

    class FakeConn:
        def execute(self, statement):
            calls.append(statement)

    class FakePool:
        def connection(self):
            pool = self

            class _Ctx:
                def __enter__(self_inner):
                    return FakeConn()

                def __exit__(self_inner, *a):
                    return False
            return _Ctx()

    seen: dict = {}

    def fake_make_pool(dsn):
        seen["dsn"] = dsn
        return FakePool()

    class FakeSaver:
        def __init__(self, conn):
            seen["conn"] = conn

        def setup(self):
            calls.append("SETUP")

    monkeypatch.setattr(checkpoint, "_make_pool", fake_make_pool)
    import langgraph.checkpoint.postgres as lgp
    monkeypatch.setattr(lgp, "PostgresSaver", FakeSaver)

    saver = checkpoint.build_checkpointer(database_url="postgresql+psycopg2://u:p@h:6543/db")
    assert isinstance(saver, FakeSaver)
    assert seen["dsn"] == "postgresql://u:p@h:6543/db"
    assert isinstance(seen["conn"], FakePool)  # a pool, not a single long-lived connection
    assert calls[0] == "SETUP"  # RLS only after the tables exist
    assert calls[1:] == checkpoint.checkpoint_rls_sql()


def test_pool_is_health_checked_autocommit_and_pgbouncer_safe(monkeypatch):
    import psycopg_pool

    captured: dict = {}

    class FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", FakePool)
    checkpoint._make_pool("postgresql://u:p@h/db")
    assert captured["check"] is FakePool.check_connection
    assert captured["kwargs"]["autocommit"] is True
    assert captured["kwargs"]["prepare_threshold"] is None
