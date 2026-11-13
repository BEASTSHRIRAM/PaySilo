"use client";

import { Bell } from "lucide-react";

import { gsap, usePlayOnView } from "./motion";

const SIG = "M6 42 C16 8 28 6 26 36 S38 54 52 24 S70 6 72 34 S92 48 110 20 C120 6 126 32 142 28 S160 14 174 30";

function Statement({ n, title, body, children, tileRef }: { n: string; title: string; body: string; children: React.ReactNode; tileRef: React.RefObject<HTMLElement | null> }) {
  return (
    <article ref={tileRef as React.RefObject<HTMLElement>} className="flex flex-col rounded-3xl border border-ink-800/15 bg-sand-100 p-6">
      <div className="relative grid h-32 place-items-center rounded-2xl bg-ink-800">{children}</div>
      <p className="mt-5 text-xs font-extrabold uppercase tracking-widest text-coral-600">{n}</p>
      <h3 className="mt-1 font-display text-2xl font-semibold leading-tight text-ink-800">{title}</h3>
      <p className="mt-2 text-sm leading-relaxed text-clay-600">{body}</p>
    </article>
  );
}

function CodeDecides() {
  const ref = usePlayOnView((_, q) => {
    const tl = gsap.timeline();
    tl.from(q("[data-shackle]"), { y: -12, duration: 0.7, ease: "bounce.out" })
      .from(q("[data-ledger]"), { opacity: 0, x: -10, stagger: 0.12, duration: 0.3 }, 0.2);
    return tl;
  });
  return (
    <Statement tileRef={ref} n="01" title="Code decides the money." body="Fees, balances and limits are checked by code. A model never picks an amount.">
      <div className="flex items-center gap-5">
        <svg viewBox="0 0 48 56" className="h-16 w-14" aria-hidden>
          <path data-shackle d="M13 24 V16 a11 11 0 0 1 22 0 V24" fill="none" className="stroke-sand-200" strokeWidth="5" strokeLinecap="round" />
          <rect x="6" y="24" width="36" height="28" rx="7" className="fill-coral-500" />
          <circle cx="24" cy="36" r="4" className="fill-ink-900" />
        </svg>
        <div className="space-y-1.5 text-xs font-extrabold text-sand-200">
          <p data-ledger>Fees priced first</p>
          <p data-ledger>Own wallet only</p>
          <p data-ledger>Reserve kept</p>
        </div>
      </div>
    </Statement>
  );
}

function HumanSigns() {
  const ref = usePlayOnView((_, q) => {
    const path = q("[data-sig]")[0] as unknown as SVGPathElement;
    const len = path.getTotalLength();
    const tl = gsap.timeline();
    tl.fromTo(path, { strokeDasharray: len, strokeDashoffset: len }, { strokeDashoffset: 0, duration: 1.5, ease: "power1.inOut" })
      .from(q("[data-signed]"), { opacity: 0, y: 6, duration: 0.3 }, 1.5);
    return tl;
  });
  return (
    <Statement tileRef={ref} n="02" title="A human signs." body="Every run needs your signature over the exact amounts. No signature, no money moves.">
      <div className="w-full px-6">
        <svg viewBox="0 0 180 56" className="h-16 w-full" aria-hidden>
          <path d="M6 50 H174" className="stroke-ink-500" strokeWidth="1.5" strokeDasharray="3 4" />
          <path data-sig d={SIG} className="stroke-sand-100" strokeWidth="3.5" strokeLinecap="round" fill="none" />
        </svg>
        <p data-signed className="text-center text-xs font-extrabold uppercase tracking-wider text-mint-400">Signed by you</p>
      </div>
    </Statement>
  );
}

function AgentDrafts() {
  const ref = usePlayOnView((_, q) => {
    const tl = gsap.timeline();
    tl.from(q("[data-draft]"), { scaleX: 0, transformOrigin: "left", duration: 0.35, stagger: 0.15 })
      .from(q("[data-bell]"), { scale: 0.6, opacity: 0, duration: 0.4, ease: "back.out(2.4)" }, 0.7)
      .to(q("[data-bell-icon]"), { rotation: 18, transformOrigin: "50% 0%", duration: 0.08, yoyo: true, repeat: 5 }, 1.1);
    return tl;
  });
  return (
    <Statement tileRef={ref} n="03" title="The agent drafts and reminds." body="It reads the sheets, writes the plan and nudges you when funds are short. The signature line stays empty.">
      <div className="flex w-full items-center gap-4 px-6">
        <div className="flex-1 space-y-1.5">
          <div data-draft className="h-2 w-full rounded-full bg-sage-500/60" />
          <div data-draft className="h-2 w-4/5 rounded-full bg-sage-500/60" />
          <div className="mt-3 rounded-md border border-dashed border-sand-200/40 px-2 py-1.5 text-center text-[10px] font-extrabold uppercase tracking-wider text-sand-200/70">Sign here</div>
        </div>
        <div data-bell className="grid size-14 shrink-0 place-items-center rounded-2xl bg-coral-500 text-sand-50">
          <span data-bell-icon><Bell className="size-6" /></span>
        </div>
      </div>
    </Statement>
  );
}

const PARTNERS = [
  { name: "Airwallex", line: "Holds each client's wallet and sends the payouts." },
  { name: "Visa", line: "Signs approvals, and issues benefit cards through Airi." },
  { name: "Amazon Bedrock AgentCore", line: "Re-computes the plan. It has to match." },
  { name: "LangGraph", line: "Runs the agent graph, step by step." },
  { name: "Claude", line: "Reads inputs and words messages. Never decides money." },
  { name: "Metal", line: "Receipt anchoring. A mock chain in the demo." },
];

export function Trust() {
  return (
    <section id="trust" className="relative scroll-mt-20 py-14 lg:py-20">
      <div className="container">
        <div className="rounded-[2.5rem] bg-sand-200 p-6 text-ink-800 sm:p-10 lg:p-14">
          <p className="text-sm font-extrabold uppercase tracking-widest text-coral-600">Trust</p>
          <h2 className="mt-3 max-w-3xl font-display text-4xl font-semibold leading-[1.05] text-ink-800 text-balance sm:text-6xl">
            The agent never holds the pen.
          </h2>
          <div className="mt-10 grid gap-4 md:grid-cols-3">
            <CodeDecides />
            <HumanSigns />
            <AgentDrafts />
          </div>
        </div>

        <div id="partners" className="mt-16 scroll-mt-24">
          <p className="text-sm font-extrabold uppercase tracking-widest text-sage-500">Built with</p>
          <ul className="mt-5 grid gap-x-8 gap-y-6 sm:grid-cols-2 lg:grid-cols-3">
            {PARTNERS.map((p) => (
              <li key={p.name} className="border-t border-sand-200/10 pt-4">
                <p className="font-display text-2xl font-semibold tracking-tight text-sand-100">{p.name}</p>
                <p className="mt-1 text-sm text-sage-300">{p.line}</p>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
