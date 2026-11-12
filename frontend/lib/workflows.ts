// Shared workflow node registry for the Workflows canvas (WF-FE).
// ONE registry drives everything: palette entry, card rendering,
// config-form fields, labels, default data and essence lines — there
// are no per-node-type switch statements elsewhere in the UI.
// Node types are exact backend identifiers — never invent new ones, and
// never offer approve / purchase / pay nodes: every consequential step
// goes through gate.wait_for_approval instead.
// Graph JSON is the only truth; React Flow state is a pure projection
// of the saved graph (see the editor's graphToFlow / flowToGraph).

export type WorkflowNodeKind = "trigger" | "condition" | "gate" | "action";
/** Palette categories: Triggers / Logic / Actions / Human. */
export type PaletteGroup = "Triggers" | "Logic" | "Actions" | "Human";

export interface ConfigFieldOption { value: string; label: string }
export interface ConfigFieldDef {
  key: string;
  label: string;
  kind: "select" | "text" | "textarea" | "number";
  options?: ConfigFieldOption[];
  placeholder?: string;
  help?: string;
  required?: boolean;
  defaultValue?: string;
}

export interface WorkflowNodeDef {
  type: string;
  label: string;
  description: string;
  kind: WorkflowNodeKind;
  group: PaletteGroup;
  fields: ConfigFieldDef[];
  /** One-line configured essence shown on the card — the flow must read without opening panels. */
  summary: (data: Record<string, unknown>) => string | null;
}

const str = (v: unknown) => (typeof v === "string" ? v : v === null || v === undefined ? "" : String(v));
const trunc = (s: string, n = 42) => (s.length > n ? `${s.slice(0, n)}…` : s);
const cap = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

// ------------------------------------------------------------------ config vocabularies
export const FIELD_OPTIONS: ConfigFieldOption[] = ["amount", "status", "employer_name", "currency", "destination"].map((v) => ({ value: v, label: v }));
export const OPERATOR_OPTIONS: ConfigFieldOption[] = [
  { value: "eq", label: "equals" }, { value: "neq", label: "does not equal" },
  { value: "gt", label: "greater than" }, { value: "gte", label: "greater than or equal" },
  { value: "lt", label: "less than" }, { value: "lte", label: "less than or equal" },
  { value: "contains", label: "contains" },
];
export const OPERATOR_LABELS: Record<string, string> = Object.fromEntries(OPERATOR_OPTIONS.map((o) => [o.value, o.label]));
export const OPERATOR_SYMBOLS: Record<string, string> = { eq: "=", neq: "!=", gt: ">", gte: ">=", lt: "<", lte: "<=", contains: "contains" };
export const EMAIL_TEMPLATES: ConfigFieldOption[] = [
  { value: "approval_requested", label: "Approval requested" }, { value: "kit_completed", label: "Kit completed" },
  { value: "trip_itinerary", label: "Trip itinerary" }, { value: "purchase_receipt", label: "Purchase receipt" },
  { value: "payroll_held_alert", label: "Payroll held alert" }, { value: "employer_welcome", label: "Employer welcome" },
];
export const EMAIL_RECIPIENTS: ConfigFieldOption[] = [
  { value: "decider", label: "Decider" }, { value: "contractor", label: "Contractor" },
  { value: "employer_contact", label: "Employer contact" }, { value: "operator", label: "Operator" },
];
export const GATE_ENTITIES: ConfigFieldOption[] = [
  { value: "trigger", label: "Triggering entity" }, { value: "kit", label: "Onboarding kit" }, { value: "trip", label: "Trip" },
];
export const SCHEDULE_CADENCES: ConfigFieldOption[] = [{ value: "monthly", label: "Monthly" }, { value: "weekly", label: "Weekly" }];
export const SCHEDULE_WEEKDAYS: ConfigFieldOption[] = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].map((d) => ({ value: d, label: cap(d) }));
export const FAN_OUT_OPTIONS: ConfigFieldOption[] = [{ value: "employers", label: "Per employer" }, { value: "org", label: "Whole organisation" }];

export function templateLabel(v: string): string { return EMAIL_TEMPLATES.find((t) => t.value === v)?.label ?? v; }
export function recipientLabel(v: string): string { return EMAIL_RECIPIENTS.find((t) => t.value === v)?.label ?? v; }

function scheduleSummary(data: Record<string, unknown>): string {
  const cadence = str(data.cadence) || "monthly";
  const fan = str(data.fan_out) === "org" ? "whole organisation" : "per employer";
  if (cadence === "weekly") {
    const day = str(data.weekday) || str(data.day) || "friday";
    return `Weekly on ${cap(day)} · ${fan}`;
  }
  const day = str(data.day) || "1";
  return `Monthly on day ${day} · ${fan}`;
}

// ------------------------------------------------------------------ THE registry
export const WORKFLOW_NODE_DEFS: WorkflowNodeDef[] = [
  // Triggers — group "Triggers"
  { type: "trigger.employer_added", label: "Employer added", description: "Runs when a new employer is added to the organisation.", kind: "trigger", group: "Triggers", fields: [], summary: () => "When an employer is added" },
  { type: "trigger.contractor_joined", label: "Contractor joined", description: "Runs when a contractor joins an employer.", kind: "trigger", group: "Triggers", fields: [], summary: () => "When a contractor joins" },
  { type: "trigger.payroll_held", label: "Payroll held", description: "Runs when a payroll run is held (shortfall or human hold).", kind: "trigger", group: "Triggers", fields: [], summary: () => "When payroll is held" },
  { type: "trigger.payroll_settled", label: "Payroll settled", description: "Runs when a payroll run settles successfully.", kind: "trigger", group: "Triggers", fields: [], summary: () => "When payroll settles" },
  { type: "trigger.benefit_requested", label: "Benefit requested", description: "Runs when a benefit request is created.", kind: "trigger", group: "Triggers", fields: [], summary: () => "When a benefit is requested" },
  { type: "trigger.trip_proposed", label: "Trip proposed", description: "Runs when a travel trip is proposed.", kind: "trigger", group: "Triggers", fields: [], summary: () => "When a trip is proposed" },
  { type: "trigger.kit_approved", label: "Kit approved", description: "Runs when an onboarding kit is approved.", kind: "trigger", group: "Triggers", fields: [], summary: () => "When a kit is approved" },
  { type: "trigger.kit_completed", label: "Kit completed", description: "Runs when an onboarding kit is completed.", kind: "trigger", group: "Triggers", fields: [], summary: () => "When a kit is completed" },
  {
    type: "trigger.schedule", label: "Schedule", description: "Runs on a monthly or weekly cadence, per employer or for the whole organisation.", kind: "trigger", group: "Triggers",
    fields: [
      { key: "cadence", label: "Cadence", kind: "select", options: SCHEDULE_CADENCES, defaultValue: "monthly", required: true },
      { key: "day", label: "Day of month (monthly)", kind: "number", placeholder: "3", defaultValue: "1", help: "Used when the cadence is monthly." },
      { key: "weekday", label: "Weekday (weekly)", kind: "select", options: SCHEDULE_WEEKDAYS, defaultValue: "friday", help: "Used when the cadence is weekly." },
      { key: "fan_out", label: "Fan out", kind: "select", options: FAN_OUT_OPTIONS, defaultValue: "employers", required: true },
    ],
    summary: scheduleSummary,
  },
  { type: "trigger.funds_received", label: "Funds received", description: "Runs when funds land in an employer's silo.", kind: "trigger", group: "Triggers", fields: [], summary: () => "When funds land in a silo" },
  // Logic — condition
  {
    type: "condition.field_check", label: "Field check", description: "Branches on a field of the triggering event: follow the true or false handle.", kind: "condition", group: "Logic",
    fields: [
      { key: "field", label: "Field", kind: "select", options: FIELD_OPTIONS, defaultValue: "amount", required: true },
      { key: "operator", label: "Operator", kind: "select", options: OPERATOR_OPTIONS, defaultValue: "gt", required: true },
      { key: "value", label: "Value", kind: "text", placeholder: "200", required: true },
      { key: "currency", label: "Currency (optional, for amounts)", kind: "text", placeholder: "USD" },
    ],
    summary: (d) => {
      const f = str(d.field); if (!f) return null;
      const sym = OPERATOR_SYMBOLS[str(d.operator)] ?? OPERATOR_LABELS[str(d.operator)] ?? str(d.operator);
      const v = str(d.value); const cur = str(d.currency);
      const rhs = v ? (cur ? `${cur} ${v}` : v) : "…";
      return `${cap(f)} ${sym} ${rhs}`;
    },
  },
  // Human — gate
  {
    type: "gate.wait_for_approval", label: "Wait for approval", description: "Pauses the run until a human approves. The run shows as Waiting until then.", kind: "gate", group: "Human",
    fields: [{ key: "entity", label: "Waiting on", kind: "select", options: GATE_ENTITIES, defaultValue: "trigger", required: true }],
    summary: (d) => {
      const e = str(d.entity);
      if (e === "kit") return "Kit approval";
      if (e === "trip") return "Trip approval";
      return "Decider approval";
    },
  },
  // Actions
  { type: "action.draft_onboarding_kit", label: "Draft onboarding kit", description: "Drafts an onboarding kit for the contractor in the triggering event.", kind: "action", group: "Actions", fields: [], summary: () => "Standard template" },
  { type: "action.create_approval_request", label: "Create approval request", description: "Creates an approval request for a human to decide.", kind: "action", group: "Actions", fields: [], summary: () => "Approval request for a decider" },
  {
    type: "action.send_email", label: "Send email", description: "Sends one of the standard email templates to a chosen recipient.", kind: "action", group: "Actions",
    fields: [
      { key: "template", label: "Template", kind: "select", options: EMAIL_TEMPLATES, defaultValue: "approval_requested", required: true },
      { key: "recipient", label: "Recipient", kind: "select", options: EMAIL_RECIPIENTS, defaultValue: "decider", required: true },
    ],
    summary: (d) => `${templateLabel(str(d.template) || "approval_requested")} · to ${recipientLabel(str(d.recipient) || "decider")}`,
  },
  {
    type: "action.create_exception", label: "Create exception", description: "Raises an exception in the Exceptions inbox for a human to handle.", kind: "action", group: "Actions",
    fields: [
      { key: "title", label: "Exception title", kind: "text", placeholder: "Payroll held — needs review", required: true },
      { key: "detail", label: "Detail", kind: "textarea", placeholder: "What a human should check" },
    ],
    summary: (d) => str(d.title) || "New exception",
  },
  {
    type: "action.post_chat_notice", label: "Post chat notice", description: "Posts a notice message to the Silo chat.", kind: "action", group: "Actions",
    fields: [{ key: "message", label: "Message", kind: "textarea", placeholder: "Notice to post in the Silo chat", required: true }],
    summary: (d) => (str(d.message) ? trunc(str(d.message)) : "Chat notice"),
  },
  {
    type: "action.create_funding_reminder", label: "Create funding reminder", description: "Creates a funding reminder for the employer in the triggering event.", kind: "action", group: "Actions",
    fields: [{ key: "note", label: "Note", kind: "textarea", placeholder: "Note attached to the funding reminder" }],
    summary: (d) => (str(d.note) ? trunc(str(d.note)) : "Funding reminder"),
  },
  { type: "action.prepare_payroll_run", label: "Prepare payroll run", description: "Prepares a payroll run from intake and pricing only — it never approves or pays.", kind: "action", group: "Actions", fields: [], summary: () => "Intake + pricing only" },
  { type: "action.revalidate_held_run", label: "Revalidate held run", description: "Re-checks whether a held payroll run is now affordable.", kind: "action", group: "Actions", fields: [], summary: () => "Re-check affordability" },
];

export const WORKFLOW_NODE_BY_TYPE: Record<string, WorkflowNodeDef> = Object.fromEntries(WORKFLOW_NODE_DEFS.map((d) => [d.type, d]));
export const TRIGGER_TYPES = WORKFLOW_NODE_DEFS.filter((d) => d.kind === "trigger").map((d) => d.type);
export const PALETTE_GROUPS: PaletteGroup[] = ["Triggers", "Logic", "Human", "Actions"];
/** Legacy group names from the original brief, mapped to palette groups. */
export const GROUP_LABELS: Record<PaletteGroup, string> = { Triggers: "Triggers", Logic: "Logic (Conditions)", Human: "Human (Gates)", Actions: "Actions" };

export function nodeDef(type: string): WorkflowNodeDef {
  const found = WORKFLOW_NODE_BY_TYPE[type];
  if (found) return found;
  const kind: WorkflowNodeKind = type.startsWith("trigger.") ? "trigger" : type.startsWith("condition.") ? "condition" : type.startsWith("gate.") ? "gate" : "action";
  return { type, label: type, description: "Unknown node type — rendered as saved.", kind, group: kind === "trigger" ? "Triggers" : kind === "condition" ? "Logic" : kind === "gate" ? "Human" : "Actions", fields: [], summary: () => null };
}
export function isKnownNodeType(type: string): boolean { return type in WORKFLOW_NODE_BY_TYPE; }
export function humanizeTrigger(type: string | null | undefined): string { if (!type) return "—"; return nodeDef(type).label; }

/** Default config derived from the registry field schema — no per-type switch. */
export function defaultDataFor(type: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of nodeDef(type).fields) if (f.defaultValue !== undefined) out[f.key] = f.defaultValue;
  return out;
}
export function configSummary(type: string, data: Record<string, unknown> | undefined): string | null {
  return nodeDef(type).summary(data ?? {});
}
/** Inline validation from the registry schema: returns key -> message for missing required fields. */
export function validateNodeData(type: string, data: Record<string, unknown> | undefined): Record<string, string> {
  const errs: Record<string, string> = {};
  for (const f of nodeDef(type).fields) {
    if (!f.required) continue;
    const v = data?.[f.key];
    if (v === undefined || v === null || (typeof v === "string" && v.trim() === "")) errs[f.key] = `${f.label} is required.`;
  }
  return errs;
}

export function kindChipClass(kind: WorkflowNodeKind): string {
  switch (kind) {
    case "trigger": return "bg-[#90AEAD] text-[#11262E]";
    case "condition": return "bg-[#F3D7B0] text-[#563129]";
    case "gate": return "bg-[#E5BF8C] text-[#563129]";
    case "action": return "bg-[#244855] text-[#FFF4E4]";
  }
}
export function kindLabel(kind: WorkflowNodeKind): string {
  switch (kind) { case "trigger": return "Trigger"; case "condition": return "Condition"; case "gate": return "Gate"; case "action": return "Action"; }
}
export function kindMiniMapColor(kind: WorkflowNodeKind): string {
  switch (kind) { case "trigger": return "#90AEAD"; case "condition": return "#E5BF8C"; case "gate": return "#F2917F"; case "action": return "#244855"; }
}
export function newNodeId(type: string): string {
  const slug = type.replace(/[^a-z0-9]+/gi, "_");
  return `${slug}_${Math.random().toString(36).slice(2, 8)}${Date.now().toString(36).slice(-4)}`;
}
/** Strip editor/run-only keys (prefixed __) before saving a graph. */
export function cleanNodeData(data: Record<string, unknown> | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data ?? {})) if (!k.startsWith("__")) out[k] = v;
  return out;
}
export const ADD_NEXT_EVENT = "paysilo:add-next";
export interface AddNextDetail { sourceId: string; sourceHandle: string | null }
