"use client";

import { motion } from "framer-motion";
import { Calculator, Coins, FileText, Fingerprint, Link2, Scale, Server, ShieldAlert } from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { cn } from "@/lib/utils";

// Mirrors backend AGENTS (app/agents/graph.py). Only the Intake Agent uses an LLM.
const CREW = [
  { icon: FileText, name: "Intake Agent", kind: "LLM", job: "Reads payroll sheets and emails into pay lines. Powered by Claude.", partner: "Claude" },
  { icon: Calculator, name: "Pricing Agent", kind: "Code", job: "Salaries plus payout fees per currency, before any FX. No rate, no answer." },
  { icon: Scale, name: "Balance & Policy", kind: "Code", job: "Reads only this employer's wallet. Covers payroll, run fee and reserve — or holds." },
  { icon: Fingerprint, name: "Approval Gate", kind: "Human", job: "Pauses for your decision on the exact amounts, cryptographically signed.", partner: "Approval signing" },
  { icon: ShieldAlert, name: "Escalation", kind: "Human", job: "A short payroll is held and explained. Never covered from another silo." },
  { icon: Coins, name: "FX & Payout", kind: "Tools", job: "Converts and pays on the employer's behalf with idempotent request ids.", partner: "Airwallex" },
  { icon: Scale, name: "Fee & Reconcile", kind: "Code", job: "Charges the run fee only if payroll ran, then proves ledger = wallet." },
  { icon: Link2, name: "Attestation", kind: "Tools", job: "Anchors a hash of the reconciliation receipt on the settlement chain.", partner: "Metal" },
];

const KIND_STYLE: Record<string, string> = {
  LLM: "bg-coral-500/15 text-coral-300 border-coral-600/50",
  Code: "bg-sage-500/15 text-sage-300 border-sage-600/50",
  Human: "bg-sand-300/10 text-sand-300 border-sand-400/40",
  Tools: "bg-clay-500/20 text-clay-300 border-clay-400/50",
};

export function Crew() {
  return (
    <section id="crew" className="relative scroll-mt-28 py-24">
      <div className="container">
        <div className="grid items-center gap-10 rounded-[2.5rem] border-2 border-ink-700 bg-gradient-to-br from-ink-800 via-ink-850 to-ink-900 p-6 sm:p-10 lg:grid-cols-[0.8fr_1.2fr]">
          <div className="text-center lg:text-left">
            <div className="mx-auto size-56 lg:mx-0">
              <LottieAnimation name="silo-wave" className="size-full" label="Silo waving hello" />
            </div>
            <p className="mt-2 text-sm font-extrabold uppercase tracking-widest text-coral-400">Meet the crew</p>
            <h2 className="mt-3 font-display text-4xl font-semibold text-sand-100 text-balance">
              Silo leads. Each agent has one job.
            </h2>
            <p className="mt-4 text-lg text-sage-300">
              A LangGraph state machine, supervised by deterministic code — not by another model. One LLM reads; seven
              agents and a human decide, move and prove.
            </p>
            <div className="mt-6 inline-flex items-center gap-2 rounded-full bg-ink-950/60 px-4 py-2 text-sm font-bold text-sage-300">
              <Server className="size-4 text-coral-400" /> Wrapped for AWS Bedrock AgentCore
            </div>
          </div>

          <ul className="grid gap-3 sm:grid-cols-2">
            {CREW.map((agent, idx) => {
              const Icon = agent.icon;
              return (
                <motion.li
                  key={agent.name}
                  initial={{ opacity: 0, y: 16 }}
                  whileInView={{ opacity: 1, y: 0 }}
                  viewport={{ once: true }}
                  transition={{ delay: idx * 0.05 }}
                  whileHover={{ y: -4 }}
                  className="rounded-3xl border-2 border-ink-700 bg-ink-900/70 p-4"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="grid size-10 place-items-center rounded-2xl bg-ink-700 text-sand-200">
                      <Icon className="size-5" />
                    </span>
                    <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-extrabold uppercase tracking-wide", KIND_STYLE[agent.kind])}>
                      {agent.kind}
                    </span>
                  </div>
                  <h3 className="mt-3 font-extrabold text-sand-100">{agent.name}</h3>
                  <p className="mt-1 text-sm leading-relaxed text-sage-400">{agent.job}</p>
                  {agent.partner && <p className="mt-2 text-xs font-extrabold uppercase tracking-wider text-coral-400">{agent.partner}</p>}
                </motion.li>
              );
            })}
          </ul>
        </div>
      </div>
    </section>
  );
}
