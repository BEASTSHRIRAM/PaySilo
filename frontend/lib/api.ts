const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface Employer {
  key: string;
  name: string;
  contact: string;
  funding_usd: string;
  balances: Record<string, string>;
  status: string;
}

export interface DemoReport {
  scenario: string;
  employers: Record<
    string,
    {
      employer: string;
      status: string;
      assessment: {
        total_payroll: string;
        swift_fees: string;
        total_required: string;
        required_usd: string;
        available_usd: string;
        shortfall_usd: string;
        can_run: boolean;
      } | null;
      fee_charged_usd: string;
      explanation: string | null;
      balances: Record<string, string>;
    }
  >;
  cobalt: { employer: string; deposit_usd: string; balances: Record<string, string>; payroll_status: string };
  platform: { balances: Record<string, string>; fees_collected_usd: string };
  cross_tenant_guard: string;
  settlement_chain_valid: boolean;
  ledger_balanced: boolean;
  audit: string[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}

export const api = {
  health: () => request<{ status: string; mode: string }>("/health"),
  employers: () => request<Employer[]>("/employers"),
  runDemo: () => request<DemoReport>("/runs/demo", { method: "POST" }),
};
