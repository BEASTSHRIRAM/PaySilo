// Typed client for the PaySilo FastAPI backend. Every call goes to /api on
// this origin (Next proxies it), so the HttpOnly session cookie is first-party.

export class ApiError extends Error {
  constructor(public status: number, message: string, public fields: Record<string, string> = {}) {
    super(message);
  }
}

type ValidationItem = { loc?: (string | number)[]; msg?: string };

function parseError(status: number, body: unknown): ApiError {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return new ApiError(status, detail);
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as { message?: string; needs_contractor?: { row_number: number; beneficiary_name?: string | null }[] };
    const names = (d.needs_contractor ?? []).map((r) => r.beneficiary_name || `row ${r.row_number}`).join(", ");
    if (d.message) return new ApiError(status, names ? `${d.message} Missing contractors: ${names}.` : d.message);
  }
  if (Array.isArray(detail)) {
    const fields: Record<string, string> = {};
    for (const item of detail as ValidationItem[]) {
      const key = (item.loc ?? []).filter((p) => p !== "body").join(".");
      const msg = (item.msg ?? "Invalid value").replace(/^Value error, /, "");
      if (key && !fields[key]) fields[key] = msg;
    }
    const first = Object.values(fields)[0] ?? "Please check the form.";
    return new ApiError(status, first, fields);
  }
  return new ApiError(status, status >= 500 ? "PaySilo's server had a problem. Try again." : "Request failed.");
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      ...init,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "Can't reach PaySilo's backend. Is it running on port 8000?");
  }
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) throw parseError(res.status, body);
  return body as T;
}

const post = <T>(path: string, data?: unknown) =>
  request<T>(path, { method: "POST", body: data === undefined ? undefined : JSON.stringify(data) });

// ------------------------------------------------------------------ types
export interface User { id: string; email: string; full_name: string }
export interface Organization {
  id: string; name: string; slug: string; legal_name: string | null; country: string; base_currency: string;
  industry: string | null; size_band: string | null; website: string | null; is_demo: boolean;
}
export interface Session { user: User; organization: Organization; role: string }
export interface TeamMember { id: string; name: string; email: string; role: string; joined_at: string }
export interface Invitation { id: string; email: string; role: string; status: string; expires_at: string; created_at: string; invite_path?: string }
export interface PublicInvitation { organization_name: string; email: string; role: string; invited_by: string | null; existing_user: boolean; expires_at: string }

export interface SignupPayload {
  organization: { name: string; legal_name?: string | null; country: string; base_currency: string;
    industry?: string | null; size_band?: string | null; website?: string | null };
  admin: { full_name: string; email: string; password: string };
}

export interface Contractor {
  id: string; full_name: string; email: string | null; country: string; currency: string; payout_method: string;
  iban_last4: string | null; account_last4: string | null; swift_code: string | null; default_amount: string | null;
  is_active: boolean;
}
export interface Employer {
  id: string; name: string; legal_name: string | null; country: string; contact_name: string | null;
  contact_email: string | null; funding_currency: string; status: string; airwallex_account_id: string | null;
  balances: Record<string, string>; contractor_count: number; created_at: string;
}
export interface EmployerDetail extends Employer { contractors: Contractor[] }

export interface Assessment {
  payroll_currency: string; total_payroll: string; swift_fees: string; total_required: string; payroll_usd: string;
  platform_fee_usd: string; reserve_floor_usd: string; required_usd: string; available_usd: string;
  shortfall_usd: string; can_run: boolean;
  by_currency: { currency: string; payouts: number; payroll: string; fees: string; required: string; required_usd: string }[];
}
export interface RunSummary {
  id: string; employer_id: string; employer_name: string; status: string; source: string;
  /** Set on a "Pay held lines" continuation: the run whose held lines it pays. */
  parent_run_id?: string | null; period_label: string | null;
  payroll_currency: string | null; required_usd: string | null; shortfall_usd: string | null; fee_charged_usd: string;
  explanation: string | null; error: string | null; pending: PendingDecision | null;
  resumable: boolean; created_at: string; settled_at: string | null;
}
export interface PendingDecision {
  kind: "approval" | "escalation"; message: string;
  partial?: boolean; continuation?: boolean; parent_run_id?: string | null; run_fee_waived?: boolean;
}
export interface RunDetail extends RunSummary {
  assessment: Assessment | null;
  pay_lines: { id: string; position: number; beneficiary_name: string; amount: string; currency: string; method: string;
    payout_fee: string | null; status: string; airwallex_transfer_id: string | null }[];
  events: { seq: number; agent: string; level: string; message: string; created_at: string }[];
  approvals: { kind: string; decision: string; decided_by_email: string; verified: boolean; tap_key_id: string | null;
    tap_signature: string | null; created_at: string }[];
  receipt: { chain: string; chain_index: number; receipt_hash: string; payload_hash: string; tx_hash: string | null;
    explorer_url: string | null } | null;
  /** Newest "Pay held lines" run that continues this one. */
  continuation_run_id?: string | null;
  held_line_count?: number;
  /** Held lines can be paid right now (role-independent; the button also needs owner/admin). */
  can_pay_held?: boolean;
  /** The run fee was already collected on this run or a parent up the chain. */
  run_fee_collected?: boolean;
}
export interface Overview {
  organization: Organization; employers: Employer[]; platform_fees: Record<string, string>;
  runs_total: number; runs_settled: number; runs_waiting: number; mode: string;
}

export interface EmployerIn {
  name: string; legal_name?: string | null; country: string;
  contact_name?: string | null; contact_email?: string | null; funding_currency: string;
}
export interface ContractorIn {
  full_name: string; email?: string | null; country: string; currency: string;
  payout_method: "SWIFT" | "LOCAL"; iban?: string | null; swift_code?: string | null;
  account_number?: string | null; routing_code?: string | null; default_amount?: string | null;
}

// ------------------------------------------------------------------ chat (SSE v2 contract)
// Live stream: thinking / tool_start / tool_result / token / approval /
// done / error. The legacy `tool` event is still parsed so an older
// backend keeps rendering while the v2 backend rolls out.
export interface ChatContext { employer_id?: string; run_id?: string; page?: string }
export interface ChatApproval {
  run_id: string; employer_id: string; employer_name: string; summary: string;
}
/** One replayable trace entry, as stored on assistant messages in history. */
export type ChatTraceEvent =
  | { type: "thinking"; step: string; detail?: string; status: "start" | "done" }
  | { type: "tool_start"; tool: string; label: string; input: unknown }
  | { type: "tool_result"; tool: string; summary: string; data?: unknown }
  | { type: "tool"; name: string; label: string; status: string };

export type ChatEvent =
  | { type: "token"; text: string }
  | { type: "thinking"; step: string; detail?: string; status: "start" | "done" }
  | { type: "tool_start"; tool: string; label: string; input: unknown }
  | { type: "tool_result"; tool: string; summary: string; data?: unknown }
  | { type: "tool"; name: string; label: string; status: string }
  | { type: "approval"; approval: ChatApproval }
  | { type: "done"; thread_id: string }
  | { type: "error"; message: string };

function chatEventFrom(eventName: string | null, raw: string): ChatEvent | null {
  let data: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    data = parsed as Record<string, unknown>;
  } catch {
    return null;
  }
  const kind = eventName ?? (typeof data.type === "string" ? data.type : typeof data.event === "string" ? data.event : null);
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  switch (kind) {
    case "token":
      return { type: "token", text: str(data.text) || str(data.delta) };
    case "thinking": {
      const step = str(data.step) || str(data.title) || str(data.label) || str(data.message);
      if (!step) return null;
      const rawStatus = str(data.status).toLowerCase();
      return {
        type: "thinking",
        step,
        ...(str(data.detail) ? { detail: str(data.detail) } : {}),
        status: rawStatus === "done" || rawStatus === "complete" || rawStatus === "completed" ? "done" : "start",
      };
    }
    case "tool_start": {
      const tool = str(data.tool) || str(data.name);
      if (!tool) return null;
      return {
        type: "tool_start",
        tool,
        label: str(data.label) || tool,
        input: data.input ?? data.args ?? data.arguments ?? null,
      };
    }
    case "tool_result": {
      const tool = str(data.tool) || str(data.name);
      if (!tool) return null;
      const resultData = data.data ?? data.result ?? data.output;
      return {
        type: "tool_result",
        tool,
        summary: str(data.summary) || str(data.message),
        ...(resultData !== undefined ? { data: resultData } : {}),
      };
    }
    case "tool":
      return { type: "tool", name: str(data.name), label: str(data.label) || str(data.name), status: str(data.status) || "running" };
    case "approval": {
      const nested = typeof data.approval === "object" && data.approval !== null
        ? (data.approval as Record<string, unknown>)
        : data;
      return {
        type: "approval",
        approval: {
          run_id: str(nested.run_id), employer_id: str(nested.employer_id),
          employer_name: str(nested.employer_name), summary: str(nested.summary),
        },
      };
    }
    case "done":
      return { type: "done", thread_id: str(data.thread_id) };
    case "error":
      return { type: "error", message: str(data.message) || "Silo ran into a problem." };
    default:
      return null;
  }
}

// ------------------------------------------------------------------ calls
export const api = {
  me: () => request<Session>("/auth/me"),
  updateMe: (full_name: string) => request<Session>("/auth/me", { method: "PATCH", body: JSON.stringify({ full_name }) }),
  changePassword: (current_password: string, new_password: string) => request<void>("/auth/password", { method: "POST", body: JSON.stringify({ current_password, new_password }) }),
  login: (email: string, password: string) => post<Session>("/auth/login", { email, password }),
  signup: (payload: SignupPayload) => post<Session>("/auth/signup", payload),
  logout: () => post<void>("/auth/logout"),
  teamMembers: () => request<TeamMember[]>("/org/members"),
  updateMemberRole: (id: string, role: string) => request<{ id: string; role: string }>(`/org/members/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ role }) }),
  removeMember: (id: string) => request<void>(`/org/members/${encodeURIComponent(id)}`, { method: "DELETE" }),
  invitations: () => request<Invitation[]>("/org/invitations"),
  createInvitation: (email: string, role: string) => post<Invitation>("/org/invitations", { email, role }),
  revokeInvitation: (id: string) => post<{ id: string; status: string }>(`/org/invitations/${encodeURIComponent(id)}/revoke`),
  publicInvitation: (token: string) => request<PublicInvitation>(`/invitations/${encodeURIComponent(token)}`),
  acceptInvitation: (token: string, body: { full_name?: string; password: string }) => post<Session>(`/invitations/${encodeURIComponent(token)}/accept`, body),
  demoCredentials: () => request<{ email: string; password: string; organization: string }>("/auth/demo-credentials"),

  overview: () => request<Overview>("/overview"),
  employer: (id: string) => request<EmployerDetail>(`/employers/${id}`),
  createEmployer: (body: EmployerIn) => post<EmployerDetail>("/employers", body),
  fund: (id: string, amount: string) => post<Employer>(`/employers/${id}/fund`, { amount }),
  addContractor: (employerId: string, body: ContractorIn) =>
    post<Contractor>(`/employers/${employerId}/contractors`, body),
  deactivateContractor: (employerId: string, contractorId: string) =>
    request<void>(`/employers/${employerId}/contractors/${contractorId}`, { method: "DELETE" }),
  runs: (employerId?: string) =>
    request<RunSummary[]>(employerId ? `/runs?employer_id=${encodeURIComponent(employerId)}&limit=100` : "/runs"),
  run: (id: string) => request<RunDetail>(`/runs/${id}`),
  startRun: (employerId: string, lines: { contractor_id: string; amount?: string }[]) =>
    post<RunDetail>("/runs", { employer_id: employerId, lines }),
  decide: (id: string, decision: "approve" | "hold") => post<RunDetail>(`/runs/${id}/decision`, { decision }),
  retryRun: (id: string) => post<RunDetail>(`/runs/${id}/retry`),
  /** Start a NEW run that pays only this run's HELD lines (it then waits for a TAP-signed approval). */
  payHeld: (id: string) => post<RunDetail>(`/runs/${id}/pay-held`),

  // Chat history (shapes are normalised by the panel; the backend owns them).
  chatThreads: () => request<unknown>("/chat/threads"),
  chatMessages: (threadId: string) => request<unknown>(`/chat/threads/${encodeURIComponent(threadId)}/messages`),
};

/** POST /chat and stream its SSE events. Throws ApiError when the endpoint is unreachable. */
export async function streamChat(
  payload: { thread_id?: string; message: string; context?: ChatContext },
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(payload),
      signal,
    });
  } catch {
    throw new ApiError(0, "Can't reach Silo chat. Is the backend running?");
  }
  if (!res.ok || !res.body) {
    const body: unknown = await res.json().catch(() => null);
    throw parseError(res.status, body);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const flushBlock = (block: string) => {
    let eventName: string | null = null;
    const dataLines: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (dataLines.length === 0) return;
    const event = chatEventFrom(eventName, dataLines.join("\n"));
    if (event) onEvent(event);
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() ?? "";
    for (const block of blocks) flushBlock(block);
  }
  if (buffer.trim()) flushBlock(buffer);
}

// ==================================================================
// v3 — FEB lane (execution / exceptions / reconciliation / journals /
// evidence / receipts). Appended as one delimited section so the
// coordinator can merge lib/api.ts across lanes without conflicts.
// Backend contracts for these endpoints land in parallel; every field
// below is consumed defensively (missing data renders an honest empty
// state, never an invented figure).
// ==================================================================

// ------------------------------------------------------------------ v3 types
/** One row of GET /exceptions. */
export interface ExceptionItem {
  id: string;
  type: string;
  severity: string; // "critical" | "high" | "medium" | "low" (ordered worst first)
  employer_id: string;
  employer_name: string;
  run_id?: string | null;
  title: string;
  detail: string;
  owner: string;
  next_action: string;
  evidence_label: string;
  created_at: string;
}

/** One currency silo of GET /employers/{id}/reconciliation. All money is a decimal string in `currency`. */
export interface ReconCurrency {
  currency: string;
  opening_cash: string;
  deposits: string;
  refunds: string;
  fx_buys: string;
  fx_sells: string;
  payout_debits: string;
  provider_fees: string;
  platform_charges: string;
  closing_cash: string;
  /** Provider wallet balance for this currency; null/absent when the wallet could not be read. */
  wallet_balance?: string | null;
  wallet_available?: boolean;
  /** Ledger closing cash minus wallet balance (or the internal identity check when no wallet). */
  variance?: string;
  identity_variance?: string;
  status: string; // e.g. "MATCHED" | "VARIANCE" — shown verbatim, variance in coral
}

export interface ReconSettlement {
  run_id: string;
  pay_line_id: string;
  beneficiary_name: string;
  amount: string;
  currency: string;
  status: string;
  matched: boolean;
  provider_ref: string | null;
}

export interface Reconciliation {
  currencies: ReconCurrency[];
  settlements: ReconSettlement[];
  /** null when the employer has no pay lines (never reported as 100). */
  matched_percent: number | null;
  /** "ledger_vs_wallet" = compared with the provider wallet; "ledger_self_check" = internal only. */
  basis?: "ledger_vs_wallet" | "ledger_self_check";
  basis_note?: string;
}

/** GET /runs/{id}/pay-lines/{line}/receipt-token. `path` is the public frontend page (/receipt/{id}?token=). */
export interface ReceiptLink { token: string; path: string; api_path: string; expires_at: string }

/** One double-entry line of a journal transaction. Exactly one of debit/credit is non-zero. */
export interface JournalLine {
  account: string;
  debit: string | null;
  credit: string | null;
  currency: string;
}

export interface JournalTxn {
  id: string;
  date: string;
  memo: string;
  lines: JournalLine[];
  balanced: boolean;
}

/**
 * GET /runs/{id}/evidence. The top-level keys are the contract; the
 * manifest / tap / metal payloads are backend-owned records, so they are
 * typed as records and read through small accessors in the UI.
 */
export interface EvidenceBundle {
  manifest: Record<string, unknown> | string | null;
  employer: Record<string, unknown> | null;
  run: Record<string, unknown> | null;
  approvals: Record<string, unknown>[];
  pay_lines: Record<string, unknown>[];
  agent_events: Record<string, unknown>[];
  tap: Record<string, unknown> | null;
  metal: Record<string, unknown> | null;
  journal_transaction_ids: string[];
  exceptions: Record<string, unknown>[];
  limitations: string[];
}

/** GET /receipts/{pay_line_id}?token= — the public worker receipt. */
export interface ReceiptData {
  beneficiary_name: string;
  amount: string;
  currency: string;
  method: string;
  status: string;
  employer_name: string;
  timeline: { label: string; at: string }[];
  bank_masked: string | null;
  verification: {
    tap_verified: boolean;
    metal_tx_hash: string | null;
    metal_explorer_url: string | null;
    mode: string;
  };
}

// ------------------------------------------------------------------ v3 calls
/** v3 endpoints, merged into the shared `api` object at the bottom of this section. */
// v3 — FE-A lane: Command Center support, Import Wizard, Payroll
// Calendar, Funding Planner. Appended in a delimited section; other
// v3 lanes append their own sections — the coordinator merges.
// Contracts (backend landing in parallel — built exactly to these):
//   POST /employers/{id}/imports/preview
//   POST /employers/{id}/imports/{import_id}/commit
//   GET  /employers/{id}/calendar
// ==================================================================

export interface ImportPreviewRow {
  row_number: number;
  /** Backend-owned vocabulary: "ok" | "error" | "duplicate" (the UI also tolerates "valid" / "invalid"). */
  status: string;
  errors: string[];
  data: Record<string, unknown>;
  /** Set on valid rows: the matched existing contractor, or needs_contractor=true when none matches. */
  contractor_id?: string | null;
  needs_contractor?: boolean;
  match_note?: string | null;
}

export interface ImportPreview {
  import_id: string;
  content_hash: string;
  row_count: number;
  valid_count: number;
  error_count: number;
  duplicate_count: number;
  /** Valid rows that match no existing contractor; commit is refused (422) until they are added. */
  needs_contractor_count?: number;
  rows: ImportPreviewRow[];
  summary: Record<string, unknown> | string | null;
}

export interface ImportPreviewIn {
  filename: string;
  content: string;
  /** CSV column name -> canonical PaySilo field name. */
  mapping?: Record<string, string>;
}

export interface ImportCommitIn {
  period_label?: string;
  /** ISO date, YYYY-MM-DD. */
  pay_date?: string;
}

export type CalendarEventKind = "pay_date" | "cutoff" | "funding_due" | "period_start";

export interface CalendarEvent {
  /** ISO date, YYYY-MM-DD. */
  date: string;
  kind: CalendarEventKind | string;
  run_id?: string | null;
  label: string;
  employer_id?: string | null;
  employer_name?: string | null;
}

export interface CalendarResponse {
  events: CalendarEvent[];
}

export const apiV3 = {
  // FEB lane
  exceptions: (limit = 100, offset = 0) =>
    request<ExceptionItem[]>(`/exceptions?limit=${limit}&offset=${offset}`),
  receiptLink: (runId: string, payLineId: string) =>
    request<ReceiptLink>(`/runs/${encodeURIComponent(runId)}/pay-lines/${encodeURIComponent(payLineId)}/receipt-token`),
  reconciliation: (employerId: string) =>
    request<Reconciliation>(`/employers/${encodeURIComponent(employerId)}/reconciliation`),
  employerJournals: (employerId: string) =>
    request<JournalTxn[]>(`/employers/${encodeURIComponent(employerId)}/journals`),
  platformJournals: () => request<JournalTxn[]>("/platform/journals"),
  runEvidence: (runId: string) =>
    request<EvidenceBundle>(`/runs/${encodeURIComponent(runId)}/evidence`),
  receipt: (payLineId: string, token: string) =>
    request<ReceiptData>(`/receipts/${encodeURIComponent(payLineId)}?token=${encodeURIComponent(token)}`),
  // FE-A lane
  previewImport: (employerId: string, body: ImportPreviewIn) =>
    post<ImportPreview>(`/employers/${encodeURIComponent(employerId)}/imports/preview`, body),
  commitImport: (employerId: string, importId: string, body: ImportCommitIn = {}) =>
    post<RunSummary>(`/employers/${encodeURIComponent(employerId)}/imports/${encodeURIComponent(importId)}/commit`, body),
  calendar: (employerId: string) =>
    request<CalendarResponse>(`/employers/${encodeURIComponent(employerId)}/calendar`),
};

/** Direct-download URLs (the endpoints return file attachments; the session cookie is first-party). */
export const v3Downloads = {
  employerJournals: (employerId: string, format: "csv" | "json") =>
    `/api/employers/${encodeURIComponent(employerId)}/journals/export?format=${format}`,
  platformJournals: (format: "csv" | "json") => `/api/platform/journals/export?format=${format}`,
  evidenceBundle: (runId: string) => `/api/runs/${encodeURIComponent(runId)}/evidence`,
};

// Pages in this lane import { api } for the pre-existing calls above and
// { apiV3 } for the calls in this section; the two are kept as separate
// objects so this section stays append-only for the cross-lane merge.

// ====================================================================== contractor trust (onboarding, tax, bank-change control)
// Statuses and dates only - never tax ids, documents or full bank values.
export type TaxStatus = "US_PERSON" | "NON_US" | "UNKNOWN";
export type TaxFormType = "W9" | "W8BEN" | "W8BENE" | "NONE";
export type TaxFormStatus = "MISSING" | "RECEIVED" | "EXPIRED";

// Declaration merging: adds the trust fields to the existing Contractor type.
export interface Contractor {
  tax_status: TaxStatus; tax_form_type: TaxFormType; tax_form_status: TaxFormStatus;
  tax_form_received_at: string | null; tax_form_expires_at: string | null;
  bank_verification_status: "VERIFIED" | "PENDING_VERIFICATION";
  bank_details_changed_at: string | null; bank_change_requested_by_id: string | null;
  bank_verified_at: string | null; onboarded_at: string | null;
}
export interface BankValidation { valid: boolean; checked: boolean; errors: string[] }
export interface ContractorPatch {
  full_name?: string; email?: string | null; country?: string; currency?: string; payout_method?: "SWIFT" | "LOCAL";
  iban?: string | null; swift_code?: string | null; account_number?: string | null; routing_code?: string | null;
  default_amount?: string | null; tax_status?: TaxStatus; tax_form_type?: TaxFormType; tax_form_status?: TaxFormStatus;
  tax_form_received_at?: string | null; tax_form_expires_at?: string | null;
}
export interface ContractorPatchResult { contractor: Contractor; bank_change_pending: boolean; bank_validation: BankValidation | null }
export interface OnboardingLink {
  id: string; invite_path: string | null; email: string | null; status: string; expires_at: string; created_at: string;
}
export interface ContractorHistory {
  changes: { id: string; group: "bank" | "profile" | "tax" | "onboarding"; summary: string; changed_by: string | null; created_at: string }[];
  payments: { run_id: string; period_label: string | null; amount: string; currency: string; status: string; created_at: string }[];
  invites: OnboardingLink[];
}
export interface PayoutOption { key: string; currency: string; method: string; label: string; fields: string[] }
export interface PublicOnboarding {
  contractor_name: string; first_name: string; employer_name: string; agency_name: string; country: string;
  payout_options: PayoutOption[]; expires_at: string;
}
export interface OnboardingSubmit {
  option: string; country: string; iban?: string | null; swift_code?: string | null; account_number?: string | null;
  routing_code?: string | null; tax_status: "US_PERSON" | "NON_US"; confirmed_accurate: boolean;
}
export interface ComplianceRow {
  contractor_id: string; employer_id: string; employer_name: string; full_name: string; country: string;
}
export interface ComplianceOverview {
  year: number; threshold_usd: string; disclaimer: string;
  counts: { tax_forms: number; bank_pending: number; watch_1099: number; contractors: number };
  tax_forms: (ComplianceRow & {
    issue: "missing" | "expired" | "expiring_soon" | "status_unknown"; tax_status: TaxStatus; tax_form_type: TaxFormType;
    tax_form_status: TaxFormStatus; tax_form_expires_at: string | null; days_to_expiry: number | null;
  })[];
  bank_pending: (ComplianceRow & { changed_at: string | null; age_hours: number | null; requested_by_id: string | null; requested_by: string | null })[];
  watch_1099: (ComplianceRow & { paid_ytd_usd: string; payments: number; w9_on_file: boolean })[];
  ytd: (ComplianceRow & {
    tax_status: TaxStatus; tax_form_status: TaxFormStatus; tax_form_type: TaxFormType; paid_ytd_usd: string; payments: number;
    paid_by_currency: Record<string, string>; unpriced_lines: number; over_1099_threshold: boolean;
  })[];
}

const eid = encodeURIComponent;
export const apiTrust = {
  patchContractor: (employerId: string, contractorId: string, body: ContractorPatch) =>
    request<ContractorPatchResult>(`/employers/${eid(employerId)}/contractors/${eid(contractorId)}`, { method: "PATCH", body: JSON.stringify(body) }),
  bankCheck: (employerId: string, contractorId: string) =>
    request<BankValidation>(`/employers/${eid(employerId)}/contractors/${eid(contractorId)}/bank-check`),
  verifyBank: (employerId: string, contractorId: string) =>
    post<Contractor>(`/employers/${eid(employerId)}/contractors/${eid(contractorId)}/verify-bank`, { callback_confirmed: true }),
  createOnboardingLink: (employerId: string, contractorId: string) =>
    post<OnboardingLink>(`/employers/${eid(employerId)}/contractors/${eid(contractorId)}/onboarding-link`),
  revokeOnboardingLink: (employerId: string, contractorId: string, inviteId: string) =>
    post<OnboardingLink>(`/employers/${eid(employerId)}/contractors/${eid(contractorId)}/onboarding-link/${eid(inviteId)}/revoke`),
  history: (employerId: string, contractorId: string) =>
    request<ContractorHistory>(`/employers/${eid(employerId)}/contractors/${eid(contractorId)}/history`),
  onboardingInfo: (token: string) => request<PublicOnboarding>(`/onboarding/${eid(token)}`),
  onboardingSubmit: (token: string, body: OnboardingSubmit) =>
    post<{ status: string; message: string }>(`/onboarding/${eid(token)}/submit`, body),
  complianceOverview: () => request<ComplianceOverview>("/compliance/overview"),
};
export const trustDownloads = { complianceYtdCsv: "/api/compliance/ytd.csv" };

// ====================================================================
// Funding planner + notifications outbox (feat/funding-planner)
// ====================================================================
export type FundingStatus = "FUNDED" | "SHORT" | "DUE_SOON" | "OVERDUE" | "NO_SCHEDULE" | "NO_LINES" | "RATE_UNAVAILABLE";
export type PayFrequency = "WEEKLY" | "BIWEEKLY" | "MONTHLY";
export interface FundingLine {
  contractor_id: string; name: string; currency: string; method: string;
  amount: string | null; fee: string | null; amount_usd: string | null; fee_usd: string | null; needs_amount: boolean;
}
export interface WireDetails {
  status: "READY" | "PENDING" | "UNAVAILABLE"; currency: string; message: string | null;
  account_name?: string | null; account_number?: string | null; account_type?: string | null;
  swift_code?: string | null; iban?: string | null; bank_name?: string | null; bank_address?: string | null;
  reference?: string | null; routing: { type: string; label: string; value: string }[];
}
export interface FundingPlan {
  employer_id: string; employer_name: string; status: FundingStatus; message: string;
  pay_frequency: PayFrequency | null; funding_lead_days: number;
  scheduled_pay_date: string | null; pay_date: string | null; fund_by: string | null;
  days_until: number | null; days_until_fund_by: number | null; today: string;
  lines: FundingLine[]; needs_amount: FundingLine[];
  by_currency: { currency: string; payouts: number; payroll: string; fees: string; required: string; required_usd: string }[];
  wallet: Record<string, string>;
  payroll_usd: string; run_fee_usd: string; reserve_floor_usd: string; required_in_wallet_usd: string;
  available_usd: string; gap_usd: string; recommended_transfer_usd: string;
  is_short: boolean; contact_email: string | null; contact_name: string | null; wire: WireDetails | null;
}
export interface FundingSummary {
  employers: number; short: number; overdue: number; funded: number; counts: Record<string, number>;
  total_to_collect_usd: string; total_gap_usd: string;
}
export interface OrgFundingPlan { today: string; summary: FundingSummary; plans: FundingPlan[] }
export interface FundingStatement {
  organization: string; employer: string; generated_on: string; status: FundingStatus;
  pay_date: string | null; fund_by: string | null; lines: FundingLine[]; needs_amount: FundingLine[];
  by_currency: FundingPlan["by_currency"]; payroll_usd: string; run_fee_usd: string; reserve_floor_usd: string;
  required_in_wallet_usd: string; available_usd: string; gap_usd: string; recommended_transfer_usd: string;
  wire: WireDetails | null;
}
export interface SweepResult {
  employer_id: string; employer_name: string; action: "queued" | "skipped"; reason: string;
  notification_id: string | null; status: string | null;
  auto_continuation?: { action: string; reason: string; run_id: string | null };
}
export interface SweepResponse { queued: number; skipped: number; results: SweepResult[] }
export type NotificationKind = "FUNDING_REMINDER" | "FUNDING_STATEMENT" | "FUNDING_OVERDUE" | "FUNDS_RECEIVED" | "APPROVAL_REQUESTED"
  | "HELD_LINES_REMINDER" | "RUN_SETTLED" | "PAYMENT_FAILED" | "INVITE" | "PAYMENT_NOTICE" | "GENERIC";
export interface AppNotification {
  id: string; kind: NotificationKind; channel: string; recipient: string; subject: string; body: string;
  status: "QUEUED" | "SENT" | "FAILED"; provider: string | null; attempts: number; last_attempt_at: string | null; error: string | null;
  employer_id: string | null; employer_name: string | null; created_at: string; sent_at: string | null;
}
export interface EmailStatus { configured: boolean; provider: string | null; message: string | null }
export interface NotificationList { email: EmailStatus; total: number; limit: number; offset: number; items: AppNotification[] }

export const apiFunding = {
  plan: () => request<OrgFundingPlan>("/funding/plan"),
  employerPlan: (employerId: string) =>
    request<FundingPlan>(`/employers/${encodeURIComponent(employerId)}/funding-plan`),
  updateSchedule: (employerId: string, body: { pay_frequency?: PayFrequency | null; next_pay_date?: string | null; funding_lead_days?: number }) =>
    request<FundingPlan>(`/employers/${encodeURIComponent(employerId)}/schedule`, { method: "PATCH", body: JSON.stringify(body) }),
  statement: (employerId: string) =>
    request<FundingStatement>(`/employers/${encodeURIComponent(employerId)}/funding-statement`),
  sendReminder: (employerId: string, kind: "reminder" | "statement" = "reminder") =>
    post<AppNotification>(`/employers/${encodeURIComponent(employerId)}/funding-reminder`, { kind }),
  runSweep: () => post<SweepResponse>("/funding/reminders/run"),
  notifications: (params: { employer_id?: string; kind?: string; status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") q.set(k, String(v));
    const qs = q.toString();
    return request<NotificationList>(`/notifications${qs ? `?${qs}` : ""}`);
  },
  statementCsvUrl: (employerId: string) => `/api/employers/${encodeURIComponent(employerId)}/funding-statement?format=csv`,
};

// ====================================================================== proof of payment
export interface PaymentTrace {
  line_id: string; contractor: string; amount: string; currency: string; method: string;
  transfer_id: string | null;
  state: "SCHEDULED" | "SENT" | "PAID" | "FAILED" | "HELD" | "RETURNED";
  paid: boolean; headline: string; diagnosis: string; next_step: string;
  expected_window: string; failure_code: string | null;
}
interface VerifyLine {
  contractor: string; amount: string; currency: string; method: string; status: string;
  bank_masked: string | null; transfer_id: string | null; paid_at: string | null;
}
export interface VerifyResult {
  valid: boolean;
  reason?: string;
  kind?: "line" | "statement";
  facts?: {
    document: "proof" | "notice" | "statement"; agency: string; employer: string; run_id: string;
    period: string | null; pay_date: string | null; tap_verified: boolean; receipt_hash: string | null;
    state?: string; line?: VerifyLine; lines?: VerifyLine[];
  };
}
export const apiProof = {
  trace: (runId: string, lineId: string) =>
    request<PaymentTrace>(`/runs/${encodeURIComponent(runId)}/lines/${encodeURIComponent(lineId)}/trace`),
  verify: (token: string) => request<VerifyResult>(`/verify/${encodeURIComponent(token)}`),
};
/** Direct-download URLs (PDF attachments; the session cookie is first-party). */
export const proofDownloads = {
  line: (runId: string, lineId: string) => `/api/runs/${encodeURIComponent(runId)}/lines/${encodeURIComponent(lineId)}/proof.pdf`,
  statement: (runId: string) => `/api/runs/${encodeURIComponent(runId)}/statement.pdf`,
  receipt: (lineId: string, token: string) => `/api/receipts/${encodeURIComponent(lineId)}/proof.pdf?token=${encodeURIComponent(token)}`,
};
