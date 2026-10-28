-- PaySilo PostgreSQL schema.
-- Mirrors the in-memory ledger/rules in app/ledger.py: money is NUMERIC,
-- entries are append-only, and a trigger rejects any transaction whose
-- entries span two employer namespaces or fail to sum to zero per currency.

CREATE TABLE IF NOT EXISTS employers (
    id              TEXT PRIMARY KEY,               -- employer key, e.g. 'acme'
    name            TEXT NOT NULL,
    contact_name    TEXT NOT NULL DEFAULT '',
    airwallex_account_id TEXT UNIQUE,
    status          TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wallets (
    id              BIGSERIAL PRIMARY KEY,
    employer_id     TEXT NOT NULL REFERENCES employers(id),
    currency        CHAR(3) NOT NULL,
    UNIQUE (employer_id, currency)
    -- No balance column: balances are DERIVED from ledger_entries.
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT,
    memo            TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id              BIGSERIAL PRIMARY KEY,
    transaction_id  BIGINT NOT NULL REFERENCES ledger_transactions(id),
    account         TEXT NOT NULL,                  -- '<namespace>:<account>:<CCY>'
    namespace       TEXT NOT NULL,                  -- employer id | 'platform' | 'external'
    currency        CHAR(3) NOT NULL,
    amount          NUMERIC(19, 2) NOT NULL         -- signed; + into the account
);
CREATE INDEX IF NOT EXISTS idx_entries_account ON ledger_entries(account);
CREATE INDEX IF NOT EXISTS idx_entries_txn ON ledger_entries(transaction_id);

CREATE TABLE IF NOT EXISTS payroll_runs (
    id              TEXT PRIMARY KEY,               -- run id / LangGraph thread id
    employer_id     TEXT NOT NULL REFERENCES employers(id),
    status          TEXT NOT NULL DEFAULT 'RECEIVED',
    -- PRICED -> RESERVED -> APPROVED -> SENT -> SETTLED / HELD_SHORTFALL
    assessment      JSONB,
    fee_charged_usd NUMERIC(19, 2) NOT NULL DEFAULT 0,
    metal_receipt   JSONB,
    explanation     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pay_lines (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES payroll_runs(id),
    beneficiary_name TEXT NOT NULL,
    amount          NUMERIC(19, 2) NOT NULL,
    currency        CHAR(3) NOT NULL,
    beneficiary_id  TEXT,
    transfer_id     TEXT,
    transfer_status TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES payroll_runs(id),
    kind            TEXT NOT NULL,                  -- 'approval' | 'escalation'
    payload         JSONB NOT NULL,                 -- exact amount/currency/beneficiaries shown
    tap_signature   TEXT,                           -- Visa TAP Signature header (signed approvals)
    decision        TEXT,                           -- 'approve' | 'hold'
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT,
    employer_id     TEXT,
    event           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    request_id      TEXT PRIMARY KEY,               -- deterministic per (run, operation)
    operation       TEXT NOT NULL,
    run_id          TEXT,
    result          JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tenant-isolation + balance guard, enforced by the database itself:
-- a transaction's entries must (a) touch at most one employer namespace
-- ('platform'/'external' are shared counterparties) and (b) sum to zero
-- per currency. Checked at COMMIT via a deferred constraint trigger.
CREATE OR REPLACE FUNCTION check_ledger_transaction() RETURNS trigger AS $$
DECLARE
    employer_namespaces INT;
    unbalanced INT;
BEGIN
    SELECT count(DISTINCT namespace) INTO employer_namespaces
      FROM ledger_entries
     WHERE transaction_id = NEW.transaction_id
       AND namespace NOT IN ('platform', 'external');
    IF employer_namespaces > 1 THEN
        RAISE EXCEPTION 'cross-employer transaction rejected: funds are tenant-scoped';
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

DROP TRIGGER IF EXISTS trg_check_ledger_transaction ON ledger_entries;
CREATE CONSTRAINT TRIGGER trg_check_ledger_transaction
    AFTER INSERT ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_ledger_transaction();
