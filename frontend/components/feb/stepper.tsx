"use client";

import { Check, RotateCcw, X } from "lucide-react";

import { isMoneyMoved } from "@/lib/status";
import { cn } from "@/lib/utils";

/**
 * Payment lifecycle: Initiated -> Processed -> Settled, with Failed and
 * Returned as terminal branches shown beneath the track (never as a
 * fourth "step" — the money did not travel that path).
 */
export type LifecycleStage = 0 | 1 | 2; // index of the furthest stage reached
export type LifecycleTerminal = "failed" | "returned" | null;

const STAGES = ["Initiated", "Processed", "Settled"] as const;

/** Map a pay-line status (+ its run's status) onto the lifecycle. */
export function lifecycleFor(
  payLineStatus: string,
  runStatus?: string | null,
): { stage: LifecycleStage; terminal: LifecycleTerminal; held: boolean } {
  const s = (payLineStatus || "").toUpperCase();
  if (s === "FAILED" || s === "REFUSED") return { stage: 1, terminal: "failed", held: false };
  if (s === "RETURNED" || s === "REFUNDED" || s === "REVERSED")
    return { stage: 2, terminal: "returned", held: false };
  if (s === "PAID" || s === "SETTLED" || s === "COMPLETED") return { stage: 2, terminal: null, held: false };
  if (s === "SENT" || s === "PROCESSING" || s === "PROCESSED" || s === "IN_TRANSIT")
    return { stage: 1, terminal: null, held: false };
  // A settled run lifts any non-terminal line to Settled.
  if (isMoneyMoved((runStatus || "").toUpperCase()) && s !== "HELD") return { stage: 2, terminal: null, held: false };
  return { stage: 0, terminal: null, held: s === "HELD" || s.includes("HOLD") };
}

export function LifecycleStepper({
  stage,
  terminal,
  held = false,
  compact = false,
}: {
  stage: LifecycleStage;
  terminal: LifecycleTerminal;
  held?: boolean;
  compact?: boolean;
}) {
  return (
    <div>
      <ol className="flex items-start" aria-label="Payment lifecycle">
        {STAGES.map((label, i) => {
          const done = terminal ? i < stage : i <= stage;
          const isLast = i === STAGES.length - 1;
          const reachedFinal = stage === 2 && !terminal;
          return (
            <li key={label} className={cn("flex items-start", !isLast && "flex-1")}>
              <div className="flex flex-col items-center gap-1.5">
                <span
                  className={cn(
                    "grid place-items-center rounded-full border-2 font-black",
                    compact ? "size-7 text-[11px]" : "size-9 text-xs",
                    done
                      ? reachedFinal && i === 2
                        ? "border-mint-500 bg-mint-500 text-ink-950"
                        : "border-sage-500 bg-sage-500 text-ink-950"
                      : "border-ink-600 bg-ink-800 text-sage-500",
                    terminal && i === stage && "border-coral-500 bg-coral-500/20 text-coral-300",
                  )}
                >
                  {done ? <Check className={compact ? "size-3.5" : "size-4"} /> : i + 1}
                </span>
                <span
                  className={cn(
                    "whitespace-nowrap font-extrabold",
                    compact ? "text-[11px]" : "text-xs",
                    done ? "text-sand-100" : "text-sage-500",
                  )}
                >
                  {label}
                </span>
              </div>
              {!isLast && (
                <span
                  aria-hidden
                  className={cn(
                    "mx-2 mt-4 h-0.5 flex-1 rounded-full",
                    compact && "mt-3",
                    i < stage ? "bg-sage-500" : "bg-ink-600",
                  )}
                />
              )}
            </li>
          );
        })}
      </ol>
      {held && !terminal && (
        <p className="mt-2 inline-flex items-center gap-1.5 rounded-full border-2 border-sand-400/50 bg-sand-300/10 px-2.5 py-0.5 text-xs font-extrabold text-sand-300">
          On hold — waiting on a decision or funds
        </p>
      )}
      {terminal === "failed" && (
        <p className="mt-2 inline-flex items-center gap-1.5 rounded-full border-2 border-coral-700/70 bg-coral-500/15 px-2.5 py-0.5 text-xs font-extrabold text-coral-300">
          <X className="size-3.5" /> Failed — this payment did not settle
        </p>
      )}
      {terminal === "returned" && (
        <p className="mt-2 inline-flex items-center gap-1.5 rounded-full border-2 border-sand-400/50 bg-sand-300/10 px-2.5 py-0.5 text-xs font-extrabold text-sand-300">
          <RotateCcw className="size-3.5" /> Returned — the funds came back after processing
        </p>
      )}
    </div>
  );
}
