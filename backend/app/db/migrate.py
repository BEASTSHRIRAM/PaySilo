"""Additive schema migration for databases created by an older PaySilo.

``Base.metadata.create_all`` creates missing TABLES but never adds new
COLUMNS to tables that already exist. A team database that already holds
the v2 schema would therefore break on startup as soon as a model gains a
column (``tap_keys.algorithm`` NOT NULL, ``chat_messages.trace`` ...).

:func:`add_missing_columns` inspects every model table that already exists
and ``ALTER TABLE ... ADD COLUMN`` for each model column the table lacks. It
is additive and idempotent, portable across SQLite and Postgres, and never
drops, renames or retypes anything. A NOT NULL column gets a server default
(the model's scalar default, or ``CURRENT_TIMESTAMP`` for time columns) so
existing rows stay valid; a NOT NULL column with no usable default is added
as nullable rather than failing the migration.

On Postgres it also widens the ``ck_run_status`` CHECK constraint to the
current ``RUN_STATUSES`` (the status vocabulary grew in v3).
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import Column

from .models import RUN_STATUSES, Base

logger = logging.getLogger(__name__)


def _quote(engine: Engine, name: str) -> str:
    return engine.dialect.identifier_preparer.quote(name)


def _literal(value: object) -> str | None:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _server_default_sql(column: Column) -> str | None:
    if column.server_default is not None:
        arg = getattr(column.server_default, "arg", None)
        if isinstance(arg, str):
            return _literal(arg)
        return str(arg) if arg is not None else None
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        return _literal(default.arg)
    if default is not None and isinstance(column.type.python_type, type) and \
            issubclass(column.type.python_type, (dt.datetime, dt.date)):
        return "CURRENT_TIMESTAMP"
    return None


def add_column_ddl(engine: Engine, table: str, column: Column) -> str:
    """The ``ALTER TABLE`` statement that adds ``column`` to an existing table."""
    ctype = column.type.compile(dialect=engine.dialect)
    default = _server_default_sql(column)
    parts = [f"ALTER TABLE {_quote(engine, table)} ADD COLUMN {_quote(engine, column.name)} {ctype}"]
    if default is not None:
        parts.append(f"DEFAULT {default}")
    if not column.nullable and default is not None:
        parts.append("NOT NULL")
    for fk in column.foreign_keys:
        target = fk.column
        clause = f"REFERENCES {_quote(engine, target.table.name)} ({_quote(engine, target.name)})"
        if fk.ondelete:
            clause += f" ON DELETE {fk.ondelete}"
        parts.append(clause)
    return " ".join(parts)


def add_missing_columns(engine: Engine) -> list[str]:
    """Add every model column missing from an already-existing table. Returns ``table.column`` names added."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all makes whole tables
        have = {c["name"] for c in inspector.get_columns(table.name)}
        new_columns: set[str] = set()
        for column in table.columns:
            if column.name in have:
                continue
            ddl = add_column_ddl(engine, table.name, column)
            with engine.begin() as conn:
                conn.execute(text(ddl))
            logger.warning("Schema migration: added %s.%s (%s)", table.name, column.name, ddl)
            added.append(f"{table.name}.{column.name}")
            new_columns.add(column.name)
        # create_all only indexes NEW tables: index the added columns once every column exists.
        have |= new_columns
        for index in table.indexes:
            names = {c.name for c in index.columns}
            if index.name and names & new_columns and names <= have:
                cols = ", ".join(_quote(engine, c.name) for c in index.columns)
                uniq = "UNIQUE " if index.unique else ""
                with engine.begin() as conn:
                    conn.execute(text(f"CREATE {uniq}INDEX IF NOT EXISTS {_quote(engine, index.name)} "
                                      f"ON {_quote(engine, table.name)} ({cols})"))
    return added


def widen_run_status_check(conn: Connection) -> None:
    """Postgres: replace ``ck_run_status`` so newly added run statuses are accepted."""
    allowed = ", ".join("'" + s + "'" for s in RUN_STATUSES)
    conn.execute(text('ALTER TABLE "payroll_runs" DROP CONSTRAINT IF EXISTS ck_run_status'))
    conn.execute(text(f'ALTER TABLE "payroll_runs" ADD CONSTRAINT ck_run_status CHECK (status IN ({allowed}))'))


def widen_notification_kind_check(conn: Connection) -> None:
    """Postgres: replace ``ck_notification_kind`` so newly added notification kinds are accepted."""
    from .models import NOTIFICATION_KINDS

    allowed = ", ".join("'" + k + "'" for k in NOTIFICATION_KINDS)
    conn.execute(text('ALTER TABLE "notifications" DROP CONSTRAINT IF EXISTS ck_notification_kind'))
    conn.execute(text(f'ALTER TABLE "notifications" ADD CONSTRAINT ck_notification_kind CHECK (kind IN ({allowed}))'))


def migrate(engine: Engine) -> list[str]:
    added = add_missing_columns(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            widen_run_status_check(conn)
            if inspect(conn).has_table("notifications"):
                widen_notification_kind_check(conn)
    return added
