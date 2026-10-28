"use client";

import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Layers, PauseCircle, Wallet } from "lucide-react";

import { cn, formatMoney } from "@/lib/utils";

// Same numbers the backend produces for the seeded demo (1 EUR = 1.08 USD,
// EUR 12.85 SWIFT fee per payout, USD 149 run fee, USD 1,000 reserve floor).
const WALLETS = [
  { name: "Acme Manufacturing", balance: 20000, needed: 7656.76, payroll: "Lena + Tomás · EUR 6,000" },
  { name: "Borealis Labs", balance: 5000, needed: 7642.88, payroll: "Ingrid · EUR 6,000" },
  { name: "Cobalt Retail", balance: 10000, needed: 0, payroll: "No payroll this week · fresh $10,000 deposit" },
];
const TOTAL = WALLETS.reduce((s, w) => s + w.balance, 0);
const NEEDED = WALLETS.reduce((s, w) => s + w.needed, 0);

type View = "pooled" | "silo";

export function Problem() {
  const [view, setView] = useState<View>("pooled");

  return (
    <section id="problem" className="relative scroll-mt-28 py-24">
      <div className="container grid gap-12 lg:grid-cols-[0.9fr_1.1fr] lg:items-center">
        <div>
          <p className="text-sm font-extrabold uppercase tracking-widest text-coral-400">The aggregated balance illusion</p>
          <h2 className="mt-3 font-display text-4xl font-semibold leading-tight text-sand-100 text-balance sm:text-5xl">
            The platform looks rich. One client is still short.
          </h2>
          <p className="mt-5 text-lg leading-relaxed text-sage-300">
            Vertex Payroll runs payroll for three companies. Add their wallets up and there&apos;s plenty of money —
            so a tired accountant, or a badly built AI, quietly covers Borealis with Acme&apos;s or Cobalt&apos;s cash.
            Nobody notices that day. Everybody notices one day.
          </p>
          <p className="mt-4 text-lg leading-relaxed text-sage-300">
            PaySilo only ever reads <span className="font-bold text-sand-100">one employer&apos;s wallet</span> when it
            decides whether that employer can pay. Flip the view and see the difference.
          </p>

          <div role="tablist" aria-label="Balance view" className="mt-8 inline-flex rounded-2xl border-2 border-ink-600 bg-ink-900/70 p-1.5">
            {([["pooled", "Pooled view", Layers], ["silo", "Silo view", Wallet]] as const).map(([key, label, Icon]) => (
              <button
                key={key}
                role="tab"
                aria-selected={view === key}
                onClick={() => setView(key)}
                className={cn(
                  "inline-flex items-center gap-2 rounded-xl px-5 py-2.5 text-sm font-extrabold uppercase tracking-wide transition-all",
                  view === key
                    ? key === "pooled"
                      ? "bg-sand-200 text-ink-800 shadow-[0_3px_0_0_theme(colors.sand.400)]"
                      : "bg-coral-500 text-sand-50 shadow-[0_3px_0_0_theme(colors.coral.700)]"
                    : "text-sage-400 hover:text-sand-100",
                )}
              >
                <Icon className="size-4" /> {label}
              </button>
            ))}
          </div>
        </div>

        <div className="relative rounded-[2.5rem] border-2 border-ink-700 bg-ink-850/80 p-5 sm:p-7">
          <AnimatePresence mode="wait">
            {view === "pooled" ? (
              <motion.div key="pooled" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -12 }}>
                <div className="rounded-3xl bg-sand-200 p-6 text-ink-800">
                  <p className="text-sm font-extrabold uppercase tracking-widest text-clay-500">Platform total</p>
                  <p className="mt-1 font-display text-5xl font-semibold tabular-nums">{formatMoney(TOTAL)}</p>
                  <p className="mt-2 font-semibold">vs {formatMoney(NEEDED)} needed for every payroll, fees and reserves</p>
                  <div className="mt-4 inline-flex items-center gap-2 rounded-full bg-mint-500/25 px-3 py-1 text-sm font-extrabold text-ink-800">
                    <CheckCircle2 className="size-4" /> “Everyone can be paid”
                  </div>
                </div>
                <div className="mt-4 flex items-start gap-3 rounded-3xl border-2 border-coral-500/40 bg-coral-500/10 p-5">
                  <AlertTriangle className="mt-0.5 size-6 shrink-0 text-coral-400" />
                  <p className="text-sand-100">
                    <span className="font-extrabold">This is the bug.</span> To pay Ingrid in full, $2,642.88 of it would
                    have to come out of Acme&apos;s or Cobalt&apos;s money. Legal problem, trust problem, product over.
                  </p>
                </div>
              </motion.div>
            ) : (
              <motion.ul key="silo" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -12 }} className="grid gap-3">
                {WALLETS.map((w, idx) => {
                  const short = w.needed > w.balance;
                  const idle = w.needed === 0;
                  return (
                    <motion.li
                      key={w.name}
                      initial={{ opacity: 0, x: 16 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: idx * 0.08 }}
                      className={cn(
                        "flex items-center justify-between gap-4 rounded-3xl border-2 p-4 sm:p-5",
                        short ? "border-sand-400/50 bg-sand-300/10" : idle ? "border-ink-600 bg-ink-800/70" : "border-mint-700/60 bg-mint-500/10",
                      )}
                    >
                      <div className="min-w-0">
                        <p className="font-extrabold text-sand-100">{w.name}</p>
                        <p className="truncate text-sm text-sage-400">{w.payroll}</p>
                      </div>
                      <div className="text-right">
                        <p className="font-display text-2xl font-semibold tabular-nums text-sand-100">{formatMoney(w.balance)}</p>
                        <p className={cn("inline-flex items-center gap-1 text-sm font-extrabold",
                          short ? "text-sand-300" : idle ? "text-sage-400" : "text-mint-400")}>
                          {short ? <PauseCircle className="size-4" /> : <CheckCircle2 className="size-4" />}
                          {short ? `Held · short ${formatMoney(w.needed - w.balance)}` : idle ? "Nothing to pay" : "Can pay"}
                        </p>
                      </div>
                    </motion.li>
                  );
                })}
                <li className="rounded-3xl bg-ink-950/50 p-4 text-sm font-semibold text-sage-300">
                  <span className="font-extrabold text-coral-400">Audit log:</span> Refusing to draw on Cobalt Retail&apos;s
                  wallet for Borealis Labs — funds are tenant-scoped.
                </li>
              </motion.ul>
            )}
          </AnimatePresence>
        </div>
      </div>
    </section>
  );
}
