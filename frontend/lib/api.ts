// Typed client for the PaySilo FastAPI backend. Every call goes to /api on
// this origin (Next proxies it), so the HttpOnly session cookie is first-party.

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public fields: Record<string, string> = {},
    /** Machine-readable code when the backend sent `detail: {code, message}` (e.g. a 409 policy refusal). */
    public code: string | null = null,
    /** Verbatim server validation errors, e.g. 422 {detail: {code: "INVALID_GRAPH", errors: [...]}}. */
    public validationErrors: string[] = [],
  ) {
    super(message);
  }
}

type ValidationItem = { loc?: (string | number)[]; msg?: string };

function parseError(status: number, body: unknown): ApiError {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return new ApiError(status, detail);
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    // Structured refusal: {code, message} — keep the server message verbatim
    // and expose the code so callers (e.g. Benefits) can branch on it.
    // Workflow validation: {code: "INVALID_GRAPH", errors: [...]} — preserve
    // the errors verbatim so the editor can surface them in a banner.
    const d = detail as {
      code?: unknown; message?: string; errors?: unknown;
      needs_contractor?: { row_number: number; beneficiary_name?: string | null }[];
    };
    const code = typeof d.code === "string" && d.code ? d.code : null;
    const validationErrors: string[] = Array.isArray(d.errors)
      ? d.errors.map((e) => (typeof e === "string" ? e : JSON.stringify(e)))
      : [];
    if (code) {
      const msg = typeof d.message === "string" && d.message
        ? d.message
        : validationErrors.length > 0
          ? validationErrors.join(" ")
          : code;
      return new ApiError(status, msg, {}, code, validationErrors);
    }
    const names = (d.needs_contractor ?? []).map((r) => r.beneficiary_name || `row ${r.row_number}`).join(", ");
    if (d.message) return new ApiError(status, names ? `${d.message} Missing contractors: ${names}.` : d.message, {}, null, validationErrors);
    if (validationErrors.length > 0) return new ApiError(status, validationErrors[0], {}, null, validationErrors);
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
  id: string; employer_id: string; employer_name: string; status: string; source: string; period_label: string | null;
  payroll_currency: string | null; required_usd: string | null; shortfall_usd: string | null; fee_charged_usd: string;
  explanation: string | null; error: string | null; pending: { kind: "approval" | "escalation"; message: string } | null;
  resumable: boolean; created_at: string; settled_at: string | null;
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

// ==================================================================
// VIC lane — Silo Benefits (employer-funded benefits bought via Visa
// VIC / Airi). Appended as its own delimited section, like the v3
// lanes above. All endpoints are org-scoped by the session cookie;
// every money field is a decimal string; every id is a string.
// Policy refusals arrive as HTTP 409 with body
//   {detail: {code, message}}
// which parseError() above surfaces as ApiError.message (verbatim) +
// ApiError.code — see benefitError() in the Benefits page.
// ==================================================================

// ------------------------------------------------------------------ VIC types
export type VicMode = "SIMULATED" | "VIC" | "VIC_UNAVAILABLE";

/** GET /benefits/status — how benefit purchases will be executed right now. */
export interface VicStatus {
  mode: VicMode;
  /** Masked credential reference (never a raw credential). */
  credential_ref: string;
  /** True when purchases run against the published Airwallex test card. */
  test_card: boolean;
  /** Per-transaction ceiling imposed by the credential, when known. */
  tx_limit_usd: string | null;
  cli_available: boolean;
}

/** One employer's benefits budget: GET /benefits/budgets[]. */
export interface BenefitBudget {
  id: string;
  employer_id: string;
  employer_name: string;
  currency: string;
  monthly_cap: string;
  per_tx_limit: string | null;
  spent_this_period: string;
  remaining: string;
  period: string;
  enabled: boolean;
  /** True while payroll for this employer is held — wages come first. */
  locked: boolean;
  lock_reason: string | null;
}

export interface BenefitBudgetIn {
  currency: string;
  monthly_cap: string;
  per_tx_limit: string | null;
  enabled: boolean;
}

export type BenefitRequestStatus = "REQUESTED" | "APPROVED" | "DENIED" | "PURCHASED" | "FAILED";

/** One purchase attempt against a benefit request: GET /benefits/purchases[]. */
export interface BenefitPurchase {
  id: string;
  request_id: string;
  employer_id: string;
  attempt_no: number;
  mode: "SIMULATED" | "VIC";
  credential_ref: string;
  provider_ref: string | null;
  status: "SUCCEEDED" | "FAILED";
  failure_code: string | null;
  failure_detail: string | null;
  amount: string;
  currency: string;
  journal_txn_id: number | null;
  created_at: string;
}

/** One benefit request: GET /benefits/requests[]. */
export interface BenefitRequest {
  id: string;
  employer_id: string;
  employer_name: string;
  contractor_id: string | null;
  contractor_name: string;
  merchant: string;
  item: string;
  amount: string;
  currency: string;
  status: BenefitRequestStatus;
  requested_by_email: string;
  decided_by_email: string | null;
  decided_at: string | null;
  deny_reason: string | null;
  approval_expires_at: string | null;
  approval_hash: string | null;
  created_at: string;
  latest_purchase: BenefitPurchase | null;
}

export interface BenefitRequestIn {
  employer_id: string;
  contractor_id?: string | null;
  contractor_name?: string | null;
  merchant: string;
  item: string;
  amount: string;
  currency: string;
}

export interface BenefitDecisionIn {
  decision: "approve" | "deny";
  reason?: string;
}

export interface BenefitListParams {
  employer_id?: string;
  status?: string;
}

function benefitsQuery(params?: BenefitListParams): string {
  const q = new URLSearchParams();
  if (params?.employer_id) q.set("employer_id", params.employer_id);
  if (params?.status) q.set("status", params.status);
  const s = q.toString();
  return s ? `?${s}` : "";
}

// ==================================================================
// Workflows lane — n8n-style automation canvas (WF-FE).
// Appended as its own delimited section, like the v3 / VIC lanes above.
// Contracts (WF-BE lane, built in parallel — coded exactly to these):
//   GET    /workflows
//   POST   /workflows {name, description, graph} -> 201 | 422 INVALID_GRAPH
//   GET    /workflows/{id}
//   PUT    /workflows/{id} {name?, description?, enabled?, graph?}
//   DELETE /workflows/{id}
//   GET    /workflows/runs?workflow_id=&status=
//   GET    /workflows/runs/{run_id}
// Validation failures arrive as 422 {detail: {code: "INVALID_GRAPH",
// errors: [...]}} — parseError() above preserves them on
// ApiError.validationErrors (and tolerates FastAPI's default 422 shape).
// ==================================================================

// ------------------------------------------------------------------ workflow types
export type WorkflowTriggerType = string;

export interface WorkflowSummary {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  trigger_type: string;
  updated_at: string;
}

export interface WorkflowGraphNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: Record<string, unknown>;
}

export interface WorkflowGraphEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string | null;
}

export interface WorkflowGraph {
  nodes: WorkflowGraphNode[];
  edges: WorkflowGraphEdge[];
}

export interface WorkflowDetail extends WorkflowSummary {
  graph: WorkflowGraph;
  created_at?: string;
}

export interface WorkflowCreateIn {
  name: string;
  description: string;
  graph: WorkflowGraph;
}

export interface WorkflowUpdateIn {
  name?: string;
  description?: string;
  enabled?: boolean;
  graph?: WorkflowGraph;
}

export type WorkflowRunStatus = "RUNNING" | "WAITING" | "COMPLETED" | "FAILED";

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  workflow_name: string;
  trigger_event: string;
  entity_kind: string;
  entity_id: string;
  entity_label: string;
  status: WorkflowRunStatus | string;
  started_at: string;
  finished_at: string | null;
}

export type WorkflowStepStatus = "OK" | "FAILED" | "SKIPPED" | "WAITING" | "RESUMED";

export interface WorkflowRunStep {
  node_id: string;
  node_type: string;
  status: WorkflowStepStatus | string;
  detail: string;
  created_at: string;
  /** Optional engine output (defensive — the execution view reads output.branch for taken condition edges when present). */
  output?: Record<string, unknown> | null;
  branch?: string | null;
}

export interface WorkflowRunDetail extends WorkflowRun {
  steps: WorkflowRunStep[];
}

export interface WorkflowRunListParams {
  workflow_id?: string;
  status?: string;
}

/** One employer card of GET /workflows/brief — the Payroll Morning Brief. Money is decimal strings. */
export interface MorningBriefContractor { contractor_name: string; amount: string; currency: string }
export interface MorningBriefItem {
  workflow_run_id: string;
  workflow_name: string;
  /** Payroll run id — approve via the existing api.decide(run_id, "approve") endpoint (POST /runs/{id}/decision). */
  run_id: string;
  employer_id: string;
  employer_name: string;
  pay_date: string;
  total_required: string;
  balance: string;
  shortfall: string;
  currency: string;
  will_pay: MorningBriefContractor[];
  will_hold: MorningBriefContractor[];
  /** "ALL_OR_NOTHING" | "PARTIAL" — set per employer via API/seed; never edited in the UI. */
  settlement_mode?: "ALL_OR_NOTHING" | "PARTIAL" | string;
  /** Payroll run status; may be "PARTIALLY_SETTLED" when part of the run already settled. */
  run_status?: string;
}

function workflowRunsQuery(params?: WorkflowRunListParams): string {
  const q = new URLSearchParams();
  if (params?.workflow_id) q.set("workflow_id", params.workflow_id);
  if (params?.status) q.set("status", params.status);
  const s = q.toString();
  return s ? `?${s}` : "";
}

// ------------------------------------------------------------------ workflow calls
export const apiWorkflows = {
  list: () => request<WorkflowSummary[]>("/workflows"),
  create: (body: WorkflowCreateIn) => post<WorkflowDetail>("/workflows", body),
  get: (id: string) => request<WorkflowDetail>(`/workflows/${encodeURIComponent(id)}`),
  update: (id: string, body: WorkflowUpdateIn) =>
    request<WorkflowDetail>(`/workflows/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  remove: (id: string) => request<void>(`/workflows/${encodeURIComponent(id)}`, { method: "DELETE" }),
  runs: (params?: WorkflowRunListParams) =>
    request<WorkflowRun[]>(`/workflows/runs${workflowRunsQuery(params)}`),
  run: (runId: string) => request<WorkflowRunDetail>(`/workflows/runs/${encodeURIComponent(runId)}`),
  brief: () => request<MorningBriefItem[]>("/workflows/brief"),
};

// ------------------------------------------------------------------ VIC calls
export const apiBenefits = {
  vicStatus: () => request<VicStatus>("/benefits/status"),
  budgets: () => request<BenefitBudget[]>("/benefits/budgets"),
  updateBudget: (employerId: string, body: BenefitBudgetIn) =>
    request<BenefitBudget>(`/benefits/budgets/${encodeURIComponent(employerId)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  requests: (params?: BenefitListParams) =>
    request<BenefitRequest[]>(`/benefits/requests${benefitsQuery(params)}`),
  createRequest: (body: BenefitRequestIn) => post<BenefitRequest>("/benefits/requests", body),
  decide: (id: string, body: BenefitDecisionIn) =>
    post<BenefitRequest>(`/benefits/requests/${encodeURIComponent(id)}/decision`, body),
  /** Returns 200 even when the attempt FAILED — always inspect `.status`. */
  purchase: (id: string) =>
    post<BenefitPurchase>(`/benefits/requests/${encodeURIComponent(id)}/purchase`),
  purchases: (params?: BenefitListParams) =>
    request<BenefitPurchase[]>(`/benefits/purchases${benefitsQuery(params)}`),
};

// ==================================================================
// Spend lane — VIC spend categories beyond Benefits: Onboarding kits
// and Travel & Accommodation. Appended as its own delimited section,
// like the lanes above. Both categories execute through the same Visa
// VIC (Airi) path as Benefits and share its status shape (VicStatus),
// its {code, message} 409 refusals, and its purchase row shape
// (SpendPurchase mirrors BenefitPurchase). All money fields are
// decimal strings; every id is a string.
// ==================================================================

// ------------------------------------------------------------------ shared spend types
/** One purchase attempt in any VIC spend category: same fields as BenefitPurchase, plus the parent refs each category uses. */
export interface SpendPurchase {
  id: string;
  /** Benefits-style parent ref, when the backend provides one. */
  request_id?: string;
  kit_id?: string | null;
  item_id?: string | null;
  trip_id?: string | null;
  component_id?: string | null;
  employer_id: string;
  attempt_no: number;
  mode: "SIMULATED" | "VIC";
  credential_ref: string;
  provider_ref: string | null;
  status: "SUCCEEDED" | "FAILED";
  failure_code: string | null;
  failure_detail: string | null;
  amount: string;
  currency: string;
  journal_txn_id: number | null;
  created_at: string;
}

/**
 * POST .../purchase-all is contracted as a {purchased, failed} summary,
 * but callers normalise defensively: a bare array of purchases is also
 * accepted and split on each row's status.
 */
export type SpendPurchaseAllResult =
  | { purchased: SpendPurchase[]; failed: SpendPurchase[] }
  | SpendPurchase[];

export function normalizePurchaseAll(result: SpendPurchaseAllResult): { purchased: SpendPurchase[]; failed: SpendPurchase[] } {
  if (Array.isArray(result)) {
    return {
      purchased: result.filter((p) => p.status === "SUCCEEDED"),
      failed: result.filter((p) => p.status !== "SUCCEEDED"),
    };
  }
  return { purchased: result.purchased ?? [], failed: result.failed ?? [] };
}

export interface SpendListParams {
  employer_id?: string;
  status?: string;
}

function spendQuery(params?: SpendListParams): string {
  const q = new URLSearchParams();
  if (params?.employer_id) q.set("employer_id", params.employer_id);
  if (params?.status) q.set("status", params.status);
  const s = q.toString();
  return s ? `?${s}` : "";
}

// ------------------------------------------------------------------ onboarding types
/** One employer's onboarding budget: GET /onboarding/budgets[]. Same base shape as BenefitBudget. */
export interface OnboardingBudget {
  id: string;
  employer_id: string;
  employer_name: string;
  currency: string;
  monthly_cap: string;
  per_tx_limit?: string | null;
  spent_this_period: string;
  /** May be absent on older payloads — pages derive it from cap minus spent. */
  remaining?: string;
  period: string;
  enabled: boolean;
  /** True while payroll for this employer is held — wages come first. Mirrors BenefitBudget. */
  locked?: boolean;
  lock_reason?: string | null;
}

export interface OnboardingBudgetIn {
  monthly_cap: string;
  per_tx_limit?: string | null;
  enabled: boolean;
}

export type OnboardingKitStatus = "DRAFT" | "APPROVED" | "DENIED" | "PARTIALLY_PURCHASED" | "COMPLETED";
export type OnboardingItemStatus = "PENDING" | "PURCHASED" | "FAILED";

export interface OnboardingItem {
  id: string;
  kind: string;
  label: string;
  merchant: string;
  amount: string;
  currency: string;
  status: OnboardingItemStatus;
  last_failure_code: string | null;
  /** Optional extras the backend may include; read defensively. */
  failure_detail?: string | null;
  latest_purchase?: SpendPurchase | null;
}

/** How a kit or trip came to exist: typed by hand, proposed in Silo chat, or drafted by a workflow. */
export type SpendSource = "MANUAL" | "CHAT" | "WORKFLOW";

export interface OnboardingKit {
  id: string;
  employer_id: string;
  employer_name: string;
  contractor_id: string | null;
  contractor_name: string;
  status: OnboardingKitStatus;
  source: SpendSource;
  /** Why the workflow drafted this kit (set on WORKFLOW kits); null otherwise. */
  draft_reason: string | null;
  total: string;
  currency: string;
  approval_expires_at: string | null;
  approval_hash?: string | null;
  requested_by_email?: string | null;
  decided_by_email?: string | null;
  decided_at?: string | null;
  deny_reason?: string | null;
  created_at?: string;
  items: OnboardingItem[];
}

export interface OnboardingItemIn {
  kind: string;
  label: string;
  merchant: string;
  amount: string;
  currency: string;
}

export interface OnboardingKitIn {
  employer_id: string;
  contractor_id: string;
  items: OnboardingItemIn[];
}

export interface OnboardingDecisionIn {
  decision: "approve" | "deny";
  reason?: string;
}

// ------------------------------------------------------------------ onboarding calls
export const apiOnboarding = {
  status: () => request<VicStatus>("/onboarding/status"),
  budgets: () => request<OnboardingBudget[]>("/onboarding/budgets"),
  updateBudget: (employerId: string, body: OnboardingBudgetIn) =>
    request<OnboardingBudget>(`/onboarding/budgets/${encodeURIComponent(employerId)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  kits: (params?: SpendListParams) =>
    request<OnboardingKit[]>(`/onboarding/kits${spendQuery(params)}`),
  createKit: (body: OnboardingKitIn) => post<OnboardingKit>("/onboarding/kits", body),
  decide: (id: string, body: OnboardingDecisionIn) =>
    post<OnboardingKit>(`/onboarding/kits/${encodeURIComponent(id)}/decision`, body),
  /** Returns 200 even when the attempt FAILED — always inspect `.status`. */
  purchaseItem: (kitId: string, itemId: string) =>
    post<SpendPurchase>(`/onboarding/kits/${encodeURIComponent(kitId)}/items/${encodeURIComponent(itemId)}/purchase`),
  purchaseAll: (kitId: string) =>
    post<SpendPurchaseAllResult>(`/onboarding/kits/${encodeURIComponent(kitId)}/purchase-all`),
  purchases: (params?: SpendListParams) =>
    request<SpendPurchase[]>(`/onboarding/purchases${spendQuery(params)}`),
};

// ------------------------------------------------------------------ travel types
/** One employer's travel budget: GET /travel/budgets[]. Onboarding budget shape plus the four category caps. */
export interface TravelBudget {
  id: string;
  employer_id: string;
  employer_name: string;
  currency: string;
  monthly_cap: string;
  per_tx_limit?: string | null;
  spent_this_period: string;
  remaining?: string;
  period: string;
  enabled: boolean;
  locked?: boolean;
  lock_reason?: string | null;
  cap_transport: string | null;
  cap_lodging: string | null;
  cap_per_diem: string | null;
  cap_other: string | null;
}

export interface TravelBudgetIn {
  monthly_cap: string;
  per_tx_limit?: string | null;
  enabled: boolean;
  cap_transport: string | null;
  cap_lodging: string | null;
  cap_per_diem: string | null;
  cap_other: string | null;
}

export type TravelTripStatus = "REQUESTED" | "APPROVED" | "DENIED" | "PARTIALLY_PURCHASED" | "COMPLETED";
export type TravelComponentKind = "TRANSPORT" | "LODGING" | "PER_DIEM" | "OTHER";
export type TravelComponentStatus = "PENDING" | "PURCHASED" | "FAILED";

export interface TravelComponent {
  id: string;
  kind: TravelComponentKind;
  label: string;
  merchant: string;
  amount: string;
  currency: string;
  status: TravelComponentStatus;
  /** True when the amount sits above the employer's cap for this kind. */
  over_cap: boolean;
  /** The cap for this kind as a decimal string, or null when no cap applies. */
  cap: string | null;
  /** True when the amount exceeds the credential's per-transaction limit, so it can never execute on the current credential. */
  tx_limit_blocked: boolean;
  last_failure_code: string | null;
  failure_detail?: string | null;
  latest_purchase?: SpendPurchase | null;
}

export interface TravelTrip {
  id: string;
  employer_id: string;
  employer_name: string;
  contractor_id: string | null;
  contractor_name: string;
  source: SpendSource;
  /** Plain-English policy read of this trip from the backend: cap breaches and the credential-limit block, in words. */
  policy_summary: string | null;
  destination: string;
  start_date: string;
  end_date: string;
  purpose: string;
  status: TravelTripStatus;
  /** True when a decider approved this trip with a cap override. */
  caps_overridden: boolean;
  total: string;
  currency?: string;
  approval_expires_at?: string | null;
  approval_hash?: string | null;
  requested_by_email?: string | null;
  decided_by_email?: string | null;
  decided_at?: string | null;
  deny_reason?: string | null;
  created_at?: string;
  components: TravelComponent[];
}

export interface TravelComponentIn {
  kind: TravelComponentKind;
  label: string;
  merchant: string;
  amount: string;
  currency: string;
}

export interface TravelTripIn {
  employer_id: string;
  contractor_id: string;
  destination: string;
  start_date: string;
  end_date: string;
  purpose: string;
  components: TravelComponentIn[];
}

export interface TravelDecisionIn {
  decision: "approve" | "deny";
  override_caps?: boolean;
  reason?: string;
}

// ------------------------------------------------------------------ travel calls
export const apiTravel = {
  status: () => request<VicStatus>("/travel/status"),
  budgets: () => request<TravelBudget[]>("/travel/budgets"),
  updateBudget: (employerId: string, body: TravelBudgetIn) =>
    request<TravelBudget>(`/travel/budgets/${encodeURIComponent(employerId)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  trips: (params?: SpendListParams) =>
    request<TravelTrip[]>(`/travel/trips${spendQuery(params)}`),
  createTrip: (body: TravelTripIn) => post<TravelTrip>("/travel/trips", body),
  decide: (id: string, body: TravelDecisionIn) =>
    post<TravelTrip>(`/travel/trips/${encodeURIComponent(id)}/decision`, body),
  /** Returns 200 even when the attempt FAILED — always inspect `.status`. */
  purchaseComponent: (tripId: string, componentId: string) =>
    post<SpendPurchase>(`/travel/trips/${encodeURIComponent(tripId)}/components/${encodeURIComponent(componentId)}/purchase`),
  purchaseAll: (tripId: string) =>
    post<SpendPurchaseAllResult>(`/travel/trips/${encodeURIComponent(tripId)}/purchase-all`),
  purchases: (params?: SpendListParams) =>
    request<SpendPurchase[]>(`/travel/purchases${spendQuery(params)}`),
};

// ==================================================================
// Email lane — Outbox. Appended as its own delimited section.
// Read-only: PaySilo records every outbound message in the outbox;
// on deployments without a configured mail transport the rows are
// recorded and NOT sent, and the UI says so plainly.
// ==================================================================

// ------------------------------------------------------------------ email types
export type EmailStatus = "QUEUED" | "SENT" | "FAILED" | "NOT_CONFIGURED";

/** One row of GET /emails/outbox. */
export interface OutboxEmail {
  id: string;
  to_address: string;
  /** Template key, e.g. "onboarding_kit_approved" — the UI humanizes it for display. */
  template: string;
  subject: string;
  status: EmailStatus;
  /** Human label for the entity this message belongs to, e.g. "Onboarding kit — Marta Kowalski (Acme)". */
  related_label: string;
  created_at: string;
  sent_at: string | null;
}

/** GET /emails/status — whether this deployment can actually send. */
export interface EmailConfigStatus {
  configured: boolean;
  /** The From address mail is sent as (masked is fine); null when not configured. */
  from_address: string | null;
}

// ------------------------------------------------------------------ email calls
export const apiEmails = {
  status: () => request<EmailConfigStatus>("/emails/status"),
  outbox: () => request<OutboxEmail[]>("/emails/outbox"),
};
