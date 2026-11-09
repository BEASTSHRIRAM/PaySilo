"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Loader2 } from "lucide-react";
import gsap from "gsap";

import { prefersReducedMotion } from "@/lib/gsap-anim";
import { cn } from "@/lib/utils";
import type { ThinkingStep } from "./chat-types";

/**
 * Claude-style thinking disclosure for one assistant turn.
 *
 * While the turn streams it stays open: each step appears live, the active
 * step pulses gently, finished steps get a check. The moment the answer
 * completes it folds to a single quiet line — "Thought through N steps" —
 * unless the reader has already taken control of the toggle, in which case
 * their choice wins. History turns start folded.
 */
export function ThinkingBlock({ steps, streaming }: { steps: ThinkingStep[]; streaming: boolean }) {
  const [userToggled, setUserToggled] = useState<boolean | null>(null);
  const expanded = userToggled ?? streaming;
  const doneCount = steps.filter((s) => s.status === "done").length;
  const allDone = steps.length > 0 && doneCount === steps.length;

  if (steps.length === 0) return null;

  const summary = streaming
    ? allDone
      ? `Thought through ${steps.length} ${steps.length === 1 ? "step" : "steps"}`
      : "Thinking"
    : `Thought through ${steps.length} ${steps.length === 1 ? "step" : "steps"}`;

  return (
    <section aria-label="How Silo worked through this" className="min-w-0">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setUserToggled(!expanded)}
        className={cn(
          "group flex w-full items-center gap-2 rounded-lg px-1 py-1 text-left text-[13px] font-bold",
          "text-sage-300 transition-colors hover:text-sand-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sage-400/60",
        )}
      >
        {streaming && !allDone ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-sage-400" aria-hidden />
        ) : (
          <Check className="size-3.5 shrink-0 text-mint-400" aria-hidden />
        )}
        <span className={cn(streaming && !allDone && "animate-pulse")}>{summary}</span>
        <ChevronDown
          className={cn("size-4 shrink-0 text-sage-500 transition-transform duration-200 group-hover:text-sage-300", expanded && "rotate-180")}
          aria-hidden
        />
      </button>

      {/* Grid-rows fold: animates height without measuring the DOM. */}
      <div
        className={cn(
          "grid transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none",
          expanded ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
        )}
      >
        <div className="min-h-0 overflow-hidden">
          <ol className="relative ml-[13px] grid gap-0 border-l border-ink-600/80 py-1 pl-5">
            {steps.map((step) => (
              <ThinkingStepRow key={step.id} step={step} />
            ))}
          </ol>
        </div>
      </div>
    </section>
  );
}

function ThinkingStepRow({ step }: { step: ThinkingStep }) {
  const ref = useRef<HTMLLIElement>(null);

  // One authored entrance per step: a short rise as the step lands live.
  useEffect(() => {
    const el = ref.current;
    if (!el || prefersReducedMotion()) return;
    const tween = gsap.fromTo(
      el,
      { opacity: 0, y: 6 },
      { opacity: 1, y: 0, duration: 0.32, ease: "power2.out", clearProps: "transform" },
    );
    return () => {
      tween.kill();
    };
  }, []);

  const active = step.status === "active";

  return (
    <li ref={ref} className="relative py-1">
      <span
        className={cn(
          "absolute -left-[27px] top-[7px] grid size-[15px] place-items-center rounded-full border",
          active
            ? "border-sage-400/70 bg-ink-800"
            : "border-mint-700/60 bg-mint-500/15",
        )}
        aria-hidden
      >
        {active ? (
          <span className="size-1.5 animate-pulse rounded-full bg-sage-300" />
        ) : (
          <Check className="size-2.5 text-mint-400" strokeWidth={3.5} />
        )}
      </span>
      <p className={cn("text-[13px] font-bold leading-snug", active ? "animate-pulse text-sand-100" : "text-sand-200")}>
        {step.step}
      </p>
      {step.detail && (
        <p className="mt-0.5 text-[13px] font-medium leading-snug text-sage-300/90">{step.detail}</p>
      )}
    </li>
  );
}
