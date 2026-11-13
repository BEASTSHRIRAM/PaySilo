/**
 * Pure derivation of the agent pipeline from a run's events + status. No runtime imports, so it can be
 * checked with plain node (scripts/agent-flow.check.mjs). Display data (names, icons) lives in lib/agents.ts.
 */
export interface FlowEvent { seq?: number; agent: string; level: string; message: string; created_at?: string | null }

export type NodeState = "done" | "active" | "waiting" | "warning" | "failed" | "skipped" | "upcoming" | "off";

export interface FlowNode {
  id: string;
  state: NodeState;
  /** Number of events (steps) recorded for this agent. */
  count: number;
  /** For side branches: the main node they hang off. */
  parent?: string;
  /** Short facts parsed from events (Bedrock region + ms). */
  detail?: string;
}

export interface FlowSummary {
  /** Milliseconds between the first and last event, or null without timestamps. */
  elapsedMs: number | null;
  agentsRan: number;
  humanDecisions: number;
  systems: string[];
}

export interface Flow {
  main: FlowNode[];
  branches: FlowNode[];
  summary: FlowSummary;
  /** Agent id the run is currently working on or waiting at, if any. */
  currentId: string | null;
}

export const MAIN_ORDER = ["intake", "risk", "price", "assess", "approval", "execute", "fee", "reconcile", "attest"] as const;

/** Statuses after which a run no longer changes without a new human action (mirrors lib/status.ts). */
const SETTLED_STATUSES = new Set([
  "SETTLED", "PARTIALLY_PAID", "SENT_FEE_FAILED", "SENT_UNRECONCILED", "ATTEST_PENDING",
  "FAILED", "REFUSED", "APPROVAL_REJECTED", "HELD_SHORTFALL", "HELD_BY_HUMAN",
]);

export function parseBedrock(message: string): { region: string; ms: string } | null {
  const m = /\(([A-Za-z0-9-]+), session [^,)]*, ([\d.?]+) ms\)/.exec(message);
  return m ? { region: m[1], ms: m[2] } : null;
}

export function eventMs(e: { created_at?: string | null }): number | null {
  if (!e.created_at) return null;
  const t = Date.parse(e.created_at);
  return Number.isNaN(t) ? null : t;
}

function stateOf(evs: FlowEvent[]): NodeState {
  if (evs.some((e) => e.level === "error")) return "failed";
  if (evs.some((e) => e.level === "warning")) return "warning";
  return "done";
}

export function deriveFlow(
  events: FlowEvent[],
  runStatus: string,
  opts: { pendingKind?: "approval" | "escalation" | null } = {},
): Flow {
  const by: Record<string, FlowEvent[]> = {};
  for (const e of events) (by[e.agent] ??= []).push(e);
  const has = (id: string) => (by[id]?.length ?? 0) > 0;
  const live = !SETTLED_STATUSES.has(runStatus);
  const pendingKind = opts.pendingKind ?? null;
  const approvalWaiting = !has("approval") && (pendingKind === "approval" || (pendingKind === null && runStatus === "RESERVED"));
  const escalateWaiting = pendingKind === "escalation";
  const humanBlock = approvalWaiting || escalateWaiting;

  // Last main node that has events (the frontier of progress).
  let lastIdx = -1;
  MAIN_ORDER.forEach((id, i) => { if (has(id)) lastIdx = i; });

  let currentId: string | null = null;
  const main: FlowNode[] = MAIN_ORDER.map((id, i) => {
    const count = by[id]?.length ?? 0;
    let state: NodeState;
    if (count > 0) state = stateOf(by[id]);
    else if (id === "approval" && approvalWaiting) state = "waiting";
    else if (live && !humanBlock && i === lastIdx + 1) state = "active";
    else if (live && (humanBlock || i > lastIdx)) state = "upcoming";
    else state = "skipped";
    if (state === "active" || state === "waiting") currentId = id;
    return { id, state, count };
  });
  // The risk gate logs nothing when it finds nothing: if the run got past it, it passed.
  const riskNode = main[MAIN_ORDER.indexOf("risk")];
  if (riskNode.count === 0 && lastIdx > MAIN_ORDER.indexOf("risk")) riskNode.state = "done";

  const branches: FlowNode[] = [];
  const bed = by.bedrock ?? [];
  const priceDone = has("price") || lastIdx > MAIN_ORDER.indexOf("price");
  if (bed.length > 0) {
    const parsed = [...bed].reverse().map((e) => parseBedrock(e.message)).find(Boolean) ?? null;
    branches.push({
      id: "bedrock", state: stateOf(bed), count: bed.length, parent: "price",
      detail: parsed ? `${parsed.region}, ${parsed.ms} ms` : undefined,
    });
  } else {
    branches.push({ id: "bedrock", state: priceDone ? "off" : "upcoming", count: 0, parent: "price" });
  }

  if (has("escalate") || escalateWaiting) {
    const evs = by.escalate ?? [];
    branches.push({
      id: "escalate", count: evs.length, parent: "assess",
      state: escalateWaiting ? "waiting" : evs.some((e) => e.level === "error") ? "failed" : "warning",
    });
    if (escalateWaiting) currentId = "escalate";
  }
  if (runStatus === "HELD_BY_HUMAN") branches.push({ id: "held", state: "warning", count: 0, parent: "approval" });
  if (runStatus === "REFUSED" || runStatus === "APPROVAL_REJECTED") {
    const failedMain = main.find((n) => n.state === "failed");
    branches.push({ id: "refused", state: "failed", count: 0, parent: failedMain?.id ?? (bed.some((e) => e.level === "error") ? "price" : "intake") });
  }

  const times = events.map(eventMs).filter((t): t is number => t !== null);
  const elapsedMs = times.length >= 1 ? Math.max(...times) - Math.min(...times) : null;
  const agentsRan = [...main, ...branches].filter((n) => n.count > 0).length;
  const humanDecisions = events.filter((e) =>
    (e.agent === "approval" && /decided/i.test(e.message)) || (e.agent === "escalate" && /acknowledged/i.test(e.message))).length;
  const systems: string[] = [];
  if (has("execute") || has("fee")) systems.push("Airwallex");
  if (bed.length > 0) systems.push("Bedrock");
  if (has("attest") && by.attest.some((e) => e.level !== "error")) systems.push("Metal");

  return { main, branches, summary: { elapsedMs, agentsRan, humanDecisions, systems }, currentId };
}

export function formatElapsed(ms: number | null): string {
  if (ms === null) return "n/a";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  return `${Math.floor(s / 60)} m ${Math.round(s % 60)} s`;
}

/** "+3.2 s" offset of an event from the run's first event, or null without timestamps. */
export function relativeTime(events: FlowEvent[], e: FlowEvent): string | null {
  const t = eventMs(e);
  if (t === null) return null;
  const first = Math.min(...events.map(eventMs).filter((x): x is number => x !== null));
  const d = t - first;
  return d < 1000 ? `+${d} ms` : `+${(d / 1000).toFixed(1)} s`;
}
