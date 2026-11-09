import { LottieAnimation } from "@/components/brand/lottie";

const PARTNERS = [
  { name: "Airwallex", job: "Connected accounts, FX, payouts, charges" },
  { name: "Visa VIC", job: "Agent-purchased employer benefits via Airi" },
  { name: "Metal", job: "Reconciliation receipts anchored" },
  { name: "Claude", job: "The Intake Agent's reader" },
  { name: "AWS Bedrock AgentCore", job: "Agent runtime" },
  { name: "LangGraph", job: "Multi-agent orchestration" },
  { name: "Supabase", job: "Postgres with RLS" },
];

export function PartnerMarquee() {
  const row = [...PARTNERS, ...PARTNERS];
  return (
    <div className="relative overflow-hidden border-y border-ink-700 bg-ink-900/60 py-5">
      <div className="pointer-events-none absolute inset-y-0 left-0 z-10 w-24 bg-gradient-to-r from-background to-transparent" />
      <div className="pointer-events-none absolute inset-y-0 right-0 z-10 w-24 bg-gradient-to-l from-background to-transparent" />
      <ul className="flex w-max animate-marquee gap-10 pr-10">
        {row.map((p, idx) => (
          <li key={`${p.name}-${idx}`} aria-hidden={idx >= PARTNERS.length} className="flex items-center gap-3 whitespace-nowrap">
            <span className="size-2 rounded-full bg-coral-500" />
            <span className="font-display text-lg font-semibold text-sand-100">{p.name}</span>
            <span className="text-sm font-semibold text-sage-500">{p.job}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function SiloScene() {
  return (
    <section id="partners" className="relative scroll-mt-28 py-24">
      <div className="container grid items-center gap-12 lg:grid-cols-2">
        <div className="order-2 lg:order-1">
          <div className="mx-auto aspect-[7/5] w-full max-w-lg rounded-[2.5rem] border-2 border-ink-700 bg-ink-850/70 p-6">
            <LottieAnimation name="silos" className="size-full" label="Coins dropping into separate locked silos" />
          </div>
        </div>
        <div className="order-1 lg:order-2">
          <p className="text-sm font-extrabold uppercase tracking-widest text-coral-400">Built with the hackathon partners</p>
          <h2 className="mt-3 font-display text-4xl font-semibold text-sand-100 text-balance sm:text-5xl">
            Every coin lands in its own silo.
          </h2>
          <p className="mt-4 text-lg text-sage-300">
            Each partner has one concrete job in PaySilo — none of them is decoration.
          </p>
          <dl className="mt-8 grid gap-3 sm:grid-cols-2">
            {PARTNERS.slice(0, 6).map((p) => (
              <div key={p.name} className="rounded-2xl border-2 border-ink-700 bg-ink-900/60 px-4 py-3">
                <dt className="font-extrabold text-sand-100">{p.name}</dt>
                <dd className="text-sm text-sage-400">{p.job}</dd>
              </div>
            ))}
          </dl>
        </div>
      </div>
    </section>
  );
}
