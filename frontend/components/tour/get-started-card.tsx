"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowRight, Check, X } from "lucide-react";

import { Mascot } from "@/components/tour/mascot";
import { useTour } from "@/components/tour/tour-provider";
import { apiTour, type TourChecklist } from "@/lib/api";
import { useDataChanged } from "@/lib/events";
import { cn } from "@/lib/utils";

function Ring({ done, total }: { done: number; total: number }) {
  const reduced = useReducedMotion();
  const r = 24;
  const c = 2 * Math.PI * r;
  const pct = total === 0 ? 0 : done / total;
  return (
    <div className="relative size-16 shrink-0" role="img" aria-label={`${done} of ${total} steps done`}>
      <svg viewBox="0 0 64 64" className="size-full -rotate-90">
        <circle cx="32" cy="32" r={r} fill="none" strokeWidth="7" className="stroke-ink-700" />
        <motion.circle
          cx="32" cy="32" r={r} fill="none" strokeWidth="7" strokeLinecap="round"
          className={pct === 1 ? "stroke-mint-500" : "stroke-coral-500"}
          strokeDasharray={c}
          initial={reduced ? false : { strokeDashoffset: c }}
          animate={{ strokeDashoffset: c * (1 - pct) }}
          transition={{ duration: 0.7, ease: "easeOut" }}
        />
      </svg>
      <span className="absolute inset-0 grid place-items-center font-display text-base font-semibold tabular-nums text-sand-100">
        {done}/{total}
      </span>
    </div>
  );
}

/**
 * "Get started" checklist on the dashboard home. Every tick is derived server-side from real
 * organization data (GET /me/onboarding/checklist); nothing here stores or fakes progress.
 */
export function GetStartedCard() {
  const { state } = useTour();
  const [data, setData] = useState<TourChecklist | null>(null);

  const load = useCallback(() => {
    apiTour.checklist().then(setData).catch(() => undefined);
  }, []);
  useEffect(load, [load]);
  useDataChanged(load);
  useEffect(() => {
    const onFocus = () => load();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [load]);

  // Replaying "Show the Get started list" clears the dismissal server-side; mirror it when state changes.
  const dismissed = data?.dismissed ?? false;

  async function dismiss() {
    setData((d) => (d ? { ...d, dismissed: true } : d));
    await apiTour.save({ checklist_dismissed: true }).catch(() => null);
  }

  const show = !!data && data.visible && !dismissed && !!state;
  const required = data?.items.filter((i) => !i.optional) ?? [];
  const optional = data?.items.filter((i) => i.optional) ?? [];
  const next = required.find((i) => !i.done);

  return (
    <AnimatePresence initial={false}>
      {show && data && (
        <motion.section
          key="get-started"
          aria-label="Get started checklist"
          data-tour="get-started"
          className="glass rounded-4xl border-2 border-sand-200/10 p-5 sm:p-6"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, height: 0, marginTop: 0, paddingTop: 0, paddingBottom: 0 }}
          transition={{ duration: 0.3 }}
        >
          <div className="flex items-start gap-4">
            <Ring done={data.done_count} total={data.total} />
            <div className="min-w-0 flex-1">
              <h2 className="font-display text-2xl font-semibold text-sand-100">
                {data.all_done ? "You're set up" : "Get started"}
              </h2>
              <p className="mt-0.5 text-sm font-semibold text-sage-300">
                {data.all_done
                  ? "Everything on the list is done. Silo is in the corner whenever you need it."
                  : next
                    ? `Next up: ${next.label.toLowerCase()}. Ticks appear on their own when it's really done.`
                    : "Almost there."}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Mascot pose={data.all_done ? "cheer" : "idle"} size={48} className="hidden sm:grid" />
              <button type="button" onClick={() => void dismiss()} aria-label="Dismiss the Get started list" title="Dismiss"
                className="rounded-full p-1.5 text-sage-400 transition-colors hover:bg-ink-700 hover:text-sand-100 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60">
                <X className="size-5" />
              </button>
            </div>
          </div>

          <ul className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {[...required, ...optional].map((item) => (
              <li key={item.key}>
                <Link
                  href={item.href}
                  className={cn(
                    "group flex items-center gap-3 rounded-3xl border-2 px-4 py-3 transition-colors focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60",
                    item.done ? "border-mint-700/40 bg-mint-500/5" : "border-ink-700 bg-ink-850/60 hover:border-coral-500/60",
                  )}
                >
                  <span className={cn("grid size-6 shrink-0 place-items-center rounded-full border-2",
                    item.done ? "border-mint-500 bg-mint-500 text-ink-950" : "border-ink-500")}>
                    {item.done && <Check className="size-3.5" strokeWidth={3.5} />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className={cn("block text-sm font-extrabold", item.done ? "text-sage-400 line-through decoration-sage-600" : "text-sand-100")}>
                      {item.label}{item.optional && <span className="ml-2 text-[10px] font-extrabold uppercase tracking-widest text-sage-500">Optional</span>}
                    </span>
                    <span className="block truncate text-xs font-semibold text-sage-500">{item.hint}</span>
                  </span>
                  {!item.done && <ArrowRight className="size-4 shrink-0 text-sage-500 transition-transform group-hover:translate-x-0.5 group-hover:text-coral-400" />}
                </Link>
              </li>
            ))}
          </ul>
        </motion.section>
      )}
    </AnimatePresence>
  );
}
