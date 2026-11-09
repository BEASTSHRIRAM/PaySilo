"use client";

import { useEffect, useRef, useState, type ComponentType } from "react";
import {
  ArrowRightLeft, Banknote, Building2, Calculator, Check, ChevronDown, FileText,
  Landmark, Loader2, ReceiptText, Scale, Search, ShieldCheck, Users, Wallet, Wrench, X,
} from "lucide-react";
import gsap from "gsap";

import { prefersReducedMotion } from "@/lib/gsap-anim";
import { cn } from "@/lib/utils";
import { formatJson, type ToolCardState } from "./chat-types";

/**
 * Icon per tool, chosen from what the tool actually does. Deliberately
 * concrete payroll objects — never a generic sparkle or robot.
 */
const TOOL_ICONS: { match: RegExp; Icon: ComponentType<{ className?: string }> }[] = [
  { match: /balance|wallet|funding|account_balance/, Icon: Wallet },
  { match: /assess|shortfall|price|quote|fee/, Icon: Calculator },
  { match: /run|payroll|execute|start/, Icon: Banknote },
  { match: /transfer|payout|pay_|send|disburse/, Icon: ArrowRightLeft },
  { match: /employer|company|organisation|organization/, Icon: Building2 },
  { match: /contractor|employee|worker|people|team/, Icon: Users },
  { match: /receipt|proof|verify|audit|receipt/, Icon: ReceiptText },
  { match: /compliance|policy|rule|check|valid/, Icon: ShieldCheck },
  { match: /bank|airwallex|account/, Icon: Landmark },
  { match: /search|lookup|find|list|get_|fetch/, Icon: Search },
  { match: /document|file|report|summary|explain/, Icon: FileText },
  { match: /convert|currency|fx|exchange/, Icon: Scale },
];

export function iconForTool(tool: string): ComponentType<{ className?: string }> {
  const name = tool.toLowerCase();
  return TOOL_ICONS.find(({ match }) => match.test(name))?.Icon ?? Wrench;
}

/**
 * One real tool call. Renders only from a tool_start event; the result
 * half appears only when tool_result arrives. Raw result data sits behind
 * an explicit "Inspect result" disclosure — data is mono because it is
 * data, not as decoration.
 */
export function ToolCard({ card }: { card: ToolCardState }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const Icon = iconForTool(card.tool);
  const running = card.status === "running";
  const failed = card.status === "error";

  useEffect(() => {
    const el = ref.current;
    if (!el || prefersReducedMotion()) return;
    const tween = gsap.fromTo(
      el,
      { opacity: 0, y: 8 },
      { opacity: 1, y: 0, duration: 0.35, ease: "power2.out", clearProps: "transform" },
    );
    return () => {
      tween.kill();
    };
  }, []);

  return (
    <div
      ref={ref}
      className={cn(
        "min-w-0 overflow-hidden rounded-2xl border bg-ink-950/50",
        failed ? "border-coral-600/50" : "border-ink-600/70",
      )}
    >
      <div className="flex items-start gap-2.5 px-3 py-2.5">
        <span
          className={cn(
            "mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg",
            failed ? "bg-coral-500/15 text-coral-300" : "bg-sage-500/15 text-sage-300",
          )}
          aria-hidden
        >
          <Icon className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="text-sm font-bold leading-snug text-sand-100">{card.label}</span>
            <span
              className={cn(
                "inline-flex items-center gap-1 text-[11px] font-extrabold uppercase tracking-wide",
                running ? "text-sage-300" : failed ? "text-coral-300" : "text-mint-400",
              )}
              role="status"
            >
              {running ? (
                <>
                  <Loader2 className="size-3 animate-spin" aria-hidden /> Running
                </>
              ) : failed ? (
                <>
                  <X className="size-3" aria-hidden /> Failed
                </>
              ) : (
                <>
                  <Check className="size-3" aria-hidden /> Done
                </>
              )}
            </span>
          </p>
          {card.inputSummary && (
            <p className="mt-0.5 truncate text-[13px] font-medium leading-snug text-sage-300/90" title={card.inputSummary}>
              {card.inputSummary}
            </p>
          )}
          {!running && card.summary && (
            <p className={cn("mt-1 text-sm font-medium leading-snug", failed ? "text-coral-200" : "text-sand-200")}>
              {card.summary}
            </p>
          )}
          {running && (
            <p className="mt-1 flex items-center gap-1.5 text-[13px] font-semibold text-sage-400">
              <span className="size-1.5 animate-pulse rounded-full bg-sage-400" aria-hidden />
              Waiting for the result…
            </p>
          )}
        </div>
      </div>

      {card.hasData && !running && (
        <>
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="flex w-full items-center gap-1.5 border-t border-ink-600/60 px-3 py-2 text-left text-xs font-extrabold text-sage-300 transition-colors hover:bg-ink-800/60 hover:text-sand-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sage-400/60"
          >
            <ChevronDown className={cn("size-3.5 transition-transform duration-200", open && "rotate-180")} aria-hidden />
            {open ? "Hide result data" : "Inspect result data"}
          </button>
          <div
            className={cn(
              "grid transition-[grid-template-rows] duration-300 ease-out motion-reduce:transition-none",
              open ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
            )}
          >
            <div className="min-h-0 overflow-hidden">
              <pre className="max-h-56 overflow-auto border-t border-ink-600/60 bg-ink-950/80 px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-sage-200">
                {formatJson(card.data)}
              </pre>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
