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
  /** Initial config for a freshly added node (merged with the field defaults). */
  defaults?: Record<string, unknown>;
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
  { value: "payslip_receipt", label: "Payslip receipt (one per paid line)" },
];
export const EMAIL_RECIPIENTS: ConfigFieldOption[] = [
  { value: "decider", label: "Decider" }, { value: "contractor", label: "Contractor" },
  { value: "employer_contact", label: "Employer contact" }, { value: "operator", label: "Operator" },
];
export const GATE_ENTITIES: ConfigFieldOption[] = [
  { value: "trigger", label: "Triggering entity" }, { value: "kit", label: "Onboarding kit" }, { value: "trip", label: "Trip" },
  { value: "run", label: "Payroll run" },
];
export const SCHEDULE_CADENCES: ConfigFieldOption[] = [
  { value: "monthly", label: "Monthly, on a day of the month" },
  { value: "weekly", label: "Weekly, on a weekday" },
  { value: "once", label: "Once, on a specific date" },
];
export const SCHEDULE_WEEKDAYS: ConfigFieldOption[] = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((d, i) => ({ value: String(i), label: d }));
export const WEEKEND_RULES: ConfigFieldOption[] = [
  { value: "as_is", label: "Run as is" },
  { value: "previous_business_day", label: "Previous business day (Friday)" },
  { value: "next_business_day", label: "Next business day (Monday)" },
];
export const FAN_OUT_OPTIONS: ConfigFieldOption[] = [{ value: "employers", label: "Per employer" }, { value: "org", label: "Whole organisation" }];
export const DEFAULT_TIMEZONE = "Asia/Kolkata";
export const COMMON_TIMEZONES = ["Asia/Kolkata", "UTC", "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney"];
export const SCHEDULE_DEFAULTS: Record<string, unknown> = { cadence: "monthly", day: 3, time: "09:00", timezone: DEFAULT_TIMEZONE, weekend_rule: "as_is", fan_out: "org" };

export function templateLabel(v: string): string { return EMAIL_TEMPLATES.find((t) => t.value === v)?.label ?? v; }
export function recipientLabel(v: string): string { return EMAIL_RECIPIENTS.find((t) => t.value === v)?.label ?? v; }

function scheduleSummary(data: Record<string, unknown>): string {
  const cadence = str(data.cadence) || "monthly";
  const day = Number(data.day);
  let when: string;
  if (cadence === "once") when = str(data.date) ? `On ${str(data.date)}` : "On a date (pick one)";
  else if (cadence === "weekly") when = `Every ${SCHEDULE_WEEKDAYS[Number.isInteger(day) && day >= 0 && day <= 6 ? day : 4].label}`;
  else if (data.last_day === true) when = "Monthly, last day";
  else when = `Monthly on day ${Number.isFinite(day) && day > 0 ? day : "?"}`;
  const time = str(data.time) ? ` · ${str(data.time)} ${str(data.timezone) || DEFAULT_TIMEZONE}` : "";
  const scope = str(data.__employerName) ? ` · ${str(data.__employerName)}` : data.employer_id ? " · one employer" : str(data.fan_out) === "employers" ? " · every employer" : " · whole organisation";
  return `${when}${time}${scope}`;
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
    type: "trigger.schedule", label: "Schedule", description: "Runs on a calendar date for one employer: monthly on a day, weekly on a weekday, or once.", kind: "trigger", group: "Triggers",
    fields: [], defaults: SCHEDULE_DEFAULTS,
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
      if (e === "run") return "Payroll run approval";
      return "Decider approval";
    },
  },
  // Actions
  { type: "action.draft_onboarding_kit", label: "Draft onboarding kit", description: "Drafts an onboarding kit for the contractor in the triggering event.", kind: "action", group: "Actions", fields: [], summary: () => "Standard template" },
  { type: "action.create_approval_request", label: "Create approval request", description: "Creates an approval request for a human to decide.", kind: "action", group: "Actions", fields: [], summary: () => "Approval request for a decider" },
  {
    type: "action.send_email", label: "Send email", description: "Queues one of the standard emails in Notifications for a chosen recipient.", kind: "action", group: "Actions",
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
  return { ...out, ...(nodeDef(type).defaults ?? {}) };
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
  if (type === "trigger.schedule") Object.assign(errs, validateScheduleData(data ?? {}));
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

/** Friendly inline checks for the Schedule trigger (the server re-checks everything and computes the next runs). */
export function validateScheduleData(data: Record<string, unknown>): Record<string, string> {
  const errs: Record<string, string> = {};
  const cadence = str(data.cadence);
  const day = Number(data.day);
  if (!["monthly", "weekly", "once"].includes(cadence)) errs.cadence = "Choose how often it runs.";
  if (cadence === "monthly" && data.last_day !== true && !(Number.isInteger(day) && day >= 1 && day <= 31)) errs.day = "Pick a day of the month from 1 to 31, or 'last day'.";
  if (cadence === "weekly" && !(Number.isInteger(day) && day >= 0 && day <= 6)) errs.day = "Pick a weekday.";
  if (cadence === "once" && !/^\d{4}-\d{2}-\d{2}$/.test(str(data.date))) errs.date = "Pick the date it should run on.";
  if (str(data.time) && !/^([01]\d|2[0-3]):[0-5]\d$/.test(str(data.time))) errs.time = "Use a 24-hour time like 09:00.";
  return errs;
}

// ------------------------------------------------------------------ connection rules (flow is LEFT -> RIGHT)
export interface HandleSpec { id: string; label: string }
/** Inputs sit on the LEFT edge, outputs on the RIGHT edge. Triggers have no input; conditions have Yes (true) / No (false) outputs. */
export function handlesFor(kind: WorkflowNodeKind): { input: boolean; outputs: HandleSpec[] } {
  if (kind === "condition") return { input: true, outputs: [{ id: "true", label: "Yes" }, { id: "false", label: "No" }] };
  return { input: kind !== "trigger", outputs: [{ id: "out", label: "" }] };
}

/** Same sentences as the server validator, so the toast and a rejected save/import agree. */
export function connectionProblem(sourceType: string, targetType: string, sourceHandle?: string | null): string | null {
  const s = nodeDef(sourceType).kind;
  const t = nodeDef(targetType).kind;
  if (t === "trigger") {
    if (s === "trigger") return "A trigger can't connect to another trigger. A workflow starts from exactly one trigger: connect the trigger to an action or condition instead, or put the second trigger in its own workflow.";
    return "Nothing can connect into a trigger: it is where the workflow starts. Connect this step to a node to the right of it instead.";
  }
  if (s === "condition" && sourceHandle && sourceHandle !== "true" && sourceHandle !== "false") return "A condition's outputs are Yes (true) and No (false).";
  return null;
}

export function wouldCreateCycle(edges: { source: string; target: string }[], source: string, target: string): boolean {
  // Adding source -> target loops iff target already reaches source.
  const next: Record<string, string[]> = {};
  for (const e of edges) (next[e.source] ??= []).push(e.target);
  const stack = [target];
  const seen = new Set<string>();
  while (stack.length) {
    const cur = stack.pop() as string;
    if (cur === source) return true;
    if (seen.has(cur)) continue;
    seen.add(cur);
    stack.push(...(next[cur] ?? []));
  }
  return false;
}
