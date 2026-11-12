"use client";

import { motion } from "framer-motion";
import { BadgeCheck, Banknote, Calculator, Fingerprint, Landmark, Link2, ScanSearch, Wallet } from "lucide-react";

import { cn } from "@/lib/utils";

const STEPS = [
  { icon: Landmark, title: "Activate", body: "Each employer gets its own Airwallex connected account. Its own silo.", tag: "Airwallex" },
  { icon: Wallet, title: "Fund", body: "Every employer tops up only its own wallet. Nothing is pooled.", tag: "x-on-behalf-of" },
  { icon: ScanSearch, title: "Read", body: "The Intake Agent turns a messy payroll email into clean pay lines.", tag: "Claude" },
  { icon: Calculator, title: "Price fees first", body: "Salaries + EUR 12.85 SWIFT fees + the run fee, before a single cent is converted.", tag: "Code" },
  { icon: Banknote, title: "Check its own silo", body: "Only this employer's wallet is read. Short? It's held, never borrowed.", tag: "Code" },
  { icon: Fingerprint, title: "Signed approval", body: "You approve the exact amounts. The decision is cryptographically signed and single-use.", tag: "Signatures" },
  { icon: BadgeCheck, title: "Convert, pay, reconcile", body: "FX + payouts on the employer's behalf, then the books must balance to close.", tag: "Airwallex" },
  { icon: Link2, title: "Anchor the receipt", body: "A hash of the reconciliation receipt is anchored as tamper-proof evidence.", tag: "Metal" },
];

const NODE_TONES = [
  "bg-sage-500 [--duo-edge:theme(colors.sage.700)] text-ink-900",
  "bg-clay-500 [--duo-edge:theme(colors.clay.700)] text-sand-100",
  "bg-coral-500 [--duo-edge:theme(colors.coral.700)] text-sand-50",
  "bg-sand-200 [--duo-edge:theme(colors.sand.400)] text-ink-800",
];

// Winding path offsets (in rem) — the Duolingo "lesson path".
const OFFSETS = [0, 4.5, 6.5, 4.5, 0, -4.5, -6.5, -4.5];

export function HowItWorks() {
  return (
    <section id="how" className="relative scroll-mt-28 py-24">
      <div className="container">
        <div className="mx-auto max-w-2xl text-center">
          <p className="text-sm font-extrabold uppercase tracking-widest text-coral-400">How it works</p>
          <h2 className="mt-3 font-display text-4xl font-semibold text-sand-100 text-balance sm:text-5xl">
            One payroll run, eight small steps
          </h2>
          <p className="mt-4 text-lg text-sage-300">
            The model reads. The code decides. The API moves the money. Each step is its own agent with one job.
          </p>
        </div>

        <ol className="relative mx-auto mt-16 max-w-3xl">
          {STEPS.map((step, idx) => {
            const Icon = step.icon;
            const left = idx % 2 === 0;
            return (
              <motion.li
                key={step.title}
                initial={{ opacity: 0, y: 24 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true, margin: "-80px" }}
                transition={{ type: "spring", stiffness: 220, damping: 22, delay: 0.04 }}
                className="relative flex flex-col items-center pb-10 last:pb-0"
              >
                <div className="flex w-full items-center justify-center md:[transform:translateX(var(--x))]"
                     style={{ ["--x" as string]: `${OFFSETS[idx]}rem` }}>
                  <div className={cn("flex w-full max-w-md items-center gap-5", !left && "md:flex-row-reverse md:text-right")}>
                    <div className={cn("relative grid size-20 shrink-0 place-items-center rounded-full shadow-duo", NODE_TONES[idx % 4])}>
                      <Icon className="size-9" strokeWidth={2.4} />
                      <span className="absolute -top-1 -right-1 grid size-7 place-items-center rounded-full bg-ink-950 text-xs font-black text-sand-100 ring-2 ring-ink-700">
                        {idx + 1}
                      </span>
                    </div>
                    <div className="rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4">
                      <div className={cn("flex items-center gap-2", !left && "md:justify-end")}>
                        <h3 className="font-display text-xl font-semibold text-sand-100">{step.title}</h3>
                        <span className="rounded-full bg-ink-700 px-2 py-0.5 text-[11px] font-extrabold uppercase tracking-wide text-sage-300">
                          {step.tag}
                        </span>
                      </div>
                      <p className="mt-1 text-sm leading-relaxed text-sage-300">{step.body}</p>
                    </div>
                  </div>
                </div>
                {idx < STEPS.length - 1 && (
                  <span aria-hidden className="mt-4 h-6 w-1.5 rounded-full bg-[repeating-linear-gradient(to_bottom,theme(colors.ink.600)_0_6px,transparent_6px_12px)]" />
                )}
              </motion.li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}
