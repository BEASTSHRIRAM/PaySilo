"""Print the full Postgres DDL (tables + ledger guards + RLS) for Supabase.

    uv run python -m app.db.export_schema > db/schema.sql

Paste the output into the Supabase SQL editor, or just set DATABASE_URL and
let the backend create the schema on startup (AUTO_CREATE_SCHEMA=true).
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from .models import Base
from .postgres import LEDGER_GUARD_SQL, rls_sql


def postgres_ddl() -> str:
    dialect = postgresql.dialect()
    parts = ["-- PaySilo schema (generated from app/db/models.py — do not edit by hand).",
             "-- Regenerate: uv run python -m app.db.export_schema > db/schema.sql", ""]
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            parts.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
        parts.append("")
    parts.append("-- Ledger guards: one employer per transaction, balanced per currency, append-only.")
    parts.append(LEDGER_GUARD_SQL.strip())
    parts.append("")
    parts.append("-- Supabase: deny the anon/authenticated REST roles; the backend connects as owner.")
    parts.append(rls_sql())
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    print(postgres_ddl(), end="")
