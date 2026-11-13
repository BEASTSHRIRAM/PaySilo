"use client";

import type { ReactNode, RefObject } from "react";
import { Check, Lock, X } from "lucide-react";

import { SiloMark } from "@/components/brand/logo";
import { cn } from "@/lib/utils";
import { gsap, usePlayOnView } from "./motion";

function Shell({ title, body, className, tileRef, children, center, solo }: { title: string; body: string; className?: string; center?: boolean; solo?: boolean; tileRef: RefObject<HTMLElement | null>; children: ReactNode }) {
  return (
    <article
      ref={tileRef as RefObject<HTMLElement>}
      className={cn(
        "group relative flex flex-col overflow-hidden rounded-[1.75rem] border border-sand-200/10 bg-ink-850 p-5 transition-colors duration-300 hover:border-sand-200/25 md:min-h-[215px] sm:p-6",
        !solo && "max-md:w-[84vw] max-md:max-w-[340px] max-md:shrink-0 max-md:snap-start",
        className,
      )}
    >
      <h3 className="font-display text-xl font-semibold text-sand-100">{title}</h3>
      <p className="mt-1 max-w-sm text-sm leading-relaxed text-sage-300">{body}</p>
      <div className={cn("relative mt-4 flex min-h-0 flex-1 flex-col", center ? "justify-center" : "justify-end max-md:justify-center")}>{children}</div>
    </article>
  );
}

/* 1. Silos never mix */
function SilosTile() {
  const ref = usePlayOnView((root, q) => {
    const tl = gsap.timeline();
    const col3 = q("[data-col3]")[0] as HTMLElement;
    tl.from(q("[data-sfill]"), { scaleY: 0, duration: 0.9, stagger: 0.12, ease: "power2.out" })
      .from(q("[data-coin]"), { y: -60, opacity: 0, duration: 0.5, ease: "bounce.out" }, 0.5)
      .from(q("[data-dep]"), { opacity: 0, y: 6, duration: 0.3 }, 1)
      .to(q("[data-sfill=cobalt]"), { scaleY: 0.88, duration: 0.6 }, 1)
      .to(q("[data-coin]"), { x: () => -(col3.offsetWidth / 2 + 4), duration: 0.7, ease: "power2.in" }, 1.8)
      .to(q("[data-wall]"), { backgroundColor: "#874F41", scale: 1.25, duration: 0.15, yoyo: true, repeat: 3 }, 2.45)
      .to(q("[data-coin]"), { x: 0, duration: 0.6, ease: "elastic.out(1,0.5)" }, 2.5)
      .from(q("[data-blocked]"), { opacity: 0, y: 8, scale: 0.9, duration: 0.35, ease: "back.out(2)" }, 2.5);
    void root;
    return tl;
  });
  const wallets = [
    { id: "acme", name: "Acme", amt: "EUR 12,400", lvl: 0.55 },
    { id: "borealis", name: "Borealis", amt: "USD 1,150", lvl: 0.24 },
    { id: "cobalt", name: "Cobalt", amt: "USD 10,000", lvl: 0.88 },
  ];
  return (
    <Shell tileRef={ref} title="Silos never mix" body="Every client has its own wallet. A transfer between two silos is refused in code, not by policy." className="md:col-span-2 lg:row-span-2" solo>
      <div className="relative flex h-full min-h-[150px] items-stretch gap-4 pt-10 md:min-h-[200px]">
        {wallets.map((w) => (
          <div key={w.id} data-col3={w.id === "cobalt" ? "" : undefined} className="relative flex min-w-0 flex-1 flex-col">
            <div className="relative flex-1 overflow-hidden rounded-2xl border-2 border-sand-200/15 bg-ink-950/60">
              <div data-sfill={w.id} className={cn("absolute inset-0 origin-bottom", w.id === "borealis" ? "bg-clay-500" : "bg-sage-500")} style={{ transform: `scaleY(${w.lvl})` }} />
              <Lock className="absolute right-2 top-2 size-4 text-sand-100/80" />
              {w.id === "cobalt" && <span data-dep className="absolute inset-x-0 bottom-2 text-center text-[11px] font-extrabold text-ink-950">+USD 10,000</span>}
            </div>
            <p className="mt-2 truncate text-sm font-extrabold text-sand-100">{w.name}</p>
            <p className="truncate text-xs font-semibold text-sage-500">{w.amt}</p>
            {w.id === "cobalt" && (
              <>
                <span data-wall className="absolute -left-[10px] top-1/3 h-16 w-1.5 -translate-x-1/2 rounded-full bg-sand-200/40" />
                <span data-coin className="absolute -top-8 left-1/2 grid size-8 -translate-x-1/2 place-items-center rounded-full bg-coral-500 text-xs font-extrabold text-sand-50 shadow-[0_3px_0_0_theme(colors.coral.700)]">$</span>
                
              </>
            )}
          </div>
        ))}
        <span data-blocked className="absolute left-1/2 top-0 -translate-x-1/2 whitespace-nowrap rounded-full bg-sand-200 px-3 py-1 text-xs font-extrabold text-clay-600">
          Blocked: different silo
        </span>
      </div>
    </Shell>
  );
}

/* 2. Held lines + auto continue */
const LINES = [
  { who: "Maya R.", amt: "EUR 1,200", held: false },
  { who: "Tomas K.", amt: "EUR 800", held: false },
  { who: "Lena V.", amt: "USD 2,100", held: true },
  { who: "Dev S.", amt: "USD 640", held: true },
];
function HeldTile() {
  const ref = usePlayOnView((root, q) => {
    const tl = gsap.timeline();
    tl.from(q("[data-line]"), { opacity: 0, x: -14, duration: 0.35, stagger: 0.1 })
      .from(q("[data-toast]"), { opacity: 0, y: -14, duration: 0.4, ease: "back.out(1.7)" }, 1.1)
      .to(q("[data-held-pill]"), { rotationX: 90, duration: 0.25, stagger: 0.08 }, 2)
      .from(q("[data-approve]"), { opacity: 0, scale: 0.8, duration: 0.3, ease: "back.out(2)" }, 2.1)
      .from(q("[data-tick]"), { scale: 0, duration: 0.3, ease: "back.out(3)" }, 2.7)
      .fromTo(q("[data-paid-pill]"), { rotationX: -90 }, { rotationX: 0, duration: 0.3, stagger: 0.1, ease: "back.out(1.6)" }, 3);
    gsap.set(q("[data-held-pill]"), { transformPerspective: 400 });
    gsap.set(q("[data-paid-pill]"), { transformPerspective: 400 });
    void root;
    return tl;
  });
  return (
    <Shell tileRef={ref} title="Held lines continue by themselves" body="Short of funds? Silo pays what fits and holds the rest. When money lands, the held lines resume." className="md:col-span-2" solo>
      <div className="relative grid grid-cols-2 gap-2">
        {LINES.map((l) => (
          <div key={l.who} data-line className="flex items-center justify-between gap-2 rounded-xl border border-sand-200/10 bg-ink-950/50 px-3 py-2.5">
            <div className="min-w-0">
              <p className="truncate text-sm font-extrabold text-sand-100">{l.who}</p>
              <p className="text-xs font-semibold text-sage-500">{l.amt}</p>
            </div>
            <div className="relative h-7 w-14 shrink-0 sm:w-[68px]">
              {l.held ? (
                <>
                  <span data-held-pill className="absolute inset-0 grid place-items-center rounded-full bg-clay-500 text-[11px] font-extrabold uppercase text-sand-100">Held</span>
                  <span data-paid-pill className="absolute inset-0 grid place-items-center rounded-full bg-mint-500 text-[11px] font-extrabold uppercase text-ink-950">Paid</span>
                </>
              ) : (
                <span className="absolute inset-0 grid place-items-center rounded-full bg-mint-500 text-[11px] font-extrabold uppercase text-ink-950">Paid</span>
              )}
            </div>
          </div>
        ))}
        <div data-toast className="flex items-center justify-between gap-2 rounded-xl bg-sand-200 px-2.5 py-2 text-[12px] font-extrabold text-ink-800 sm:text-sm col-span-2">
          <span className="whitespace-nowrap">Funds arrive: USD 2,740</span>
          <span data-approve className="inline-flex shrink-0 items-center whitespace-nowrap gap-1 rounded-full bg-ink-800 px-2 py-1 text-[10px] uppercase tracking-wide text-sand-100">
            You approve
            <span data-tick className="grid size-4 place-items-center rounded-full bg-mint-500 text-ink-950">
              <Check className="size-3" strokeWidth={4} />
            </span>
          </span>
        </div>
      </div>
    </Shell>
  );
}

/* 3. Risk gate */
function RiskTile() {
  const ref = usePlayOnView((root, q) => {
    const tl = gsap.timeline();
    tl.from(q("[data-risk-row]"), { opacity: 0, y: 10, duration: 0.4 })
      .from(q("[data-risk-tag]"), { opacity: 0, x: -10, duration: 0.3 }, 0.5)
      .fromTo(q("[data-scan]"), { xPercent: -100, opacity: 1 }, { xPercent: 420, duration: 0.9, ease: "power1.inOut" }, 0.8)
      .to(q("[data-scan]"), { opacity: 0, duration: 0.1 }, 1.7)
      .from(q("[data-stamp]"), { scale: 2.4, rotation: -14, opacity: 0, duration: 0.35, ease: "power3.in" }, 1.5);
    void root;
    return tl;
  });
  return (
    <Shell tileRef={ref} title="Risk gate" body="A new bank plus a big payment stops for review." className="" center>
      <div data-risk-row className="relative overflow-hidden rounded-xl border border-sand-200/10 bg-ink-950/50 px-3 py-3">
        <p className="text-sm font-extrabold text-sand-100">Dev S. · EUR 6,000</p>
        <p data-risk-tag className="mt-1 inline-block rounded-md bg-clay-500/25 px-1.5 py-0.5 text-[11px] font-bold text-clay-300">Bank changed 2 days ago</p>
        <span data-scan className="pointer-events-none absolute inset-y-0 left-0 w-6 bg-coral-500/40" />
        <span data-stamp className="absolute right-2 top-2 -rotate-6 rounded-md border-2 border-clay-300 px-1.5 py-0.5 text-xs font-extrabold uppercase text-clay-300">Held</span>
      </div>
    </Shell>
  );
}

/* 4. Proof of payment */
function qrCells() {
  const n = 11;
  const cells: [number, number][] = [];
  const finder = (x: number, y: number) => (x < 3 && y < 3) || (x > n - 4 && y < 3) || (x < 3 && y > n - 4);
  for (let y = 0; y < n; y++)
    for (let x = 0; x < n; x++) {
      if (finder(x, y) || ((x * 7 + y * 13 + x * y * 3) % 5 < 2 && !(x < 4 && y < 4) && !(x > n - 5 && y < 4) && !(x < 4 && y > n - 5))) cells.push([x, y]);
    }
  return { cells, n };
}
const QR = qrCells();

function ProofTile() {
  const ref = usePlayOnView((root, q) => {
    const tl = gsap.timeline();
    tl.from(q("[data-doc]"), { y: 30, opacity: 0, rotation: 2, duration: 0.6, ease: "power3.out" })
      .from(q("[data-doc-line]"), { scaleX: 0, transformOrigin: "left", duration: 0.35, stagger: 0.08 }, 0.4)
      .from(q("[data-qr] rect"), { opacity: 0, duration: 0.25, stagger: { each: 0.004, from: "random" } }, 0.7)
      .fromTo(q("[data-qscan]"), { yPercent: -100, opacity: 1 }, { yPercent: 1050, duration: 1, ease: "power1.inOut" }, 1.5)
      .to(q("[data-qscan]"), { opacity: 0, duration: 0.1 }, 2.5)
      .from(q("[data-verified]"), { scale: 0.5, opacity: 0, duration: 0.4, ease: "back.out(2.4)" }, 2.4);
    void root;
    return tl;
  });
  return (
    <Shell tileRef={ref} title="Proof of payment" body="A PDF with a QR code. Anyone can scan it to verify." className="lg:row-span-2">
      <div data-doc className="relative rounded-2xl bg-sand-100 p-4 text-ink-800 shadow-[0_18px_40px_-22px_rgba(0,0,0,0.9)]">
        <p className="text-[10px] font-extrabold uppercase tracking-widest text-clay-600">Proof of payment</p>
        <p className="font-display text-base font-semibold">EUR 6,000.00</p>
        <div className="mt-2 space-y-1.5">
          <div data-doc-line className="h-1.5 w-full rounded-full bg-ink-800/15" />
          <div data-doc-line className="h-1.5 w-4/5 rounded-full bg-ink-800/15" />
          <div data-doc-line className="h-1.5 w-3/5 rounded-full bg-ink-800/15" />
        </div>
        <div className="relative mx-auto mt-4 size-28 overflow-hidden rounded-lg bg-sand-50 p-1.5">
          <svg data-qr viewBox={`0 0 ${QR.n} ${QR.n}`} className="size-full fill-ink-800" aria-label="QR code">
            {QR.cells.map(([x, y]) => (
              <rect key={`${x}-${y}`} x={x} y={y} width="1" height="1" />
            ))}
          </svg>
          <span data-qscan className="absolute inset-x-0 top-0 h-2 bg-coral-500/70" />
        </div>
        <span data-verified className="absolute -bottom-3 left-1/2 inline-flex -translate-x-1/2 items-center gap-1 rounded-full bg-mint-500 px-3 py-1 text-xs font-extrabold uppercase text-ink-950 shadow-[0_3px_0_0_theme(colors.mint.700)]">
          <Check className="size-3.5" strokeWidth={4} /> Verified
        </span>
      </div>
    </Shell>
  );
}

/* 5. Workflow builder */
function WorkflowTile() {
  const ref = usePlayOnView((root, q) => {
    const tl = gsap.timeline();
    tl.from(q("[data-node]"), { scale: 0.6, opacity: 0, duration: 0.4, stagger: 0.28, ease: "back.out(2)" })
      .from(q("[data-wire]"), { scaleX: 0, transformOrigin: "left", duration: 0.28, stagger: 0.28 }, 0.2)
      .from(q("[data-sched]"), { y: 16, opacity: 0, duration: 0.4, stagger: 0.15 }, 1.3)
      .from(q("[data-only]"), { opacity: 0, duration: 0.4 }, 1.8);
    void root;
    return tl;
  });
  const nodes = ["Schedule", "Intake", "Pricing", "Approve"];
  return (
    <Shell tileRef={ref} title="Workflows you build once" body="Wire the steps, set a schedule per client, and only approve." className="md:col-span-2">
      <div className="rounded-2xl border border-sand-200/10 bg-ink-950/50 p-4">
        <div className="flex items-center">
          {nodes.map((n, i) => (
            <div key={n} className="flex min-w-0 flex-[1_1_auto] items-center last:flex-none">
              <span
                data-node
                className={cn(
                  "shrink-0 rounded-lg border px-1.5 py-1.5 text-[11px] font-extrabold sm:rounded-xl sm:px-3 sm:py-2 sm:text-xs",
                  n === "Approve" ? "border-coral-500 bg-coral-500 text-sand-50" : "border-sage-400/40 bg-ink-800 text-sand-100",
                )}
              >
                {n}
              </span>
              {i < nodes.length - 1 && <span data-wire className="mx-0.5 h-0.5 min-w-1 flex-1 sm:mx-1 bg-sage-500" />}
            </div>
          ))}
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          {["Acme · 3rd", "Harborview · 4th"].map((c) => (
            <span key={c} data-sched className="rounded-full bg-sand-200 px-3 py-1.5 text-xs font-extrabold text-ink-800">
              {c}
            </span>
          ))}
          <span data-only className="text-xs font-bold text-sage-300">You only approve.</span>
        </div>
      </div>
    </Shell>
  );
}

/* 6. Funding planner */
function FundingTile() {
  const ref = usePlayOnView((root, q) => {
    const tl = gsap.timeline();
    tl.from(q("[data-funded]"), { scaleX: 0, transformOrigin: "left", duration: 0.9, ease: "power2.out" })
      .from(q("[data-gap]"), { opacity: 0, duration: 0.3 }, 0.8)
      .to(q("[data-gap]"), { opacity: 0.55, duration: 0.5, yoyo: true, repeat: 3 }, 1.1)
      .from(q("[data-fundby]"), { y: 14, opacity: 0, duration: 0.4, ease: "back.out(2)" }, 1);
    void root;
    return tl;
  });
  return (
    <Shell tileRef={ref} title="Funding planner" body="Shows the gap and the date to close it." className="" center>
      <div>
        <div className="flex h-5 overflow-hidden rounded-full bg-ink-950/60">
          <div data-funded className="h-full w-[70%] bg-sage-500" />
          <div data-gap className="hatch h-full flex-1" />
        </div>
        <div className="mt-2 flex justify-between text-[11px] font-bold text-sage-500">
          <span>Funded</span>
          <span className="text-clay-300">Gap</span>
        </div>
        <p data-fundby className="mt-3 inline-block rounded-xl bg-sand-200 px-3 py-1.5 text-xs font-extrabold text-ink-800">Fund the gap by the 3rd</p>
      </div>
    </Shell>
  );
}

/* 7. Benefits via Visa */
function BenefitsTile() {
  const ref = usePlayOnView((root, q) => {
    const tl = gsap.timeline();
    tl.from(q("[data-vcard]"), { x: -40, rotation: -8, opacity: 0, duration: 0.6, ease: "power3.out" })
      .from(q("[data-buy]"), { opacity: 0, x: 16, duration: 0.35, stagger: 0.9 }, 0.6)
      .to(q("[data-auth]"), { y: 2, duration: 0.1, yoyo: true, repeat: 1 }, 1.1)
      .from(q("[data-ok]"), { scale: 0, duration: 0.3, ease: "back.out(3)" }, 1.25)
      .from(q("[data-no]"), { scale: 0, duration: 0.3, ease: "back.out(3)" }, 2.1)
      .to(q("[data-buy-no]"), { x: 4, duration: 0.06, yoyo: true, repeat: 5 }, 2.1);
    void root;
    return tl;
  });
  return (
    <Shell tileRef={ref} title="Benefits on a Visa card" body="Kits, benefits and travel with a cap on every purchase." className="md:col-span-1 lg:col-span-2">
      <div className="grid items-center gap-3 min-[480px]:grid-cols-[150px_1fr] md:grid-cols-[170px_1fr]">
        <div data-vcard className="relative flex h-[112px] flex-col justify-between rounded-2xl bg-ink-600 p-3 ring-1 ring-sand-200/15">
          <div className="flex items-center justify-between">
            <span className="font-display text-lg font-semibold italic text-sand-100">Visa</span>
            <span className="h-5 w-7 rounded bg-sand-300/80" />
          </div>
          <p className="text-[11px] font-extrabold uppercase leading-tight tracking-wider text-sage-300">
            Cap per purchase
            <span className="block text-sm text-sand-100">USD 250</span>
          </p>
        </div>
        <div className="space-y-2">
          <div data-buy className="flex items-center justify-between gap-2 rounded-xl border border-sand-200/10 bg-ink-950/50 px-3 py-2 text-sm">
            <span className="font-extrabold text-sand-100">Laptop stand · USD 180</span>
            <span data-auth className="inline-flex items-center gap-1 rounded-full bg-mint-500 px-2 py-0.5 text-[11px] font-extrabold uppercase text-ink-950">
              <span data-ok><Check className="size-3" strokeWidth={4} /></span> OK
            </span>
          </div>
          <div data-buy data-buy-no className="flex items-center justify-between gap-2 rounded-xl border border-sand-200/10 bg-ink-950/50 px-3 py-2 text-sm">
            <span className="font-extrabold text-sand-100">Desk · USD 420</span>
            <span className="inline-flex items-center gap-1 rounded-full bg-clay-500 px-2 py-0.5 text-[11px] font-extrabold uppercase text-sand-100">
              <span data-no><X className="size-3" strokeWidth={4} /></span> Over cap
            </span>
          </div>
        </div>
      </div>
    </Shell>
  );
}

/* 8. Ask Silo */
function AskTile() {
  const ref = usePlayOnView((root, q) => {
    const tl = gsap.timeline();
    tl.from(q("[data-you]"), { y: 14, opacity: 0, duration: 0.4 })
      .from(q("[data-typing]"), { opacity: 0, duration: 0.2 }, 0.7)
      .fromTo(q("[data-dot]"), { y: 0 }, { y: -4, duration: 0.25, yoyo: true, repeat: 3, stagger: 0.1 }, 0.7)
      .to(q("[data-typing]"), { opacity: 0, height: 0, marginTop: 0, duration: 0.2 }, 1.9)
      .from(q("[data-silo]"), { y: 14, opacity: 0, duration: 0.4 }, 2)
      .from(q("[data-act]"), { scale: 0.7, opacity: 0, duration: 0.3, stagger: 0.12, ease: "back.out(2)" }, 2.5);
    void root;
    return tl;
  });
  return (
    <Shell tileRef={ref} title="Ask Silo" body="Chat can read, propose and start a run. It can never approve." className="md:col-span-2">
      <div className="space-y-2.5 text-sm">
        <p data-you className="ml-auto w-fit max-w-[85%] rounded-2xl rounded-br-sm bg-ink-600 px-3.5 py-2 font-bold text-sand-100">Approve the run.</p>
        <div data-typing className="flex w-fit gap-1 rounded-2xl bg-sand-200/90 px-3 py-2.5">
          {[0, 1, 2].map((i) => (
            <span key={i} data-dot className="size-1.5 rounded-full bg-ink-700" />
          ))}
        </div>
        <div data-silo className="flex items-end gap-2">
          <span className="grid size-8 shrink-0 place-items-center rounded-full bg-sand-200"><SiloMark className="size-6" /></span>
          <div>
            <p className="max-w-[28rem] rounded-2xl rounded-bl-sm bg-sand-200 px-3.5 py-2 font-bold text-ink-800">
              I can&apos;t approve. Only you can sign. I can have it ready for you.
            </p>
            <div className="mt-2 flex gap-2">
              <span data-act className="rounded-full bg-coral-500 px-3 py-1 text-xs font-extrabold text-sand-50">Prepare the run</span>
              <span data-act className="rounded-full border border-sand-200/25 px-3 py-1 text-xs font-extrabold text-sand-200">Not now</span>
            </div>
          </div>
        </div>
      </div>
    </Shell>
  );
}

export function Bento() {
  return (
    <section id="features" className="relative scroll-mt-20 py-12 lg:py-14">
      <div className="container">
        <p className="text-sm font-extrabold uppercase tracking-widest text-coral-400">Around the run</p>
        <h2 className="mt-2 max-w-3xl font-display text-3xl font-semibold leading-tight text-sand-100 text-balance sm:text-5xl">
          Everything a payroll month needs, each with its own guard.
        </h2>
        <div className="mt-8 grid gap-3 md:mt-8 md:gap-4 md:grid-cols-2 lg:auto-rows-[minmax(215px,auto)] lg:grid-cols-4">
          <SilosTile />
          <HeldTile />
          <div className="-mx-5 flex scroll-px-5 snap-x snap-mandatory gap-3 overflow-x-auto overscroll-x-contain px-5 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden md:contents">
            <RiskTile />
            <ProofTile />
            <WorkflowTile />
            <FundingTile />
            <BenefitsTile />
            <AskTile />
          </div>
        </div>
      </div>
    </section>
  );
}
