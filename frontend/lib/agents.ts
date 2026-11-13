import {
  Anchor, Ban, Calculator, Cloud, FileText, ListChecks, PauseCircle, Receipt, Scale, Send, ShieldCheck, TriangleAlert, UserCheck,
  type LucideIcon,
} from "lucide-react";

/** One registry for every agent shown on a payroll run (mirrors AGENTS in backend/app/agents/graph.py). */
export type AgentKind = "llm" | "code" | "human" | "partner";

export interface AgentInfo {
  id: string;
  name: string;
  /** One line: what this agent does. */
  job: string;
  icon: LucideIcon;
  kind: AgentKind;
  /** Outside system this agent touches, shown as a small tag. */
  partner?: string;
}

export const AGENTS: Record<string, AgentInfo> = {
  intake: { id: "intake", name: "Intake", job: "Reads the payroll sheet or email into pay lines.", icon: FileText, kind: "llm" },
  risk: { id: "risk", name: "Risk Gate", job: "Holds lines with unverified or recently changed bank details.", icon: ShieldCheck, kind: "code" },
  price: { id: "price", name: "Pricing", job: "Prices salaries and payout fees before any FX.", icon: Calculator, kind: "code" },
  bedrock: { id: "bedrock", name: "Bedrock AgentCore", job: "Re computes the pricing plan on AWS and must match to the cent.", icon: Cloud, kind: "partner", partner: "Bedrock AgentCore" },
  assess: { id: "assess", name: "Balance & Policy", job: "Checks only this employer's own wallet, run fee and reserve.", icon: Scale, kind: "code" },
  approval: { id: "approval", name: "Approval Gate", job: "Waits for a signed human decision on the exact amounts.", icon: UserCheck, kind: "human", partner: "Visa TAP style signature" },
  escalate: { id: "escalate", name: "Escalation", job: "Holds short or risky lines and asks a human.", icon: TriangleAlert, kind: "human" },
  held: { id: "held", name: "Held by a person", job: "A person chose to hold this payroll.", icon: PauseCircle, kind: "human" },
  refused: { id: "refused", name: "Refused", job: "Policy stopped this run before any money moved.", icon: Ban, kind: "code" },
  execute: { id: "execute", name: "FX & Payout", job: "Converts and pays contractors from the employer's wallet.", icon: Send, kind: "partner", partner: "Airwallex" },
  fee: { id: "fee", name: "Fee", job: "Collects the run fee only when the payroll ran.", icon: Receipt, kind: "partner", partner: "Airwallex" },
  reconcile: { id: "reconcile", name: "Reconciliation", job: "Proves the ledger matches the wallet.", icon: ListChecks, kind: "code" },
  attest: { id: "attest", name: "Attestation", job: "Anchors a hash of the receipt on-chain.", icon: Anchor, kind: "partner", partner: "Metal" },
};

export function agentName(id: string): string {
  return AGENTS[id]?.name ?? id;
}
