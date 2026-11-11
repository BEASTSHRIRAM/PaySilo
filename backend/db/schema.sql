-- PaySilo schema (generated from app/db/models.py — do not edit by hand).
-- Regenerate: uv run python -m app.db.export_schema > db/schema.sql

CREATE TABLE organizations (
	id UUID NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	legal_name VARCHAR(200), 
	country VARCHAR(2) NOT NULL, 
	base_currency VARCHAR(3) NOT NULL, 
	industry VARCHAR(80), 
	size_band VARCHAR(20), 
	website VARCHAR(200), 
	airwallex_platform_account_id VARCHAR(64), 
	is_demo BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (slug)
);

CREATE TABLE tap_keys (
	key_id VARCHAR(80) NOT NULL, 
	agent_id VARCHAR(80) NOT NULL, 
	public_key_pem TEXT NOT NULL, 
	algorithm VARCHAR(20) NOT NULL, 
	retired_at TIMESTAMP WITH TIME ZONE, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (key_id)
);

CREATE TABLE users (
	id UUID NOT NULL, 
	email VARCHAR(254) NOT NULL, 
	full_name VARCHAR(120) NOT NULL, 
	password_hash VARCHAR(100) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	last_login_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (email)
);

CREATE TABLE chat_threads (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_chat_threads_organization_id ON chat_threads (organization_id);
CREATE INDEX ix_chat_threads_user_id ON chat_threads (user_id);
CREATE INDEX ix_chat_threads_user_updated ON chat_threads (user_id, updated_at);

CREATE TABLE employers (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	legal_name VARCHAR(200), 
	country VARCHAR(2) NOT NULL, 
	contact_name VARCHAR(120), 
	contact_email VARCHAR(254), 
	funding_currency VARCHAR(3) NOT NULL, 
	airwallex_account_id VARCHAR(64), 
	airwallex_global_account_id VARCHAR(64), 
	status VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_employer_org_name UNIQUE (organization_id, name), 
	CONSTRAINT ck_employer_status CHECK (status IN ('PENDING', 'ACTIVE', 'SUSPENDED')), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	UNIQUE (airwallex_account_id)
);
CREATE INDEX ix_employers_organization_id ON employers (organization_id);

CREATE TABLE invitations (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	email VARCHAR(254) NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	invited_by_id UUID, 
	status VARCHAR(16) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	accepted_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_invitation_role CHECK (role IN ('admin', 'approver', 'viewer')), 
	CONSTRAINT ck_invitation_status CHECK (status IN ('PENDING', 'ACCEPTED', 'REVOKED')), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	UNIQUE (token_hash), 
	FOREIGN KEY(invited_by_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_invitations_organization_id ON invitations (organization_id);

CREATE TABLE memberships (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_membership_org_user UNIQUE (organization_id, user_id), 
	CONSTRAINT ck_membership_role CHECK (role IN ('owner', 'admin', 'approver', 'viewer')), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_memberships_user_id ON memberships (user_id);

CREATE TABLE chat_messages (
	id BIGSERIAL NOT NULL, 
	thread_id UUID NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	trace JSONB, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_chat_message_role CHECK (role IN ('user', 'assistant', 'system', 'tool')), 
	FOREIGN KEY(thread_id) REFERENCES chat_threads (id) ON DELETE CASCADE
);
CREATE INDEX ix_chat_messages_thread_id ON chat_messages (thread_id);

CREATE TABLE chat_security_events (
	id BIGSERIAL NOT NULL, 
	organization_id UUID NOT NULL, 
	user_id UUID, 
	thread_id UUID, 
	kind VARCHAR(40) NOT NULL, 
	detail TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL, 
	FOREIGN KEY(thread_id) REFERENCES chat_threads (id) ON DELETE SET NULL
);
CREATE INDEX ix_chat_security_events_organization_id ON chat_security_events (organization_id);

CREATE TABLE contractors (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	employer_id UUID NOT NULL, 
	full_name VARCHAR(120) NOT NULL, 
	email VARCHAR(254), 
	country VARCHAR(2) NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	payout_method VARCHAR(8) NOT NULL, 
	iban VARCHAR(34), 
	swift_code VARCHAR(11), 
	account_number VARCHAR(34), 
	routing_code VARCHAR(20), 
	default_amount NUMERIC(19, 2), 
	airwallex_beneficiary_id VARCHAR(64), 
	is_active BOOLEAN NOT NULL, 
	tax_status VARCHAR(10) DEFAULT 'UNKNOWN' NOT NULL, 
	tax_form_type VARCHAR(8) DEFAULT 'NONE' NOT NULL, 
	tax_form_status VARCHAR(10) DEFAULT 'MISSING' NOT NULL, 
	tax_form_received_at DATE, 
	tax_form_expires_at DATE, 
	bank_verification_status VARCHAR(24) DEFAULT 'VERIFIED' NOT NULL, 
	bank_details_changed_at TIMESTAMP WITH TIME ZONE, 
	bank_change_requested_by_id UUID, 
	bank_verified_by_id UUID, 
	bank_verified_at TIMESTAMP WITH TIME ZONE, 
	onboarded_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_contractor_method CHECK (payout_method IN ('SWIFT', 'LOCAL')), 
	CONSTRAINT ck_contractor_bank_details CHECK (iban IS NOT NULL OR account_number IS NOT NULL), 
	CONSTRAINT ck_contractor_amount_positive CHECK (default_amount IS NULL OR default_amount > 0), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(employer_id) REFERENCES employers (id) ON DELETE CASCADE, 
	FOREIGN KEY(bank_change_requested_by_id) REFERENCES users (id) ON DELETE SET NULL, 
	FOREIGN KEY(bank_verified_by_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_contractors_employer_id ON contractors (employer_id);
CREATE INDEX ix_contractors_organization_id ON contractors (organization_id);

CREATE TABLE payroll_runs (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	employer_id UUID NOT NULL, 
	created_by_id UUID, 
	parent_run_id UUID, 
	status VARCHAR(20) NOT NULL, 
	source VARCHAR(16) NOT NULL, 
	period_label VARCHAR(60), 
	pay_date DATE, 
	raw_input TEXT, 
	payroll_currency VARCHAR(5), 
	required_usd NUMERIC(19, 2), 
	shortfall_usd NUMERIC(19, 2), 
	fee_charged_usd NUMERIC(19, 2) NOT NULL, 
	assessment JSONB, 
	pending_interrupt JSONB, 
	explanation TEXT, 
	error TEXT, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	settled_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_run_status CHECK (status IN ('RECEIVED', 'PRICED', 'RESERVED', 'APPROVED', 'SENT', 'SETTLED', 'HELD_SHORTFALL', 'HELD_BY_HUMAN', 'APPROVAL_REJECTED', 'REFUSED', 'FAILED', 'PARTIALLY_PAID', 'SENT_FEE_FAILED', 'SENT_UNRECONCILED', 'ATTEST_PENDING')), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(employer_id) REFERENCES employers (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by_id) REFERENCES users (id) ON DELETE SET NULL, 
	FOREIGN KEY(parent_run_id) REFERENCES payroll_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_payroll_runs_employer_id ON payroll_runs (employer_id);
CREATE INDEX ix_payroll_runs_parent_run_id ON payroll_runs (parent_run_id);
CREATE INDEX ix_runs_org_created ON payroll_runs (organization_id, created_at);

CREATE TABLE agent_events (
	id BIGSERIAL NOT NULL, 
	run_id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	seq INTEGER NOT NULL, 
	agent VARCHAR(24) NOT NULL, 
	level VARCHAR(10) NOT NULL, 
	message TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_agent_event_seq UNIQUE (run_id, seq), 
	FOREIGN KEY(run_id) REFERENCES payroll_runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE
);
CREATE INDEX ix_agent_events_organization_id ON agent_events (organization_id);

CREATE TABLE airwallex_requests (
	id BIGSERIAL NOT NULL, 
	request_id VARCHAR(64) NOT NULL, 
	organization_id UUID NOT NULL, 
	employer_id UUID, 
	run_id UUID, 
	operation VARCHAR(40) NOT NULL, 
	on_behalf_of VARCHAR(64), 
	response JSONB, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (request_id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(employer_id) REFERENCES employers (id) ON DELETE CASCADE, 
	FOREIGN KEY(run_id) REFERENCES payroll_runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_airwallex_requests_organization_id ON airwallex_requests (organization_id);
CREATE INDEX ix_airwallex_requests_run_id ON airwallex_requests (run_id);

CREATE TABLE approvals (
	id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	kind VARCHAR(12) NOT NULL, 
	decision VARCHAR(8) NOT NULL, 
	decided_by_id UUID, 
	decided_by_email VARCHAR(254) NOT NULL, 
	signed_body TEXT, 
	tap_key_id VARCHAR(80), 
	tap_signature_input TEXT, 
	tap_signature TEXT, 
	tap_nonce VARCHAR(64), 
	verified BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_approval_kind CHECK (kind IN ('approval', 'escalation')), 
	CONSTRAINT ck_approval_decision CHECK (decision IN ('approve', 'hold')), 
	FOREIGN KEY(run_id) REFERENCES payroll_runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(decided_by_id) REFERENCES users (id) ON DELETE SET NULL, 
	UNIQUE (tap_nonce)
);
CREATE INDEX ix_approvals_run_id ON approvals (run_id);

CREATE TABLE contractor_changes (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	contractor_id UUID NOT NULL, 
	change_group VARCHAR(12) NOT NULL, 
	summary TEXT NOT NULL, 
	changed_by_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_contractor_change_group CHECK (change_group IN ('bank', 'profile', 'tax', 'onboarding')), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(contractor_id) REFERENCES contractors (id) ON DELETE CASCADE, 
	FOREIGN KEY(changed_by_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_contractor_changes_contractor ON contractor_changes (contractor_id, created_at);
CREATE INDEX ix_contractor_changes_organization_id ON contractor_changes (organization_id);

CREATE TABLE contractor_onboarding_invites (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	employer_id UUID NOT NULL, 
	contractor_id UUID NOT NULL, 
	email VARCHAR(254), 
	token_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_by_id UUID, 
	accepted_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_onboarding_invite_status CHECK (status IN ('PENDING', 'ACCEPTED', 'REVOKED')), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(employer_id) REFERENCES employers (id) ON DELETE CASCADE, 
	FOREIGN KEY(contractor_id) REFERENCES contractors (id) ON DELETE CASCADE, 
	UNIQUE (token_hash), 
	FOREIGN KEY(created_by_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_contractor_onboarding_invites_contractor_id ON contractor_onboarding_invites (contractor_id);
CREATE INDEX ix_contractor_onboarding_invites_organization_id ON contractor_onboarding_invites (organization_id);

CREATE TABLE ledger_transactions (
	id BIGSERIAL NOT NULL, 
	organization_id UUID NOT NULL, 
	employer_id UUID, 
	run_id UUID, 
	memo VARCHAR(200) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(employer_id) REFERENCES employers (id) ON DELETE CASCADE, 
	FOREIGN KEY(run_id) REFERENCES payroll_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_ledger_transactions_employer_id ON ledger_transactions (employer_id);
CREATE INDEX ix_ledger_transactions_organization_id ON ledger_transactions (organization_id);

CREATE TABLE pay_lines (
	id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	contractor_id UUID, 
	position INTEGER NOT NULL, 
	beneficiary_name VARCHAR(120) NOT NULL, 
	amount NUMERIC(19, 2) NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	method VARCHAR(8) NOT NULL, 
	payout_fee NUMERIC(19, 2), 
	status VARCHAR(10) NOT NULL, 
	request_id VARCHAR(64) NOT NULL, 
	airwallex_transfer_id VARCHAR(64), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_pay_line_position UNIQUE (run_id, position), 
	CONSTRAINT ck_pay_line_amount_positive CHECK (amount > 0), 
	CONSTRAINT ck_pay_line_method CHECK (method IN ('SWIFT', 'LOCAL')), 
	CONSTRAINT ck_pay_line_status CHECK (status IN ('PENDING', 'PAID', 'HELD', 'FAILED')), 
	FOREIGN KEY(run_id) REFERENCES payroll_runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(contractor_id) REFERENCES contractors (id) ON DELETE SET NULL, 
	UNIQUE (request_id)
);

CREATE TABLE settlement_receipts (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	employer_id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	chain VARCHAR(16) NOT NULL, 
	chain_index INTEGER NOT NULL, 
	payload_hash VARCHAR(64) NOT NULL, 
	prev_hash VARCHAR(64) NOT NULL, 
	receipt_hash VARCHAR(64) NOT NULL, 
	tx_hash VARCHAR(80), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_receipt_chain_hash UNIQUE (chain, receipt_hash), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(employer_id) REFERENCES employers (id) ON DELETE CASCADE, 
	UNIQUE (run_id), 
	FOREIGN KEY(run_id) REFERENCES payroll_runs (id) ON DELETE CASCADE
);
CREATE INDEX ix_settlement_receipts_organization_id ON settlement_receipts (organization_id);

CREATE TABLE source_imports (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	employer_id UUID NOT NULL, 
	created_by_id UUID, 
	filename VARCHAR(255) NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	content TEXT NOT NULL, 
	mapping JSONB, 
	status VARCHAR(16) NOT NULL, 
	run_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_source_import_employer_hash UNIQUE (employer_id, content_hash), 
	CONSTRAINT ck_source_import_status CHECK (status IN ('PREVIEWED', 'COMMITTED')), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(employer_id) REFERENCES employers (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by_id) REFERENCES users (id) ON DELETE SET NULL, 
	FOREIGN KEY(run_id) REFERENCES payroll_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_source_imports_employer_id ON source_imports (employer_id);
CREATE INDEX ix_source_imports_org_employer ON source_imports (organization_id, employer_id);
CREATE INDEX ix_source_imports_organization_id ON source_imports (organization_id);

CREATE TABLE ledger_entries (
	id BIGSERIAL NOT NULL, 
	transaction_id BIGINT NOT NULL, 
	account VARCHAR(120) NOT NULL, 
	namespace VARCHAR(40) NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	amount NUMERIC(19, 2) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_ledger_entry_nonzero CHECK (amount <> 0), 
	FOREIGN KEY(transaction_id) REFERENCES ledger_transactions (id) ON DELETE CASCADE
);
CREATE INDEX ix_ledger_entries_account ON ledger_entries (account);
CREATE INDEX ix_ledger_entries_transaction_id ON ledger_entries (transaction_id);

CREATE TABLE source_import_rows (
	id BIGSERIAL NOT NULL, 
	import_id UUID NOT NULL, 
	row_number INTEGER NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	errors JSONB, 
	data JSONB, 
	business_key VARCHAR(500), 
	PRIMARY KEY (id), 
	CONSTRAINT ck_source_import_row_status CHECK (status IN ('ok', 'error', 'duplicate')), 
	FOREIGN KEY(import_id) REFERENCES source_imports (id) ON DELETE CASCADE
);
CREATE INDEX ix_source_import_rows_import ON source_import_rows (import_id);

-- Ledger guards: one employer per transaction, balanced per currency, append-only.
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

-- Supabase: deny the anon/authenticated REST roles; the backend connects as owner.
ALTER TABLE "organizations" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "tap_keys" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "users" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "chat_threads" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "employers" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "invitations" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "memberships" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "chat_messages" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "chat_security_events" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "contractors" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "payroll_runs" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "agent_events" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "airwallex_requests" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "approvals" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "contractor_changes" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "contractor_onboarding_invites" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "ledger_transactions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "pay_lines" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "settlement_receipts" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "source_imports" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "ledger_entries" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "source_import_rows" ENABLE ROW LEVEL SECURITY;
