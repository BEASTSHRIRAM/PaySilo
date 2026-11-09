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
  pay_lines: { position: number; beneficiary_name: string; amount: string; currency: string; method: string;
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

// ------------------------------------------------------------------ chat (issue #11 contract)
export interface ChatContext { employer_id?: string; run_id?: string; page?: string }
export interface ChatApproval {
  run_id: string; employer_id: string; employer_name: string; summary: string;
}
export type ChatEvent =
  | { type: "token"; text: string }
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
    case "tool":
      return { type: "tool", name: str(data.name), label: str(data.label) || str(data.name), status: str(data.status) || "running" };
    case "approval":
      return {
        type: "approval",
        approval: {
          run_id: str(data.run_id), employer_id: str(data.employer_id),
          employer_name: str(data.employer_name), summary: str(data.summary),
        },
      };
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
  login: (email: string, password: string) => post<Session>("/auth/login", { email, password }),
  signup: (payload: SignupPayload) => post<Session>("/auth/signup", payload),
  logout: () => post<void>("/auth/logout"),
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
