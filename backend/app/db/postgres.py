"""Postgres-only guards, installed after ``create_all``.

1. **Ledger trigger** — the database itself rejects a ledger transaction
   whose entries span two employer namespaces or do not sum to zero per
   currency (checked at COMMIT via a deferred constraint trigger). The app
   enforces the same rule in ``app.ledger``; this is defence in depth.
2. **Row Level Security** — Supabase exposes the ``public`` schema through
   its REST API with the anon key. Enabling RLS with no policies means the
   anon/authenticated roles can read nothing; the backend connects as the
   database owner and is unaffected.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .models import Base

LEDGER_GUARD_SQL = """
CREATE OR REPLACE FUNCTION paysilo_check_ledger_transaction() RETURNS trigger AS $$
DECLARE
    employer_namespaces INT;
    unbalanced INT;
BEGIN
    SELECT count(DISTINCT namespace) INTO employer_namespaces
      FROM ledger_entries
     WHERE transaction_id = NEW.transaction_id
       AND namespace NOT IN ('platform', 'external');
    IF employer_namespaces > 1 THEN
        RAISE EXCEPTION 'cross-employer ledger transaction rejected: funds are tenant-scoped';
    END IF;
    SELECT count(*) INTO unbalanced FROM (
        SELECT currency FROM ledger_entries
         WHERE transaction_id = NEW.transaction_id
         GROUP BY currency HAVING sum(amount) <> 0
    ) s;
    IF unbalanced > 0 THEN
        RAISE EXCEPTION 'ledger transaction does not balance per currency';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_paysilo_check_ledger_transaction ON ledger_entries;
CREATE CONSTRAINT TRIGGER trg_paysilo_check_ledger_transaction
    AFTER INSERT ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION paysilo_check_ledger_transaction();

CREATE OR REPLACE FUNCTION paysilo_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger is append-only: % on % is not allowed', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_paysilo_ledger_entries_append_only ON ledger_entries;
CREATE TRIGGER trg_paysilo_ledger_entries_append_only
    BEFORE UPDATE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION paysilo_append_only();
"""


def rls_sql() -> str:
    return "\n".join(f'ALTER TABLE "{t.name}" ENABLE ROW LEVEL SECURITY;' for t in Base.metadata.sorted_tables)


def install_postgres_guards(conn: Connection) -> None:
    conn.execute(text(LEDGER_GUARD_SQL))
    conn.execute(text(rls_sql()))
