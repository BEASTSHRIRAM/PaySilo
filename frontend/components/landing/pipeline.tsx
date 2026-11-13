"use client";

import { useEffect, useRef } from "react";
import { Check, Lock } from "lucide-react";

import { cn } from "@/lib/utils";
import { gsap, prefersReducedMotion, ScrollTrigger, useRefreshOnLayout } from "./motion";

const STAGES = [
  { short: "Intake", name: "Intake", partner: "Claude", body: "Reads your sheet or email and turns it into pay lines.", example: "acme_march.xlsx: 14 lines read, 1 new contractor." },
  { short: "Risk", name: "Risk Gate", partner: "Code rules", body: "Holds a line when the bank is unverified, or changed recently on a large payment.", example: "Bank changed 2 days ago, EUR 6,000: held for a human look." },
  { short: "Pricing", name: "Pricing", partner: "Airwallex", body: "Prices every fee first, before any FX.", example: "EUR 6,000 + EUR 25.70 fees, priced before any FX." },
  { short: "Policy", name: "Balance & Policy", partner: "Code rules", body: "Checks only that client's wallet. Pays what fits and holds the rest, all or nothing or partial by priority.", example: "Borealis is short: 3 lines paid, 2 held." },
  { short: "Approval", name: "Human approval", partner: "Visa TAP", body: "You review and sign the exact amounts. The agent can never approve or pay.", example: "Your signature covers these amounts only." },
  { short: "Payout", name: "FX & Payout", partner: "Airwallex", body: "Converts and pays each contractor from the client's own wallet.", example: "EUR 6,000 sent. Fee was EUR 25.70, as priced." },
  { short: "Reconcile", name: "Reconcile", partner: "Code rules", body: "Matches every payout back to the plan, line by line.", example: "Paid lines match the plan to the cent." },
  { short: "Receipt", name: "Receipt", partner: "Metal", body: "Writes the receipt and a proof of payment PDF with a QR anyone can verify.", example: "Receipt anchored. Proof PDF ready." },
];

const FORK_AFTER = 2; // fork sits right after Pricing
const GATE = 4; // Human approval

const SIG_PATH = "M6 40 C16 8 28 6 26 34 S38 52 50 22 S68 6 70 32 S88 46 106 20 C116 6 122 30 138 26 S156 12 170 28";

function Gate() {
  return (
    <div className="relative mt-4 h-[96px] rounded-2xl border border-sand-200/10 bg-ink-950/50">
      <svg viewBox="0 0 180 56" className="absolute bottom-1 left-2 h-[60px] w-[calc(100%-5.5rem)]" aria-hidden>
        <path d="M6 48 H174" className="stroke-ink-600" strokeWidth="1.5" strokeDasharray="3 4" fill="none" />
        <path data-sig d="M6 40 C16 8 28 6 26 34 S38 52 50 22 S68 6 70 32 S88 46 106 20 C116 6 122 30 138 26 S156 12 170 28" className="stroke-sand-100" strokeWidth="3" strokeLinecap="round" fill="none" />
      </svg>
      <div className="absolute inset-y-0 right-8 flex w-9 items-end justify-center pb-2">
        <span className="absolute bottom-2 h-1 w-9 rounded-full bg-sand-200/30" />
        <span data-arm className="absolute bottom-2 h-9 w-1.5 origin-bottom rounded-full bg-coral-500" style={{ transform: "rotate(86deg)" }} />
        <span data-post className="absolute bottom-2 h-3 w-3 rounded-full bg-sand-200" />
      </div>
      <span data-wait className="absolute left-3 top-2 text-[10px] font-extrabold uppercase tracking-wider text-coral-400 opacity-0">
        Waiting for your signature
      </span>
      <span data-signed className="absolute left-3 top-2 inline-flex items-center gap-1 text-[10px] font-extrabold uppercase tracking-wider text-mint-400">
        <Check className="size-3" strokeWidth={4} /> Signed. Gate open
      </span>
    </div>
  );
}

function Fork() {
  return (
    <li aria-label="Bedrock cross check" data-fork className="relative hidden h-[300px] w-[360px] shrink-0 motion-safe:lg:block">
      <span className="conn absolute right-full top-[2.2rem] h-0.5 w-12 bg-ink-600">
        <i className="block size-full origin-left bg-coral-500" />
      </span>
      <svg viewBox="0 0 360 300" className="absolute inset-0 h-full w-full" fill="none" aria-hidden>
        <path d="M0 35 H360" className="stroke-ink-600" strokeWidth="2" />
        <path d="M0 35 H360" className="fork-main stroke-coral-500" strokeWidth="2" />
        <path data-branch d="M30 35 C70 35 70 130 120 130 H240 C290 130 290 35 330 35" className="stroke-sage-400" strokeWidth="2" strokeDasharray="1000" />
      </svg>
      <div data-node className="absolute left-1/2 top-[130px] w-[230px] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-sage-400/50 bg-ink-800 px-3 py-2 text-center">
        <p className="text-sm font-extrabold text-sand-100">Amazon Bedrock AgentCore</p>
      </div>
      <div className="absolute inset-x-6 top-[168px] text-center">
        <p className="font-display text-lg font-semibold leading-snug text-sand-100">Same plan, re-computed on AWS.</p>
        <p className="mt-1 text-sm font-semibold text-sage-300">Must match to the cent, or the run stops.</p>
        <div data-match className="mt-3 inline-flex items-center gap-2 rounded-full bg-mint-500/15 px-3 py-1.5 text-xs font-extrabold text-mint-400">
          <Check className="size-3.5" strokeWidth={4} /> EUR 6,025.70 = EUR 6,025.70
        </div>
      </div>
    </li>
  );
}

function safeLen(p: SVGPathElement, fallback: number) {
  try {
    return p.getTotalLength() || fallback;
  } catch {
    return fallback;
  }
}

export function Pipeline() {
  const section = useRef<HTMLElement>(null);
  const pin = useRef<HTMLDivElement>(null);
  const track = useRef<HTMLOListElement>(null);
  useRefreshOnLayout();

  useEffect(() => {
    const root = section.current;
    const pinEl = pin.current;
    const trackEl = track.current;
    if (!root || !pinEl || !trackEl) return;
    const reduced = prefersReducedMotion();
    const q = gsap.utils.selector(root);

    const ctx = gsap.context(() => {
      const mm = gsap.matchMedia();
      const dots = q<HTMLButtonElement>("[data-dot]");
      const lis = q<HTMLElement>("[data-stage]");
      const cards = q<HTMLElement>(".stage-card");
      const fill = q<HTMLElement>("[data-railfill]")[0];
      const forkEl = q<HTMLElement>("[data-fork]")[0];
      const railName = q<HTMLElement>("[data-railname]")[0];
      const swipe = q<HTMLElement>("[data-swipe]")[0];

      // Mini animations. Under reduced motion nothing is built: the final state shows.
      const sig = q<SVGPathElement>("[data-sig]")[0];
      const sigLen = sig.getTotalLength();
      const branch = q<SVGPathElement>("[data-branch]")[0];
      const branchLen = safeLen(branch, 900);
      const branchM = q<SVGPathElement>("[data-branch-m]")[0];
      const branchMLen = safeLen(branchM, 260);
      let sigTl: gsap.core.Timeline | null = null;
      let forkTl: gsap.core.Timeline | null = null;
      let forkMTl: gsap.core.Timeline | null = null;
      if (!reduced) {
        gsap.set(branch, { strokeDasharray: branchLen });
        gsap.set(branchM, { strokeDasharray: branchMLen });
        gsap.set(q("[data-wait]"), { opacity: 1 });
        gsap.set(q("[data-signed]"), { opacity: 0 });
        sigTl = gsap.timeline({ paused: true, onComplete: () => refresh() });
        sigTl
          .fromTo(sig, { strokeDasharray: sigLen, strokeDashoffset: sigLen }, { strokeDashoffset: 0, duration: 1.4, ease: "power1.inOut" }, 0.2)
          .fromTo(q("[data-arm]"), { rotation: 0 }, { rotation: 86, duration: 0.6, ease: "back.out(1.6)" }, 1.7)
          .to(q("[data-wait]"), { opacity: 0, duration: 0.2 }, 1.7)
          .to(q("[data-signed]"), { opacity: 1, duration: 0.3 }, 1.9);
        forkTl = gsap.timeline({ paused: true });
        forkTl
          .fromTo(branch, { strokeDashoffset: branchLen }, { strokeDashoffset: 0, duration: 1.1, ease: "power2.inOut" }, 0)
          .fromTo(q("[data-node]"), { scale: 0.6, opacity: 0 }, { scale: 1, opacity: 1, duration: 0.5, ease: "back.out(1.7)" }, 0.5)
          .fromTo(q("[data-match]"), { opacity: 0, y: 8 }, { opacity: 1, y: 0, duration: 0.4 }, 1.1);
        forkMTl = gsap.timeline({ paused: true });
        forkMTl
          .fromTo(branchM, { strokeDashoffset: branchMLen }, { strokeDashoffset: 0, duration: 1, ease: "power2.inOut" }, 0)
          .fromTo(q("[data-node-m]"), { opacity: 0, y: 6 }, { opacity: 1, y: 0, duration: 0.4 }, 0.5)
          .fromTo(q("[data-match-m]"), { opacity: 0, scale: 0.8 }, { opacity: 1, scale: 1, duration: 0.4, ease: "back.out(2)" }, 1);
      }
      const signed = () => reduced || !sigTl || sigTl.progress() === 1;

      let refresh: () => void = () => {};
      let last = { idx: -2, fork: false, signed: !sigTl };
      let line = 0;
      let cidx = 0;

      const paint = (idx: number, locked: boolean) => {
        lis.forEach((li, i) => (li.dataset.reached = String(i <= idx)));
        cards.forEach((c, i) => {
          c.dataset.active = String(i === idx);
          c.dataset.locked = String(locked && i > GATE);
        });
        dots.forEach((d, i) => (d.dataset.reached = String(i <= idx)));
        if (fill) fill.style.transform = `scaleX(${Math.max(idx, 0) / (STAGES.length - 1)})`;
        if (railName && idx >= 0) railName.textContent = `${idx + 1} of ${STAGES.length}: ${STAGES[idx].name}`;
      };

      // Desktop (pinned) state from the scroll position.
      const apply = () => {
        let idx = -1;
        let fork = false;
        lis.forEach((li, i) => {
          if (li.offsetLeft + li.offsetWidth / 2 <= line) idx = i;
        });
        fork = forkEl.offsetLeft + forkEl.offsetWidth / 2 <= line;
        if (!signed()) idx = Math.min(idx, GATE);
        const sg = signed();
        if (idx === last.idx && fork === last.fork && sg === last.signed) return;
        last = { idx, fork, signed: sg };
        paint(idx, false);
        forkEl.dataset.reached = String(fork);
        if (forkTl) fork ? forkTl.play() : forkTl.progress() > 0 && forkTl.reverse();
      };

      // Carousel state from the active card.
      const applyC = () => {
        paint(cidx, !signed());
        if (cidx >= FORK_AFTER) forkMTl?.play();
        if (cidx >= GATE && sigTl && sigTl.progress() < 1 && !sigTl.isActive()) sigTl.play();
      };

      // Horizontal, pinned (desktop, motion allowed).
      mm.add("(min-width: 1024px) and (prefers-reduced-motion: no-preference)", () => {
        pinEl.dataset.mode = "h";
        refresh = apply;
        const dist = () => Math.max(0, trackEl.scrollWidth - window.innerWidth);
        const scrollLen = () => dist() * 0.4;
        const tween = gsap.to(trackEl, {
          x: () => -dist(),
          ease: "none",
          scrollTrigger: {
            trigger: pinEl,
            pin: true,
            scrub: 0.5,
            start: "top top",
            end: () => "+=" + scrollLen(),
            invalidateOnRefresh: true,
            anticipatePin: 1,
            onUpdate: (self) => {
              line = window.innerWidth * (0.3 + 0.55 * self.progress) + dist() * self.progress;
              apply();
              if (sigTl && !sigTl.isActive() && sigTl.progress() < 1) {
                const gateLi = lis[GATE];
                if (gateLi.offsetLeft + gateLi.offsetWidth / 2 - 280 <= line) sigTl.play();
              }
              if (sigTl && sigTl.progress() > 0 && line < lis[GATE].offsetLeft - 60) sigTl.reverse();
            },
            onRefresh: (self) => {
              line = window.innerWidth * (0.3 + 0.55 * self.progress) + dist() * self.progress;
              apply();
            },
          },
        });
        const st = tween.scrollTrigger!;
        const goTo = (i: number) => {
          const center = lis[i].offsetLeft + lis[i].offsetWidth / 2;
          const vw = window.innerWidth;
          const p = Math.min(1, Math.max(0, (center - 0.3 * vw) / (dist() + 0.55 * vw)));
          window.scrollTo({ top: st.start + p * (st.end - st.start) + 2, behavior: "smooth" });
        };
        const handlers = dots.map((d, i) => {
          const h = () => goTo(i);
          d.addEventListener("click", h);
          return () => d.removeEventListener("click", h);
        });
        return () => {
          handlers.forEach((off) => off());
          delete pinEl.dataset.mode;
          gsap.set(trackEl, { clearProps: "transform" });
        };
      });

      // Snap carousel (phones, tablets, reduced motion). No pin: native touch scrolling.
      mm.add("(max-width: 1023px), (prefers-reduced-motion: reduce)", () => {
        pinEl.dataset.mode = "c";
        refresh = applyC;
        let raf = 0;
        const measure = () => {
          raf = 0;
          const c = trackEl.scrollLeft + trackEl.clientWidth / 2;
          let best = 0;
          let bd = Infinity;
          lis.forEach((li, i) => {
            const d = Math.abs(li.offsetLeft + li.offsetWidth / 2 - c);
            if (d < bd) {
              bd = d;
              best = i;
            }
          });
          if (best !== cidx) {
            cidx = best;
            applyC();
          }
          if (swipe && trackEl.scrollLeft > 24) swipe.style.opacity = "0";
        };
        const onScroll = () => {
          if (!raf) raf = requestAnimationFrame(measure);
        };
        trackEl.addEventListener("scroll", onScroll, { passive: true });
        cidx = 0;
        applyC();
        const go = dots.map((d, i) => {
          const h = () => {
            const li = lis[i];
            trackEl.scrollTo({ left: li.offsetLeft + li.offsetWidth / 2 - trackEl.clientWidth / 2, behavior: reduced ? "auto" : "smooth" });
          };
          d.addEventListener("click", h);
          return () => d.removeEventListener("click", h);
        });
        return () => {
          trackEl.removeEventListener("scroll", onScroll);
          cancelAnimationFrame(raf);
          go.forEach((off) => off());
          delete pinEl.dataset.mode;
          if (swipe) swipe.style.opacity = "";
        };
      });
    }, root);

    return () => ctx.revert();
  }, []);

  return (
    <section id="pipeline" ref={section} className="relative scroll-mt-20 border-y border-sand-200/5 bg-ink-900 motion-safe:lg:scroll-mt-0">
      <div ref={pin} className="relative flex flex-col justify-center py-10 sm:py-14 motion-safe:lg:h-screen motion-safe:lg:overflow-hidden motion-safe:lg:pb-8 motion-safe:lg:pt-24">
        <div className="container">
          <p className="text-sm font-extrabold uppercase tracking-widest text-coral-400">The agent pipeline</p>
          <div className="mt-2 grid gap-3 lg:grid-cols-[1fr_auto] lg:items-end">
            <h2 className="font-display text-3xl font-semibold leading-tight text-sand-100 text-balance sm:text-5xl">
              Eight agents. One signature.
            </h2>
            <p className="max-w-md text-sm leading-relaxed text-sage-300 sm:text-base">
              Follow one payroll line from the sheet to the receipt. LangGraph runs the steps. Code decides every amount.
            </p>
          </div>

          <div className="mt-5 lg:mb-2 lg:mt-6" aria-label="Pipeline progress">
            <div className="relative flex items-center justify-between">
              <div className="absolute inset-x-5 top-1/2 h-0.5 -translate-y-1/2 bg-ink-600 lg:inset-x-3" />
              <div className="absolute inset-x-5 top-1/2 h-0.5 -translate-y-1/2 lg:inset-x-3">
                <div data-railfill className="h-full origin-left bg-coral-500 transition-transform duration-500" style={{ transform: "scaleX(0)" }} />
              </div>
              {STAGES.map((s, i) => (
                <button
                  key={s.name}
                  data-dot
                  type="button"
                  aria-label={`Go to ${s.name}`}
                  className="group relative grid h-11 w-10 place-items-center rounded-full outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60 lg:size-6"
                >
                  <span className="size-3 rounded-full border-2 border-ink-600 bg-ink-900 transition-colors duration-300 group-data-[reached=true]:border-coral-500 group-data-[reached=true]:bg-coral-500" />
                  <span className="pointer-events-none absolute left-1/2 top-7 hidden -translate-x-1/2 whitespace-nowrap text-[11px] font-bold text-sage-500 transition-colors group-hover:text-sand-100 group-data-[reached=true]:text-sand-200 lg:block">
                    {s.short}
                  </span>
                </button>
              ))}
            </div>
            <div className="mt-1 flex items-center justify-between gap-3 lg:hidden">
              <p data-railname aria-live="polite" className="text-xs font-extrabold text-sand-200">1 of 8: Intake</p>
              <p data-swipe className="swipe-hint text-xs font-bold text-sage-300 transition-opacity duration-300">Swipe</p>
            </div>
          </div>
        </div>

        <ol
          ref={track}
          className="relative mt-4 flex snap-x snap-mandatory items-start gap-3 overflow-x-auto overscroll-x-contain px-[calc(50vw-min(42vw,170px))] pb-3 [-webkit-overflow-scrolling:touch] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden motion-safe:lg:mt-12 motion-safe:lg:w-max motion-safe:lg:snap-none motion-safe:lg:items-start motion-safe:lg:gap-12 motion-safe:lg:overflow-visible motion-safe:lg:px-[max(1.25rem,calc((100vw-1200px)/2+1.25rem))] motion-safe:lg:pb-0 will-change-transform"
        >
          {STAGES.map((s, i) => (
            <StageItem key={s.name} s={s} i={i} />
          ))}
        </ol>
      </div>
    </section>
  );
}

function MiniFork() {
  return (
    <div className="mt-3 rounded-2xl border border-sand-200/10 bg-ink-950/50 p-3 motion-safe:lg:hidden">
      <svg viewBox="0 0 240 40" className="h-10 w-full" fill="none" aria-hidden>
        <path d="M0 8 H240" className="stroke-coral-500" strokeWidth="2" />
        <path data-branch-m d="M16 8 C30 8 34 32 56 32 H184 C206 32 210 8 224 8" className="stroke-sage-400" strokeWidth="2" />
      </svg>
      <p data-node-m className="text-center text-xs font-extrabold text-sand-100">Amazon Bedrock AgentCore</p>
      <p className="mt-0.5 text-center text-xs text-sage-300">Same plan, re-computed on AWS. Must match to the cent.</p>
      <p data-match-m className="mx-auto mt-2 flex w-fit items-center gap-1.5 rounded-full bg-mint-500/15 px-2.5 py-1 text-[11px] font-extrabold text-mint-400">
        <Check className="size-3" strokeWidth={4} /> EUR 6,025.70 = EUR 6,025.70
      </p>
    </div>
  );
}

function StageItem({ s, i }: { s: (typeof STAGES)[number]; i: number }) {
  const isGate = i === GATE;
  return (
    <>
      <li data-stage className="relative w-[min(84vw,340px)] shrink-0 snap-center motion-safe:lg:w-[290px]">
        {i > 0 && (
          <span className="conn absolute hidden bg-ink-600 motion-safe:lg:right-full motion-safe:lg:top-[2.2rem] motion-safe:lg:block motion-safe:lg:h-0.5 motion-safe:lg:w-12">
            <i className="block size-full origin-left bg-coral-500" />
          </span>
        )}
        <article
          className={cn(
            "stage-card flex h-full flex-col rounded-3xl border bg-ink-850 p-5",
            isGate ? "border-coral-500/60" : "border-sand-200/10",
          )}
        >
          <div className="flex items-center justify-between gap-3">
            <span className="grid size-10 place-items-center rounded-full bg-ink-700 font-display text-base font-semibold text-sand-100 ring-1 ring-sand-200/15">
              {i + 1}
            </span>
            <span className="rounded-full border border-sand-200/10 px-2.5 py-1 text-[11px] font-extrabold uppercase tracking-wider text-sage-300">
              {s.partner}
            </span>
          </div>
          <h3 className="mt-4 flex items-center gap-2 font-display text-2xl font-semibold text-sand-100">
            {s.name}
            {isGate && <Lock className="size-4 text-coral-400" />}
          </h3>
          <p className="mt-2 text-sm leading-relaxed text-sage-300">{s.body}</p>
          {isGate && <Gate />}
          {i === FORK_AFTER && <MiniFork />}
          <div className="stage-detail mt-auto pt-4">
            <p className="text-[10px] font-extrabold uppercase tracking-widest text-sage-500">Example</p>
            <p className="mt-1 rounded-xl bg-ink-950/60 px-3 py-2 text-sm font-semibold leading-snug text-sand-200">{s.example}</p>
          </div>
        </article>
      </li>
      {i === FORK_AFTER && <Fork />}
    </>
  );
}
