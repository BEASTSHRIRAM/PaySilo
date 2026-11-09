/**
 * Shared state shapes for one Silo assistant turn, plus the small pure
 * helpers the thinking block and tool cards use to summarise event data.
 *
 * Everything here is built only from real SSE v2 events (live) or the
 * persisted `trace` array (history) — nothing invents steps or tools.
 */

export interface ThinkingStep {
  id: string;
  step: string;
  detail?: string;
  status: "active" | "done";
}

export interface ToolCardState {
  id: string;
  tool: string;
  label: string;
  /** Raw input exactly as the tool_start event carried it. */
  input: unknown;
  /** One-line human summary of the input, derived from `input`. */
  inputSummary: string;
  status: "running" | "done" | "error";
  /** Real result summary from tool_result; empty while running. */
  summary: string;
  /** Raw result payload from tool_result, when the backend sent one. */
  data?: unknown;
  hasData: boolean;
}

/** Short, single-line rendering of any JSON-ish value. */
export function summariseValue(value: unknown, max = 90): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return truncate(value, max);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "empty list";
    const first = summariseValue(value[0], 40);
    return value.length === 1 ? first : `${first} +${value.length - 1} more`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).filter(
      ([, v]) => v !== null && v !== undefined && v !== "",
    );
    if (entries.length === 0) return "";
    return truncate(
      entries.map(([k, v]) => `${prettifyKey(k)}: ${summariseValue(v, 32)}`).join(" · "),
      max,
    );
  }
  return truncate(String(value), max);
}

function prettifyKey(key: string): string {
  return key.replace(/_/g, " ");
}

function truncate(text: string, max: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max - 1).trimEnd()}…` : clean;
}

/** Pretty JSON for the inspect view; falls back to a plain string. */
export function formatJson(value: unknown): string {
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}
