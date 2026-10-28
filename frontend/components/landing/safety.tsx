import { Fingerprint, Layers3, Lock, Repeat2, Receipt, Scale } from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";

const FEATURES = [
  { icon: Layers3, title: "Isolation in three layers", body: "API identity (x-on-behalf-of), tenant-scoped queries, and a ledger that rejects any transaction spanning two employers." },
  { icon: Repeat2, title: "Retries never pay twice", body: "Every money move has a deterministic request_id made by code — never by the model. Airwallex dedupes, we record." },
  { icon: Scale, title: "Fees first, reserve kept", body: "Payout fees, the run fee and a USD 1,000 reserve floor are priced before a cent moves. Unknown fee? Refused." },
  { icon: Fingerprint, title: "Visa TAP-signed approvals", body: "Ed25519 signatures over the exact run, amounts and pay lines. Tampered, replayed or unsigned? No money moves." },
  { icon: Receipt, title: "Receipts on Metal", body: "Each settled run's reconciliation receipt is hashed and anchored. Only hashes on-chain — never financial data." },
  { icon: Lock, title: "Supabase-ready, RLS on", body: "Postgres triggers guard the ledger; Row Level Security locks the public API out of every table." },
];

export function Safety() {
  return (
    <section id="safety" className="relative scroll-mt-28 overflow-hidden bg-sand-200 py-24 text-ink-800">
      <div className="bg-grain pointer-events-none absolute inset-0 opacity-[0.12] mix-blend-multiply" />
      <div className="container relative grid gap-12 lg:grid-cols-[0.8fr_1.2fr] lg:items-center">
        <div>
          <div className="size-48">
            <LottieAnimation name="shield" className="size-full" label="Signed approval shield" />
          </div>
          <p className="mt-4 text-sm font-extrabold uppercase tracking-widest text-coral-600">Safety is the product</p>
          <h2 className="mt-3 font-display text-4xl font-semibold leading-tight text-ink-800 text-balance sm:text-5xl">
            Rules written in code, not in a prompt.
          </h2>
          <p className="mt-4 text-lg leading-relaxed text-clay-600">
            No model can talk its way around a database trigger. PaySilo&apos;s money rules live where an LLM can&apos;t
            reach them — and every decision leaves a trail you can replay.
          </p>
        </div>
        <ul className="grid gap-4 sm:grid-cols-2">
          {FEATURES.map(({ icon: Icon, title, body }) => (
            <li key={title} className="rounded-3xl border-2 border-sand-400/70 bg-sand-100 p-5 shadow-[0_5px_0_0_theme(colors.sand.400)]">
              <span className="grid size-11 place-items-center rounded-2xl bg-ink-700 text-sand-100">
                <Icon className="size-5" />
              </span>
              <h3 className="mt-3 text-lg font-extrabold text-ink-800">{title}</h3>
              <p className="mt-1 text-[15px] leading-relaxed text-clay-600">{body}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
