// All onboarding-guide copy lives here. Edit freely: the engine never hard-codes a sentence.
// House style: one short sentence (`body`) plus one "why it matters" line (`why`), plain language,
// roughly 22 words per bubble. No LLM calls: this file is the whole script.
//
// `{org}` and `{first}` are replaced at runtime. Bump TOUR_VERSION in backend/app/api/routes/tour.py
// (and here) after a major product change to re-OFFER the tour to people who finished or skipped it.

export const TOUR_VERSION = 1;

export type TourRole = "owner" | "admin" | "approver" | "viewer";
export type Pose = "wave" | "idle" | "cheer" | "shy";

export type ChapterId = "setup" | "payroll" | "safety" | "extras" | "automation" | "trust";

export const CHAPTERS: { id: ChapterId; label: string; blurb: string }[] = [
  { id: "setup", label: "Set up your silos", blurb: "Navigation, employers, contractors" },
  { id: "payroll", label: "Funding and payroll", blurb: "Fund, run, approve" },
  { id: "safety", label: "Safety and proof", blurb: "Held lines, risk gate, receipts" },
  { id: "extras", label: "Benefits and travel", blurb: "Separate budgets, wages first" },
  { id: "automation", label: "Automation", blurb: "Workflows, calendar, morning brief" },
  { id: "trust", label: "Team and Silo", blurb: "Compliance, roles, chat" },
];

/** What the tour knows about the org so it can skip empty tables and use friendly sample copy. */
export interface TourContext {
  role: TourRole;
  employerCount: number;
  firstEmployerId: string | null;
}

export interface TourStep {
  id: string;
  chapter: ChapterId;
  title: string;
  body: string;
  why: string;
  pose: Pose;
  /** Route to be on. A function may return null when the data it needs does not exist yet. */
  route?: string | ((ctx: TourContext) => string | null);
  /** `data-tour` key of the element to spotlight. Missing target means a centered bubble. */
  target?: string;
  /** Extra CSS selectors tried when no `data-tour` match is visible (e.g. the page header). */
  fallback?: string[];
  side?: "top" | "right" | "bottom" | "left";
  /** Roles that see this step. Omit for everyone. */
  roles?: TourRole[];
  /** Per-role copy overrides. */
  roleCopy?: Partial<Record<TourRole, { body?: string; why?: string }>>;
  /** The step needs at least one employer to point at real data. */
  needsEmployer?: boolean;
  /** Shown instead when the org has no data yet (centered unless `target` is given). */
  empty?: { title?: string; body: string; why: string; target?: string; action?: { label: string; href: string } };
}

const PAGE_HEADER = ["[data-tour='page-header']", "main h1"];
const OWNERS: TourRole[] = ["owner", "admin"];
const DECIDERS: TourRole[] = ["owner", "admin", "approver"];

export const TOUR_STEPS: TourStep[] = [
  {
    id: "sidebar", chapter: "setup", pose: "wave", route: "/dashboard", target: "sidebar", side: "right",
    title: "Your map",
    body: "Everything is grouped the way money moves, from payroll to proof.",
    why: "Any task is two clicks away.",
  },
  {
    id: "employers", chapter: "setup", pose: "idle", route: "/dashboard", target: "employers", side: "top",
    title: "Employers",
    body: "Each client is its own silo with its own wallet.",
    why: "Money never mixes between clients.",
    roleCopy: { viewer: { body: "Each client is its own silo. You can see every balance, read-only." } },
    empty: {
      title: "Add your first employer",
      body: "Each client gets its own silo and wallet. Start with one.",
      why: "Everything else builds on this.",
      target: "add-employer",
      action: { label: "Add employer", href: "/dashboard/employers/new" },
    },
  },
  {
    id: "contractors", chapter: "setup", pose: "idle", needsEmployer: true, target: "contractors", side: "top",
    route: (c) => (c.firstEmployerId ? `/dashboard/employers/${c.firstEmployerId}` : null),
    title: "Contractors",
    body: "Add the people you pay, or send them a private link to add themselves.",
    why: "Verified bank details mean fewer failed payouts.",
    roles: DECIDERS,
    empty: {
      body: "Contractors live inside each employer, with a bank check, an onboarding link and a pay priority.",
      why: "Add an employer and I'll show you.",
    },
  },
  {
    id: "funding", chapter: "payroll", pose: "idle", route: "/dashboard/funding", target: "funding-plan",
    fallback: PAGE_HEADER, side: "bottom",
    title: "Funding planner",
    body: "It shows the exact amount to wire, by when, and where to send it.",
    why: "No surprise shortfall on pay day.",
  },
  {
    id: "run", chapter: "payroll", pose: "idle", route: "/dashboard", target: "run-payroll", side: "bottom", roles: OWNERS,
    title: "Run payroll",
    body: "I price fees first, pay what the wallet covers, and hold the rest.",
    why: "No half-sent mystery batches.",
    needsEmployer: true,
    empty: {
      body: "Once an employer has contractors, Run Payroll prices fees first, pays what fits and holds the rest.",
      why: "Nothing is sent until a person signs.",
    },
  },
  {
    id: "approval", chapter: "payroll", pose: "shy", route: "/dashboard", target: "stats", side: "bottom",
    title: "A human signs",
    body: "You review the exact amounts and approve. I can never approve or pay.",
    why: "Only people move money.",
    roleCopy: {
      approver: { body: "This is your job: review the exact amounts and sign. I can never approve or pay." },
      viewer: { body: "Approvers sign every payment. You can follow each run, read-only.", why: "Everyone stays in the loop." },
    },
  },
  {
    id: "held", chapter: "safety", pose: "idle", route: "/dashboard/exceptions", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom", roles: DECIDERS,
    title: "Held lines",
    body: "Lines the wallet can't cover wait here. When funds arrive, I prepare the rest for you.",
    why: "Nothing is lost, and nothing pays itself.",
  },
  {
    id: "risk", chapter: "safety", pose: "shy", route: "/dashboard/exceptions", roles: DECIDERS,
    title: "Risk gate",
    body: "Unverified or freshly changed bank details are stopped before any money moves.",
    why: "Fake \"we switched banks\" requests get caught.",
  },
  {
    id: "proof", chapter: "safety", pose: "idle", route: "/dashboard/evidence", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom",
    title: "Proof of payment",
    body: "Every payout gets a PDF receipt, and contractors can check \"Where is my money?\" themselves.",
    why: "Fewer \"did it arrive?\" emails.",
  },
  {
    id: "notifications", chapter: "safety", pose: "idle", route: "/dashboard/notifications", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom",
    title: "Notifications",
    body: "Every email PaySilo wants to send queues here until email is connected.",
    why: "You see each message before and after it goes.",
  },
  {
    id: "benefits", chapter: "extras", pose: "idle", route: "/dashboard/benefits", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom", roles: OWNERS,
    title: "Benefits",
    body: "Pay for a contractor's course or gear by Visa through Airi, from a separate budget.",
    why: "Wages are always paid first.",
  },
  {
    id: "travel", chapter: "extras", pose: "idle", route: "/dashboard/travel", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom", roles: OWNERS,
    title: "Onboarding kits and travel",
    body: "Kits and trips have their own budgets, and a person approves each one.",
    why: "Spending never eats into payroll cash.",
  },
  {
    id: "workflows", chapter: "automation", pose: "idle", route: "/dashboard/workflows", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom", roles: OWNERS,
    title: "Workflows",
    body: "Build a flow once, pick a schedule, and I prepare every run on time.",
    why: "You only approve.",
  },
  {
    id: "calendar", chapter: "automation", pose: "idle", route: "/dashboard/calendar", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom", roles: DECIDERS,
    title: "Calendar",
    body: "See every schedule at a glance: one client on the 3rd, another on the 4th.",
    why: "Each client runs on its own day.",
  },
  {
    id: "brief", chapter: "automation", pose: "wave", route: "/dashboard/workflows/brief", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom", roles: DECIDERS,
    title: "Morning brief",
    body: "Each morning: who's ready, who's short, and what needs your decision.",
    why: "Start the day knowing what to do.",
  },
  {
    id: "compliance", chapter: "trust", pose: "idle", route: "/dashboard/compliance", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom", roles: ["owner", "admin", "viewer"],
    title: "Compliance",
    body: "Tax-form status and year-to-date totals for every contractor.",
    why: "No spreadsheet scramble at filing time.",
  },
  {
    id: "team", chapter: "trust", pose: "idle", route: "/dashboard/team", target: "page-header",
    fallback: PAGE_HEADER, side: "bottom", roles: OWNERS,
    title: "Team and roles",
    body: "Invite teammates as admin, approver or viewer, each with only what they need.",
    why: "Two sets of eyes on every payment.",
  },
  {
    id: "ask-silo", chapter: "trust", pose: "wave", route: "/dashboard", target: "ask-silo",
    fallback: ["button[aria-label='Open Silo chat']"], side: "left",
    title: "Ask me anything",
    body: "Ask about balances, runs or held lines. I can start a run, but never approve or pay.",
    why: "Faster than clicking around.",
  },
];

/** Steps for a role, in story order. */
export function stepsForRole(role: TourRole): TourStep[] {
  return TOUR_STEPS.filter((s) => !s.roles || s.roles.includes(role));
}

export function stepsForChapter(role: TourRole, chapter: ChapterId): TourStep[] {
  return stepsForRole(role).filter((s) => s.chapter === chapter);
}

export interface ResolvedStep {
  step: TourStep;
  title: string;
  body: string;
  why: string;
  /** Route to navigate to, or null when staying put / the data is missing. */
  route: string | null;
  target: string | undefined;
  /** True when sample-state copy replaced the real data step. */
  isEmpty: boolean;
  action?: { label: string; href: string };
}

export function fill(text: string, vars: { org: string; first: string }): string {
  return text.replaceAll("{org}", vars.org).replaceAll("{first}", vars.first);
}

/** Apply role copy and the empty-data variant for one step. */
export function resolveStep(step: TourStep, ctx: TourContext): ResolvedStep {
  const roleCopy = step.roleCopy?.[ctx.role];
  const route = typeof step.route === "function" ? step.route(ctx) : step.route ?? null;
  const missingData = !!step.needsEmployer && ctx.employerCount === 0;
  const emptyEmployers = step.id === "employers" && ctx.employerCount === 0;
  if ((missingData || emptyEmployers || (typeof step.route === "function" && route === null)) && step.empty) {
    // Viewers/approvers cannot add employers: keep the sample-state copy but drop the action.
    const canAdd = ctx.role === "owner" || ctx.role === "admin";
    const action = canAdd ? step.empty.action : undefined;
    const body = !canAdd && step.empty.action ? "No employers yet. An owner or admin adds the first one." : step.empty.body;
    return {
      step, title: step.empty.title ?? step.title, body, why: step.empty.why,
      route: typeof step.route === "string" ? step.route : null,
      target: emptyEmployers && canAdd ? step.empty.target : undefined,
      isEmpty: true, action,
    };
  }
  return {
    step, title: step.title, body: roleCopy?.body ?? step.body, why: roleCopy?.why ?? step.why,
    route, target: step.target, isEmpty: false,
  };
}

// ----------------------------------------------------------------- mini tours
export interface MiniTour {
  id: string; // stored in completed_chapters as `page:<id>`
  path: string;
  label: string;
  steps: TourStep[];
}

export const MINI_TOURS: MiniTour[] = [
  {
    id: "funding", path: "/dashboard/funding", label: "Funding planner",
    steps: [
      { id: "m-funding-1", chapter: "payroll", pose: "wave", target: "page-header", fallback: PAGE_HEADER, side: "bottom",
        title: "Fund before pay day", body: "Each client has a fund-by date, counted back from its pay date.", why: "Money lands before it's needed." },
      { id: "m-funding-2", chapter: "payroll", pose: "idle", target: "funding-plan", fallback: PAGE_HEADER, side: "top",
        title: "The exact gap", body: "This is what's missing in that client's own wallet, fees and reserve included.", why: "No guessing, no rounding." },
      { id: "m-funding-3", chapter: "payroll", pose: "idle", target: "funding-sweep", fallback: PAGE_HEADER, side: "bottom",
        title: "Reminders", body: "One tap queues a funding reminder with wire details for every client that needs it.", why: "Running it twice never double-sends." },
    ],
  },
  {
    id: "workflows", path: "/dashboard/workflows", label: "Workflows",
    steps: [
      { id: "m-workflows-1", chapter: "automation", pose: "wave", target: "page-header", fallback: PAGE_HEADER, side: "bottom",
        title: "Payroll on autopilot", body: "A workflow is a small flow with a schedule, such as Acme on the 3rd.", why: "You only approve." },
      { id: "m-workflows-2", chapter: "automation", pose: "idle", target: "workflow-card", fallback: PAGE_HEADER, side: "top",
        title: "Open one to edit", body: "Each card is a saved flow. Turn it on, or open the canvas to change it.", why: "Nothing runs until it's enabled." },
      { id: "m-workflows-3", chapter: "automation", pose: "shy",
        title: "Approval stays human", body: "A workflow prepares the run and waits at the gate for a signature.", why: "I never pay on my own." },
    ],
  },
];
