"use client";

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Check, ChevronDown, Clock, Copy, X, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { AGENTS, agentName } from "@/lib/agents";
import {
  deriveFlow, formatElapsed, MAIN_ORDER, relativeTime,
  type FlowEvent, type FlowNode, type NodeState,
} from "@/lib/agent-flow";
import { cn } from "@/lib/utils";

const LEVEL_DOT: Record<string, string> = {
  info: "bg-sage-500", success: "bg-mint-500", warning: "bg-clay-300", error: "bg-coral-500",
};

const STATE_LABEL: Record<NodeState, string> = {
  done: "Done", active: "Working", waiting: "Needs your approval", warning: "Needs a look", failed: "Stopped",
  skipped: "Skipped", upcoming: "Waiting", off: "Not configured",
};

const CARD_STATE: Record<NodeState, string> = {
  done: "border-sage-500/70 bg-sage-500/10",
  active: "af-pulse border-coral-500 bg-coral-500/10",
  waiting: "border-clay-400 bg-clay-500/20",
  warning: "border-clay-400 bg-clay-500/15",
  failed: "border-coral-500 bg-coral-500/15",
  skipped: "border-dashed border-ink-600 bg-transparent opacity-50",
  upcoming: "border-ink-600 bg-ink-900/40",
  off: "border-dashed border-ink-600 bg-transparent opacity-60",
};

const ICON_STATE: Record<NodeState, string> = {
  done: "bg-sage-500/20 text-sage-300",
  active: "bg-coral-500/20 text-coral-300",
  waiting: "bg-clay-500/30 text-sand-300",
  warning: "bg-clay-500/30 text-sand-300",
  failed: "bg-coral-500/25 text-coral-300",
  skipped: "bg-ink-800 text-sage-600",
  upcoming: "bg-ink-800 text-sage-500",
  off: "bg-ink-800 text-sage-600",
};

const BADGE_STATE: Partial<Record<NodeState, { cls: string; icon: React.ReactNode; label: string }>> = {
  done: { cls: "bg-sage-500 text-ink-950", icon: <Check className="size-3" strokeWidth={3} />, label: "Done" },
  warning: { cls: "bg-clay-400 text-sand-50", icon: <TriangleAlert className="size-3" strokeWidth={3} />, label: "Warning" },
  waiting: { cls: "bg-clay-400 text-sand-50", icon: <Clock className="size-3" strokeWidth={3} />, label: "Waiting for a person" },
  failed: { cls: "bg-coral-500 text-sand-50", icon: <X className="size-3" strokeWidth={3} />, label: "Failed" },
};

/** Wrap ids and amounts in monospace. */
const MONO = /((?:USD|EUR|GBP|[A-Z]{3}) ?\d[\d,]*(?:\.\d+)?|\d[\d,]*\.\d{2}\b|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{20,}|\b[a-z]{2,}_[A-Za-z0-9]{6,}\b|0x[0-9a-fA-F]{6,}|\bsession [A-Za-z0-9]{4,}\b)/;
function MonoText({ text }: { text: string }) {
  const parts = text.split(MONO);
  return (
    <>
      {parts.map((p, i) => (i % 2 === 1 ? <span key={i} className="font-mono text-[0.92em] font-semibold text-sand-100">{p}</span> : <Fragment key={i}>{p}</Fragment>))}
    </>
  );
}

function stepsLabel(n: number): string {
  return n === 1 ? "1 step" : `${n} steps`;
}

function nodeLine(node: FlowNode): string {
  if (node.id === "bedrock") {
    if (node.state === "off") return "Not configured";
    if (node.state === "upcoming") return "Waiting";
    if (node.state === "done") return "Re computed on AWS";
    if (node.state === "warning") return "Not cross checked";
    if (node.state === "failed") return "Did not match";
  }
  if (node.id === "escalate" && node.state === "waiting") return "Needs a decision";
  if (node.id === "refused") return "Refused";
  if (node.id === "held") return "Held";
  if (node.state === "failed") return node.id === "execute" ? "Payout failed" : "Stopped";
  return STATE_LABEL[node.state];
}

interface NodeCardProps {
  node: FlowNode;
  selected: boolean;
  onSelect: (id: string) => void;
  onApprove: () => void;
  className?: string;
}

function NodeCard({ node, selected, onSelect, onApprove, className }: NodeCardProps) {
  const info = AGENTS[node.id];
  if (!info) return null;
  const Icon = info.icon;
  const badge = BADGE_STATE[node.state];
  const waiting = node.state === "waiting";
  const clickable = node.count > 0 || node.state === "waiting";
  return (
    <div
      className={cn(
        "relative flex w-full flex-col rounded-2xl border-2 px-3 py-2.5 text-left transition-[background-color,border-color,opacity] duration-500",
        CARD_STATE[node.state], selected && "ring-2 ring-sand-300/70", className,
      )}
    >
      <button
        type="button"
        onClick={() => clickable && onSelect(node.id)}
        aria-pressed={selected}
        aria-label={`${info.name}: ${nodeLine(node)}${node.count ? `, ${stepsLabel(node.count)}` : ""}. ${clickable ? "Filter the log to this agent." : ""}`}
        className={cn("flex w-full items-start gap-2.5 rounded-xl text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sage-400", clickable ? "cursor-pointer" : "cursor-default")}
      >
        <span className={cn("relative grid size-9 shrink-0 place-items-center rounded-xl transition-colors duration-500", ICON_STATE[node.state])}>
          <Icon className="size-[18px]" aria-hidden />
          {badge && (
            <span className={cn("absolute -right-1.5 -top-1.5 grid size-[18px] place-items-center rounded-full ring-2 ring-ink-900", badge.cls)} role="img" aria-label={badge.label}>
              {badge.icon}
            </span>
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[13px] font-extrabold leading-tight text-sand-100">{info.name}</span>
          {info.partner && (
            <span className="mt-1 inline-block max-w-full truncate rounded-full bg-ink-800 px-1.5 py-px align-top text-[9px] font-extrabold uppercase leading-4 tracking-wide text-sage-400">
              {info.partner}
            </span>
          )}
        </span>
      </button>
      <p className={cn("mt-2 text-xs font-bold leading-snug", node.state === "failed" ? "text-coral-300" : node.state === "waiting" || node.state === "warning" ? "text-sand-300" : node.state === "active" ? "text-coral-300" : "text-sage-400")}>
        {nodeLine(node)}
        {node.count > 0 && <span className="font-semibold text-sage-500"> · {stepsLabel(node.count)}</span>}
      </p>
      {node.detail && <p className="mt-0.5 font-mono text-[11px] font-semibold leading-snug text-sage-300">{node.detail}</p>}
      {waiting && node.id === "approval" && (
        <Button size="sm" variant="sand" className="mt-2 h-8 w-full px-2 text-[10px]" onClick={onApprove}>
          Go to approval
        </Button>
      )}
    </div>
  );
}

type EdgeKind = "flow" | "done" | "idle";
function edgeKind(to: FlowNode): EdgeKind {
  if (to.state === "active" || to.state === "waiting") return "flow";
  if (to.count > 0 || to.state === "done") return "done";
  return "idle";
}

export interface AgentFlowProps {
  events: FlowEvent[];
  status: string;
  pendingKind?: "approval" | "escalation" | null;
  /** Where "Go to approval" goes when there is no #approval-controls block on this page. */
  approvalHref?: string;
  className?: string;
}

export function AgentFlow({ events, status, pendingKind = null, approvalHref, className }: AgentFlowProps) {
  const reduce = useReducedMotion();
  const flow = useMemo(() => deriveFlow(events, status, { pendingKind }), [events, status, pendingKind]);
  const [selected, setSelected] = useState<string | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [copied, setCopied] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const nodeById = useMemo(() => {
    const m: Record<string, FlowNode> = {};
    for (const n of [...flow.main, ...flow.branches]) m[n.id] = n;
    return m;
  }, [flow]);

  // Log groups: pipeline order first, then side branches / unknown agents in the order they appeared.
  const groups = useMemo(() => {
    const by: Record<string, FlowEvent[]> = {};
    for (const e of events) (by[e.agent] ??= []).push(e);
    const order = [...MAIN_ORDER as readonly string[], "bedrock", "escalate", ...Object.keys(by)];
    const seen = new Set<string>();
    return order.filter((id) => by[id] && !seen.has(id) && seen.add(id)).map((id) => ({ id, events: by[id] }));
  }, [events]);

  const lastAgent = events.length ? events[events.length - 1].agent : null;
  const focusId = flow.currentId ?? lastAgent;

  const visibleBranches = flow.branches;
  const branchesOf = (id: string) => visibleBranches.filter((b) => b.parent === id);

  function updateEdges() {
    const el = scrollRef.current;
    if (!el) return;
    setEdges({ left: el.scrollLeft > 4, right: el.scrollLeft + el.clientWidth < el.scrollWidth - 4 });
  }
  useEffect(() => {
    updateEdges();
    window.addEventListener("resize", updateEdges);
    return () => window.removeEventListener("resize", updateEdges);
  }, [flow]);

  // Keep the active node visible inside the pipeline's own scroll area (never scrolls the page).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !flow.currentId) return;
    const target = el.querySelector<HTMLElement>(`[data-node="${flow.currentId}"]`);
    if (!target || target.offsetParent === null) return;
    const left = target.offsetLeft - el.clientWidth / 2 + target.offsetWidth / 2;
    el.scrollTo({ left: Math.max(0, left), behavior: reduce ? "auto" : "smooth" });
  }, [flow.currentId, reduce]);

  function select(id: string) {
    const next = selected === id ? null : id;
    setSelected(next);
    if (next) {
      setOpen((o) => ({ ...o, [next]: true }));
      requestAnimationFrame(() => logRef.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "nearest" }));
    }
  }

  function goToApproval() {
    const el = document.getElementById("approval-controls");
    if (el) el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "center" });
    else if (approvalHref) window.location.assign(approvalHref);
  }

  async function copyLog() {
    const text = events.map((e) => {
      const t = relativeTime(events, e);
      return `${t ? `[${t}] ` : ""}${e.level.toUpperCase()} ${agentName(e.agent)}: ${e.message}`;
    }).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch { /* clipboard unavailable: nothing to do */ }
  }

  if (events.length === 0) {
    return <p className="font-semibold text-sage-400">No agent events recorded yet.</p>;
  }

  const { summary } = flow;
  const shownGroups = selected ? groups.filter((g) => g.id === selected) : groups;
  const mask = `linear-gradient(to right, ${edges.left ? "transparent, #000 28px" : "#000, #000"}, ${edges.right ? "#000 calc(100% - 28px), transparent" : "#000, #000"})`;

  return (
    <div className={cn("grid gap-4", className)}>
      {/* Summary strip */}
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          ["Elapsed", formatElapsed(summary.elapsedMs)],
          ["Agents that ran", String(summary.agentsRan)],
          ["Human decisions", String(summary.humanDecisions)],
          ["Systems touched", summary.systems.length ? summary.systems.join(", ") : "None"],
        ].map(([k, v]) => (
          <div key={k} className="min-w-0 rounded-2xl bg-ink-900/50 px-3 py-2">
            <dt className="text-[10px] font-extrabold uppercase tracking-widest text-sage-500">{k}</dt>
            <dd className="mt-0.5 break-words text-sm font-extrabold text-sand-100">{v}</dd>
          </div>
        ))}
      </dl>

      {/* Pipeline: horizontal from md up, vertical stepper below */}
      <div className="hidden md:block">
        <div ref={scrollRef} onScroll={updateEdges} className="overflow-x-auto pb-2 pt-3" style={{ maskImage: mask, WebkitMaskImage: mask }}>
          <ol className="flex min-w-max items-start px-2" aria-label="Agent pipeline">
            {flow.main.map((n, i) => {
              const kids = branchesOf(n.id);
              return (
                <Fragment key={n.id}>
                  {i > 0 && <li aria-hidden className="mt-[38px] w-8 shrink-0 list-none"><div className={cn("af-edge", `af-edge-${edgeKind(n)}`)} /></li>}
                  <motion.li
                    data-node={n.id}
                    className="w-[158px] shrink-0 list-none"
                    initial={reduce ? false : { opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.3, delay: reduce ? 0 : Math.min(i, 9) * 0.05 }}
                  >
                    <NodeCard node={n} selected={selected === n.id} onSelect={select} onApprove={goToApproval} />
                    {kids.map((k) => (
                      <motion.div
                        key={k.id}
                        data-node={k.id}
                        initial={reduce ? false : { opacity: 0, y: -6 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ duration: 0.3, delay: reduce ? 0 : 0.25 }}
                      >
                        <div aria-hidden className="mx-auto h-5 w-0"><div className={cn("af-edge-v h-full", `af-edge-${edgeKind(k)}`)} /></div>
                        <NodeCard node={k} selected={selected === k.id} onSelect={select} onApprove={goToApproval} />
                      </motion.div>
                    ))}
                  </motion.li>
                </Fragment>
              );
            })}
          </ol>
        </div>
      </div>

      <ol className="grid md:hidden" aria-label="Agent pipeline">
        {flow.main.map((n, i) => {
          const kids = branchesOf(n.id);
          const last = i === flow.main.length - 1;
          return (
            <motion.li
              key={n.id}
              data-node={n.id}
              className="relative list-none pl-0"
              initial={reduce ? false : { opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3, delay: reduce ? 0 : Math.min(i, 9) * 0.04 }}
            >
              <div>
                <div className="min-w-0">
                  <NodeCard node={n} selected={selected === n.id} onSelect={select} onApprove={goToApproval} />
                  {kids.map((k) => (
                    <div key={k.id} data-node={k.id} className="ml-5 flex">
                      <div aria-hidden className="mr-3 w-0"><div className={cn("af-edge-v h-3 ml-0", `af-edge-${edgeKind(k)}`)} /></div>
                      <div className="mt-3 min-w-0 flex-1">
                        <NodeCard node={k} selected={selected === k.id} onSelect={select} onApprove={goToApproval} />
                      </div>
                    </div>
                  ))}
                  {!last && <div aria-hidden className="ml-[17px] h-4 w-0"><div className={cn("af-edge-v h-full", `af-edge-${edgeKind(flow.main[i + 1])}`)} /></div>}
                </div>
              </div>
            </motion.li>
          );
        })}
      </ol>

      {/* Detailed log */}
      <div ref={logRef} className="scroll-mt-4">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <p className="mr-auto text-sm font-extrabold uppercase tracking-widest text-sage-500">
            {selected ? `Log: ${agentName(selected)}` : "Detailed log"}
          </p>
          {selected && (
            <Button size="sm" variant="outline" onClick={() => setSelected(null)}>Show all</Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => void copyLog()} aria-live="polite">
            <Copy className="size-4" /> {copied ? "Copied" : "Copy log"}
          </Button>
        </div>
        <div className="grid gap-2">
          {shownGroups.map((g) => {
            const node = nodeById[g.id];
            const info = AGENTS[g.id];
            const isOpen = selected === g.id || (open[g.id] ?? g.id === focusId);
            const Icon = info?.icon;
            return (
              <section key={g.id} className="rounded-2xl bg-ink-900/50">
                <button
                  type="button"
                  aria-expanded={isOpen}
                  onClick={() => setOpen((o) => ({ ...o, [g.id]: !isOpen }))}
                  className="flex w-full items-center gap-2.5 rounded-2xl px-3 py-2.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sage-400"
                >
                  {Icon && <Icon className="size-4 shrink-0 text-sage-400" aria-hidden />}
                  <span className="min-w-0 flex-1 truncate text-sm font-extrabold text-sand-100">{agentName(g.id)}</span>
                  {node && <span className={cn("size-2 shrink-0 rounded-full", node.state === "failed" ? "bg-coral-500" : node.state === "warning" || node.state === "waiting" ? "bg-clay-300" : node.state === "active" ? "bg-coral-400" : "bg-sage-500")} aria-hidden />}
                  <span className="shrink-0 text-xs font-bold text-sage-500">{stepsLabel(g.events.length)}</span>
                  <ChevronDown className={cn("size-4 shrink-0 text-sage-500 transition-transform", isOpen && "rotate-180")} aria-hidden />
                </button>
                {isOpen && (
                  <ul className="grid gap-1.5 px-3 pb-3">
                    {g.events.map((e, i) => {
                      const t = relativeTime(events, e);
                      return (
                        <li key={e.seq ?? i} className="flex gap-3 rounded-xl bg-ink-950/30 px-3 py-2">
                          <span className={cn("mt-1.5 size-2.5 shrink-0 rounded-full", LEVEL_DOT[e.level] ?? "bg-sage-500")} role="img" aria-label={e.level} />
                          <p className="min-w-0 flex-1 break-words text-sm leading-relaxed text-sand-200">
                            <MonoText text={e.message} />
                          </p>
                          {t && <span className="shrink-0 pt-0.5 font-mono text-[11px] font-semibold text-sage-500">{t}</span>}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            );
          })}
        </div>
      </div>
    </div>
  );
}
