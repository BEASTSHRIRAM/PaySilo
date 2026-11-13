"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Lock } from "lucide-react";

import { LottieAnimation, type LottieName } from "@/components/brand/lottie";
import { cn } from "@/lib/utils";
import { HeroActions } from "./session";
import { gsap, prefersReducedMotion, ScrollTrigger } from "./motion";

const AGENTS = ["Intake", "Risk Gate", "Pricing", "Policy", "Approval", "Payout", "Reconcile", "Receipt"];

const SAYS = [
  "Reading the sheet.",
  "No odd bank changes.",
  "Fees first.",
  "Borealis is short. Holding 2.",
  "Over to you to sign.",
  "Paying from each silo.",
  "Matching the books.",
  "Done. Receipt saved.",
];

const MASCOT: LottieName[] = ["silo-idle", "silo-idle", "silo-idle", "silo-shy", "silo-wave", "silo-idle", "silo-idle", "silo-cheer"];

const SILOS = [
  { id: "acme", name: "Acme", amount: "EUR 12,400", start: 0.62, end: 0.36 },
  { id: "borealis", name: "Borealis", amount: "USD 1,150", start: 0.24, end: 0.24 },
  { id: "cobalt", name: "Cobalt", amount: "USD 10,000", start: 0.82, end: 0.5 },
];

const STEP = 1.25;

function RunCard() {
  const [stage, setStage] = useState(7);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = root.current;
    if (!el || prefersReducedMotion()) return;
    const ctx = gsap.context(() => {
      const q = gsap.utils.selector(el);
      const at = (i: number) => 0.15 + i * STEP;
      const tl = gsap.timeline({ repeat: -1, repeatDelay: 0.6, paused: true });
      SILOS.forEach((s) => tl.set(q(`[data-fill=${s.id}]`), { scaleY: s.start }, 0));
      AGENTS.forEach((_, i) => tl.call(() => setStage(i), undefined, at(i)));
      tl.fromTo(q("[data-fee]"), { opacity: 0, y: 6 }, { opacity: 1, y: 0, duration: 0.4 }, at(2));
      tl.fromTo(q("[data-held]"), { opacity: 0, y: 6 }, { opacity: 1, y: 0, duration: 0.4 }, at(3));
      tl.to(q("[data-tank=borealis]"), { x: 3, duration: 0.07, yoyo: true, repeat: 5 }, at(3) + 0.3);
      tl.to(q("[data-fill=acme]"), { scaleY: SILOS[0].end, duration: 0.9, ease: "power2.inOut" }, at(5));
      tl.to(q("[data-fill=cobalt]"), { scaleY: SILOS[2].end, duration: 0.9, ease: "power2.inOut" }, at(5));
      tl.to(q("[data-fee],[data-held]"), { opacity: 0, duration: 0.3 }, at(8) + 0.6);
      SILOS.forEach((s) => tl.to(q(`[data-fill=${s.id}]`), { scaleY: s.start, duration: 0.5 }, at(8) + 0.6));
      const st = ScrollTrigger.create({
        trigger: el,
        start: "top bottom",
        end: "bottom top",
        onToggle: (self) => (self.isActive ? tl.play() : tl.pause()),
      });
      return () => st.kill();
    }, el);
    return () => ctx.revert();
  }, []);

  return (
    <div ref={root} className="relative">
      <div className="rounded-[2rem] border border-sand-200/10 bg-ink-850/90 p-4 shadow-[0_40px_80px_-40px_rgba(0,0,0,0.9)] sm:p-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-xs font-extrabold uppercase tracking-widest text-sage-500">Payroll run</p>
            <p className="font-display text-lg font-semibold text-sand-100">Vertex Payroll Services</p>
            <p aria-live="polite" className="mt-3 inline-block rounded-2xl rounded-tl-sm bg-sand-200 px-3.5 py-2 text-sm font-bold text-ink-800">
              {SAYS[stage]}
            </p>
          </div>
          <div className="relative -mr-1 -mt-2 size-24 shrink-0 sm:-mr-2 sm:-mt-3 sm:size-36">
            <LottieAnimation name={MASCOT[stage]} className="absolute inset-0" label="Silo, the PaySilo mascot" />
          </div>
        </div>

        <ol className="mt-4 grid grid-cols-4 gap-1.5 sm:mt-5 sm:gap-2" aria-label="Agent steps">
          {AGENTS.map((a, i) => {
            const done = i < stage;
            const on = i === stage;
            return (
              <li
                key={a}
                data-state={on ? "on" : done ? "done" : "idle"}
                className={cn(
                  "flex min-w-0 flex-col gap-1 rounded-xl border px-1.5 py-2 text-[10px] font-extrabold leading-none sm:px-2 transition-colors duration-300 sm:text-xs",
                  on && "border-coral-500 bg-coral-500 text-sand-50",
                  done && "border-mint-500/40 bg-mint-500/10 text-mint-400",
                  !on && !done && "border-sand-200/10 text-sage-500",
                )}
              >
                <span className="flex items-center justify-between opacity-80">
                  {String(i + 1).padStart(2, "0")}
                  {done && <Check className="size-3" strokeWidth={4} />}
                </span>
                <span className="truncate">{a}</span>
              </li>
            );
          })}
        </ol>

        <div className="mt-4 grid grid-cols-3 gap-3 sm:mt-5 sm:gap-4">
          {SILOS.map((s) => (
            <div key={s.id} className="min-w-0">
              <div data-tank={s.id} className="relative h-24 overflow-hidden rounded-2xl border-2 border-sand-200/15 bg-ink-950/60 sm:h-32">
                <div
                  data-fill={s.id}
                  className={cn("absolute inset-0 origin-bottom will-change-transform", s.id === "borealis" ? "bg-clay-500" : "bg-sage-500")}
                  style={{ transform: `scaleY(${s.end})` }}
                />
                {s.id === "borealis" && (
                  <span data-held className="absolute inset-x-1.5 top-2 rounded-lg bg-sand-200 px-1.5 py-1 text-center text-[10px] font-extrabold uppercase text-clay-600">
                    2 held
                  </span>
                )}
                {s.id === "acme" && (
                  <span data-fee className="absolute inset-x-1.5 top-2 rounded-lg bg-ink-950/80 px-1.5 py-1 text-center text-[10px] font-extrabold text-sand-200">
                    fees 25.70
                  </span>
                )}
                <Lock className="absolute bottom-1.5 right-1.5 size-3 text-ink-950/70" />
              </div>
              <p className="mt-2 truncate text-sm font-extrabold text-sand-100">{s.name}</p>
              <p className="truncate text-xs font-semibold text-sage-500">{s.amount}</p>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs font-semibold text-sage-500">Three wallets. Money never crosses between them.</p>
      </div>
    </div>
  );
}

export function Hero() {
  const section = useRef<HTMLElement>(null);
  const card = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = section.current;
    if (!el || prefersReducedMotion()) return;
    const ctx = gsap.context(() => {
      const scrub = { trigger: el, start: "top top", end: "bottom top", scrub: true };
      gsap.to("[data-par='slow']", { yPercent: 18, ease: "none", scrollTrigger: scrub });
      gsap.to("[data-par='fast']", { yPercent: -10, ease: "none", scrollTrigger: scrub });
      const rx = gsap.quickTo(card.current, "rotationY", { duration: 0.8, ease: "power3" });
      const ry = gsap.quickTo(card.current, "rotationX", { duration: 0.8, ease: "power3" });
      const move = (e: PointerEvent) => {
        rx((e.clientX / window.innerWidth - 0.5) * 6);
        ry(-(e.clientY / window.innerHeight - 0.5) * 5);
      };
      el.addEventListener("pointermove", move);
      return () => el.removeEventListener("pointermove", move);
    }, el);
    return () => ctx.revert();
  }, []);

  return (
    <section ref={section} className="relative overflow-hidden pb-12 pt-28 sm:pt-36 lg:pb-14">
      <div className="bg-grid pointer-events-none absolute inset-0 [mask-image:radial-gradient(ellipse_at_top,black_30%,transparent_75%)]" />
      <div data-par="slow" aria-hidden className="pointer-events-none absolute -right-48 top-16 size-[560px] rounded-full border border-sand-200/[0.06]" />
      <div data-par="fast" aria-hidden className="pointer-events-none absolute -left-32 top-72 size-[360px] rounded-full border border-coral-500/20" />

      <div className="container relative grid items-center gap-8 sm:gap-14 lg:grid-cols-[1.05fr_0.95fr]">
        <div>
          <h1 className="rise font-display text-[2.5rem] font-semibold leading-[1.04] tracking-tight text-sand-100 text-balance sm:text-6xl lg:text-7xl">
            Payroll for every client. <span className="text-coral-400">Never a mixed penny.</span>
          </h1>
          <p className="rise mt-4 max-w-xl text-base leading-relaxed sm:mt-6 sm:text-lg text-sage-300 [animation-delay:120ms]">
            A crew of agents prepares, prices and pays contractor payroll for each client from that client&apos;s own wallet.
            You sign every run.
          </p>
          <HeroActions />
        </div>

        <div className="rise mx-auto w-full max-w-[500px] [animation-delay:200ms] [perspective:1200px]">
          <div ref={card} className="[transform-style:preserve-3d]">
            <RunCard />
          </div>
        </div>
      </div>
    </section>
  );
}
