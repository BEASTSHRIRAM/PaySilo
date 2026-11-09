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


def _postgres_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy-style Postgres URL to a plain psycopg DSN."""
    url = database_url
    for prefix in ("postgresql+psycopg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


def is_postgres_url(database_url: str | None) -> bool:
    return bool(database_url) and database_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://"))


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
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver

        # autocommit + prepare_threshold=0: the checkpointer manages its own
        # transactions, and Supabase's PgBouncer pooler rejects server-side
        # prepared statements. The connection is held for the process life.
        conn = psycopg.connect(_postgres_dsn(database_url), autocommit=True, prepare_threshold=0)
        saver = PostgresSaver(conn)
        saver.setup()
        logger.info("LangGraph checkpointer: PostgresSaver on DATABASE_URL (durable)")
        return saver

    logger.warning("LangGraph checkpointer: MemorySaver — paused runs will NOT survive a restart. "
                   "Set CHECKPOINT_PATH or a Postgres DATABASE_URL for durable runs.")
    return MemorySaver()
