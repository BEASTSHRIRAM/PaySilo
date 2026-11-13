"use client";

import { useEffect, useRef } from "react";

import { cn } from "@/lib/utils";
import { gsap, prefersReducedMotion, useRefreshOnLayout } from "./motion";

const STEPS = [
  { day: "3rd", who: "Acme", text: "Run prepared. Fees priced, wallet checked.", tone: "bg-sage-500 text-ink-950", tag: "Prepared" },
  { day: "3rd", who: "Acme", text: "You approve. Acme is paid.", tone: "bg-mint-500 text-ink-950", tag: "Paid" },
  { day: "4th", who: "Harborview", text: "Wallet is short. Two lines are held.", tone: "bg-clay-500 text-sand-100", tag: "Held" },
  { day: "4th", who: "Harborview", text: "Funds arrive. Held lines continue.", tone: "bg-sage-500 text-ink-950", tag: "Continue" },
  { day: "4th", who: "Harborview", text: "You approve. Harborview is paid.", tone: "bg-mint-500 text-ink-950", tag: "Paid" },
];

export function Month() {
  const root = useRef<HTMLElement>(null);
  useRefreshOnLayout();

  useEffect(() => {
    const el = root.current;
    if (!el || prefersReducedMotion()) return;
    const ctx = gsap.context(() => {
      const q = gsap.utils.selector(el);
      const rows = q("[data-row]");
      const tl = gsap.timeline({
        scrollTrigger: { trigger: q("[data-list]")[0], start: "top 70%", end: "bottom 60%", scrub: 0.6 },
      });
      tl.from(q("[data-fillline]"), { scaleY: 0, transformOrigin: "top", ease: "none", duration: rows.length });
      rows.forEach((r, i) => {
        tl.from(r, { opacity: 0.25, x: 16, duration: 0.6, ease: "power2.out" }, i);
        tl.from(r.querySelector("[data-pt]"), { scale: 0.4, backgroundColor: "#15303A", duration: 0.4 }, i);
      });
    }, el);
    return () => ctx.revert();
  }, []);

  return (
    <section id="month" ref={root} className="relative scroll-mt-20 py-14 lg:py-20">
      <div className="container grid gap-10 lg:grid-cols-[0.9fr_1.1fr] lg:gap-16">
        <div className="lg:sticky lg:top-28 lg:self-start">
          <p className="text-sm font-extrabold uppercase tracking-widest text-coral-400">How a month looks</p>
          <h2 className="mt-3 font-display text-4xl font-semibold leading-tight text-sand-100 text-balance sm:text-5xl">
            Acme on the 3rd. Harborview on the 4th.
          </h2>
          <p className="mt-4 max-w-md text-base leading-relaxed text-sage-300">
            Each client runs on its own schedule. Silo prepares, holds what does not fit, and waits for you. You approve.
          </p>
        </div>
        <ol data-list className="relative space-y-4 pl-10">
          <span className="absolute bottom-3 left-[11px] top-3 w-0.5 bg-ink-600" />
          <span data-fillline className="absolute bottom-3 left-[11px] top-3 w-0.5 bg-coral-500" />
          {STEPS.map((s, i) => (
            <li key={i} data-row className="relative flex items-center gap-4 rounded-2xl border border-sand-200/10 bg-ink-850 px-4 py-4">
              <span data-pt className="absolute -left-[37px] top-1/2 size-[14px] -translate-y-1/2 rounded-full border-2 border-coral-500 bg-coral-500" />
              <span className="grid size-12 shrink-0 place-items-center rounded-xl bg-ink-700 font-display text-lg font-semibold text-sand-100">{s.day}</span>
              <div className="min-w-0 flex-1">
                <p className="text-xs font-extrabold uppercase tracking-wider text-sage-500">{s.who}</p>
                <p className="text-base font-bold text-sand-100">{s.text}</p>
              </div>
              <span className={cn("hidden shrink-0 rounded-full px-3 py-1 text-xs font-extrabold uppercase sm:inline-block", s.tone)}>{s.tag}</span>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
