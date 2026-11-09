"""Durable LangGraph checkpointers for PaySilo runs.

A payroll run pauses at a human decision (a LangGraph ``interrupt``). For
that pause to survive a process restart, the graph state must live outside
the process. This factory picks the checkpointer, in order:

1. **File-backed SQLite** — when ``checkpoint_path`` is configured
   (``CHECKPOINT_PATH``). Used by tests and single-node deployments; the
   state is a plain file on disk, so a new graph instance built on the
   same file resumes a paused run exactly where it stopped.
2. **Postgres** — when ``DATABASE_URL`` points at Postgres (production:
   Supabase). ``PostgresSaver`` from ``langgraph-checkpoint-postgres``
   stores checkpoints in the same database as the rest of PaySilo. The
   wiring is complete, but note honestly: this workspace has no Postgres
   server, so the Postgres path is exercised by construction, not by a
   live restart test — the restart proof in the test suite uses the
   SQLite file checkpointer, which goes through the same LangGraph
   checkpointer interface.
3. **MemorySaver** — last-resort fallback. Paused runs do NOT survive a
   restart in this mode; ``PlatformRuntime`` reports them as not
   resumable, exactly as before.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)


_POSTGRES_PREFIXES = ("postgresql+psycopg2://", "postgresql+psycopg://", "postgres://", "postgresql://")

# Tables created by PostgresSaver.setup() in ``public``. They hold full graph
# state, i.e. pay lines with IBANs, so Supabase's REST roles must not read them.
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations")


def _postgres_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy-style Postgres URL to a plain psycopg DSN."""
    for prefix in _POSTGRES_PREFIXES:
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix):]
    return database_url


def is_postgres_url(database_url: str | None) -> bool:
    return bool(database_url) and database_url.startswith(_POSTGRES_PREFIXES)


def checkpoint_rls_sql() -> list[str]:
    """Idempotent RLS statements for the checkpoint tables (no policies = deny anon/authenticated)."""
    return [f'ALTER TABLE IF EXISTS public."{t}" ENABLE ROW LEVEL SECURITY' for t in CHECKPOINT_TABLES]


def enable_checkpoint_rls(conn) -> None:
    for statement in checkpoint_rls_sql():
        conn.execute(statement)


def _make_pool(dsn: str):
    """A health-checked psycopg pool: dead connections (Supabase idle timeouts,
    pooler restarts) are replaced instead of failing every later run.

    autocommit + dict rows are what PostgresSaver requires; prepare_threshold
    None keeps psycopg off server-side prepared statements, which Supabase's
    PgBouncer transaction pooler rejects.
    """
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    return ConnectionPool(
        conninfo=dsn, min_size=1, max_size=5, open=True,
        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        check=ConnectionPool.check_connection,
    )


def build_checkpointer(*, database_url: str | None = None, checkpoint_path: str | None = None):
    """Return a LangGraph checkpointer per the precedence above."""
    if checkpoint_path:
        path = Path(checkpoint_path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        from langgraph.checkpoint.sqlite import SqliteSaver

        saver = SqliteSaver(conn)
        saver.setup()
        logger.info("LangGraph checkpointer: SQLite file at %s (durable)", path)
        return saver

    if is_postgres_url(database_url):
        from langgraph.checkpoint.postgres import PostgresSaver

        pool = _make_pool(_postgres_dsn(database_url))
        saver = PostgresSaver(pool)
        saver.setup()
        with pool.connection() as conn:
            enable_checkpoint_rls(conn)
        logger.info("LangGraph checkpointer: PostgresSaver on DATABASE_URL (pooled, RLS enabled, durable)")
        return saver

    logger.warning("LangGraph checkpointer: MemorySaver — paused runs will NOT survive a restart. "
                   "Set CHECKPOINT_PATH or a Postgres DATABASE_URL for durable runs.")
    return MemorySaver()
