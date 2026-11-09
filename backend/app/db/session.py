"""Database engine + sessions.

``DATABASE_URL`` may be any SQLAlchemy URL. A Supabase connection string
(``postgresql://postgres:...@db.<ref>.supabase.co:5432/postgres`` or the
pooler on port 6543) is normalised to the psycopg 3 driver and made
PgBouncer-safe. With no URL, a local SQLite file is used so the demo runs
with zero setup.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ..config import BACKEND_DIR
from .migrate import migrate
from .models import Base
from .postgres import install_postgres_guards


def normalise_url(url: str | None) -> str:
    if not url:
        return f"sqlite:///{(BACKEND_DIR / 'paysilo.db').as_posix()}"
    for prefix in ("postgresql+psycopg2://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str | None) -> Engine:
    url = normalise_url(url)
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    # prepare_threshold=None keeps psycopg from using server-side prepared
    # statements, which Supabase's transaction pooler (PgBouncer) rejects.
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5,
                         connect_args={"prepare_threshold": None})


@dataclass
class Database:
    engine: Engine
    sessionmaker: sessionmaker[Session]

    @classmethod
    def from_url(cls, url: str | None) -> "Database":
        engine = make_engine(url)
        return cls(engine=engine, sessionmaker=sessionmaker(bind=engine, expire_on_commit=False))

    @property
    def is_postgres(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        migrate(self.engine)  # additive: new columns on tables that already exist
        if self.is_postgres:
            with self.engine.begin() as conn:
                install_postgres_guards(conn)

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

    def session(self) -> Iterator[Session]:
        db = self.sessionmaker()
        try:
            yield db
        finally:
            db.close()
