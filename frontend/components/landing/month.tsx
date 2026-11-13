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
        scrollTrigger: { trigger: q("[data-list]")[0], start: "top 85%", end: "bottom 70%", scrub: 0.6 },
      });
      rows.forEach((r, i) => {
        tl.from(r, { opacity: 0.25, y: 12, duration: 0.6, ease: "power2.out" }, i * 0.7);
        tl.from(r.querySelector("[data-pt]"), { scale: 0.3, opacity: 0.3, duration: 0.4 }, i * 0.7);
      });
    }, el);
    return () => ctx.revert();
  }, []);

  return (
    <section id="month" ref={root} className="relative scroll-mt-20 py-10 lg:py-14">
      <div className="container">
        <div className="grid gap-2 lg:grid-cols-[1fr_auto] lg:items-end lg:gap-10">
          <div>
            <p className="text-sm font-extrabold uppercase tracking-widest text-coral-400">How a month looks</p>
            <h2 className="mt-2 font-display text-3xl font-semibold leading-tight text-sand-100 text-balance sm:text-4xl">
              Acme on the 3rd. Harborview on the 4th.
            </h2>
          </div>
          <p className="max-w-md text-sm leading-relaxed text-sage-300">
            Each client runs on its own schedule. Silo prepares, holds what does not fit, and waits for you.
          </p>
        </div>
        <ol data-list className="mt-5 grid gap-2 lg:grid-cols-5 lg:gap-3">
          {STEPS.map((s, i) => (
            <li key={i} data-row className="flex items-center gap-3 rounded-2xl border border-sand-200/10 bg-ink-850 px-3 py-2.5 lg:flex-col lg:items-start lg:gap-2 lg:p-4">
              <div className="flex items-center gap-2 lg:w-full lg:justify-between">
                <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-ink-700 font-display text-sm font-semibold text-sand-100">{s.day}</span>
                <span data-pt className={cn("hidden rounded-full px-2.5 py-0.5 text-[11px] font-extrabold uppercase lg:inline-block", s.tone)}>{s.tag}</span>
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-[11px] font-extrabold uppercase tracking-wider text-sage-500">{s.who}</p>
                <p className="text-sm font-bold leading-snug text-sand-100">{s.text}</p>
              </div>
              <span className={cn("shrink-0 rounded-full px-2.5 py-0.5 text-[11px] font-extrabold uppercase lg:hidden", s.tone)}>{s.tag}</span>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
