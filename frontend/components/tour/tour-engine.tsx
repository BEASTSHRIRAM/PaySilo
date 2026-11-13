"use client";

// The product tour engine. Loaded on demand (dynamic import from the provider) so driver.js and the
// tour UI never weigh on a normal page view. driver.js owns the spotlight, positioning and scrolling;
// the bubble itself, the welcome and finish screens, the keyboard and the persistence are ours.

import "driver.js/dist/driver.css";
import "./tour.css";

import { driver, type Driver } from "driver.js";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal, flushSync } from "react-dom";
import { usePathname, useRouter } from "next/navigation";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowLeft, ArrowRight, Check } from "lucide-react";

import { Mascot } from "@/components/tour/mascot";
import { Button } from "@/components/ui/button";
import { api, apiTour, type TourState } from "@/lib/api";
import { findTarget, isMobileViewport, nextFrames, waitForElement } from "@/lib/tour/dom";
import {
  CHAPTERS, MINI_TOURS, fill, resolveStep, stepsForChapter, stepsForRole,
  type ChapterId, type ResolvedStep, type TourContext, type TourRole,
} from "@/lib/tour/steps";
import { cn } from "@/lib/utils";

export type TourRequest =
  | { kind: "full" }
  | { kind: "resume" }
  | { kind: "chapter"; chapter: ChapterId }
  | { kind: "mini"; id: string };

export type TourExit = { reason: "completed" | "skipped" | "paused"; state: TourState | null; goto?: string };

interface Props {
  request: TourRequest;
  state: TourState;
  onExit: (exit: TourExit) => void;
}

type Phase = "boot" | "welcome" | "steps" | "finish";

function focusables(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>("button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])"));
}

function trapTab(e: KeyboardEvent, root: HTMLElement | null) {
  const list = focusables(root);
  if (list.length === 0) return;
  const first = list[0];
  const last = list[list.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (!active || !root?.contains(active)) {
    e.preventDefault();
    first.focus({ preventScroll: true });
  } else if (e.shiftKey && active === first) {
    e.preventDefault();
    last.focus({ preventScroll: true });
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus({ preventScroll: true });
  }
}

// ------------------------------------------------------------------ screens
function Backdrop({ children, label }: { children: React.ReactNode; label: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const t = window.setTimeout(() => ref.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus({ preventScroll: true }), 80);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab") trapTab(e, ref.current);
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      window.clearTimeout(t);
      document.removeEventListener("keydown", onKey, true);
      prev?.focus?.({ preventScroll: true });
    };
  }, []);
  return (
    <motion.div
      ref={ref}
      role="dialog"
      aria-modal="true"
      aria-label={label}
      className="fixed inset-0 z-[2000] grid place-items-center overflow-y-auto bg-ink-950/85 p-4 backdrop-blur-md"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.25 }}
    >
      {children}
    </motion.div>
  );
}

function WelcomeScreen({ state, onStart, onSkip }: { state: TourState; onStart: () => void; onSkip: () => void }) {
  const reduced = useReducedMotion();
  const vars = { org: state.organization_name, first: state.first_name || "there" };
  return (
    <Backdrop label="Welcome to PaySilo">
      <motion.div
        className="glass relative my-auto w-full max-w-lg rounded-[2.5rem] border-2 border-sand-200/15 px-6 pb-7 pt-24 text-center shadow-2xl sm:px-10"
        initial={reduced ? false : { opacity: 0, y: 28, scale: 0.96 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 220, damping: 22 }}
      >
        <div className="absolute left-1/2 top-0 -ml-[66px] -mt-[66px]"><Mascot pose="wave" size={132} /></div>
        <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Hi {vars.first}, I&apos;m Silo.</h1>
        <p className="mx-auto mt-3 max-w-sm text-lg font-semibold leading-snug text-sage-300">
          Give me 60 seconds and I&apos;ll show you how <span className="text-sand-100">{vars.org}</span> pays people
          without ever mixing two clients&apos; money.
        </p>
        <div className="mt-7 flex flex-col items-stretch gap-3 sm:flex-row sm:justify-center">
          <Button size="lg" onClick={onStart} data-autofocus>
            Take the tour <ArrowRight />
          </Button>
          <Button size="lg" variant="ghost" onClick={onSkip}>Skip for now</Button>
        </div>
        <p className="mt-4 text-xs font-semibold text-sage-500">You can replay me anytime from the ? button.</p>
      </motion.div>
    </Backdrop>
  );
}

function FinishScreen({ state, onDone }: { state: TourState; onDone: (goto?: string) => void }) {
  const reduced = useReducedMotion();
  const first = state.first_name || "there";
  const setup = state.role === "owner" || state.role === "admin";
  return (
    <Backdrop label="Tour finished">
      <motion.div
        className="glass relative my-auto w-full max-w-lg rounded-[2.5rem] border-2 border-sand-200/15 px-6 pb-7 pt-24 text-center shadow-2xl sm:px-10"
        initial={reduced ? false : { opacity: 0, y: 28, scale: 0.96 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 220, damping: 22 }}
      >
        <div className="absolute left-1/2 top-0 -ml-[66px] -mt-[66px]"><Mascot pose="cheer" size={132} /></div>
        <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">You&apos;re all set, {first}.</h1>
        <p className="mx-auto mt-3 max-w-sm text-lg font-semibold leading-snug text-sage-300">
          {setup
            ? "I put a Get started list on your dashboard. Tick it off at your own pace and you'll be running payroll."
            : "That's the tour. I'm in the corner whenever you want to ask something, and the ? button replays any chapter."}
        </p>
        <div className="mt-7 flex flex-col items-stretch gap-3 sm:flex-row sm:justify-center">
          <Button size="lg" variant="success" onClick={() => onDone("/dashboard")} data-autofocus>
            {setup ? "Show my checklist" : "Go to the dashboard"} <ArrowRight />
          </Button>
        </div>
      </motion.div>
    </Backdrop>
  );
}

function TravelVeil() {
  const reduced = useReducedMotion();
  return (
    <motion.div
      className="fixed inset-0 z-[1500] grid place-items-center bg-ink-950/70 backdrop-blur-[2px]"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.15 }}
      role="status"
      aria-label="Moving to the next page"
    >
      <div className="glass flex items-center gap-3 rounded-full border-2 border-sand-200/15 py-2 pl-2 pr-5 shadow-2xl">
        <Mascot pose="wave" size={44} hopKey="travel" />
        <span className="font-bold text-sand-200">{reduced ? "One moment" : "Follow me"}</span>
      </div>
    </motion.div>
  );
}

// ------------------------------------------------------------------ the bubble
interface CardProps {
  rs: ResolvedStep;
  title: string;
  body: string;
  why: string;
  index: number;
  total: number;
  tilt: number;
  isLast: boolean;
  skipLabel: string;
  onNext: () => void;
  onPrev: () => void;
  onSkip: () => void;
  onAction: () => void;
}

function TourCard({ rs, title, body, why, index, total, tilt, isLast, skipLabel, onNext, onPrev, onSkip, onAction }: CardProps) {
  const reduced = useReducedMotion();
  const titleId = "tour-title";
  return (
    <motion.div
      key={rs.step.id}
      className="relative px-5 pb-4 pt-5 sm:px-6"
      initial={reduced ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: "easeOut" }}
    >
      <Mascot
        pose={rs.step.pose}
        size={72}
        tilt={tilt}
        hopKey={rs.step.id}
        className="absolute -left-0.5 -top-11 z-10 sm:-left-4 sm:-top-12"
      />
      <div className="min-h-[40px] pl-[68px] sm:pl-[72px]">
        <p className="text-[11px] font-extrabold uppercase tracking-widest text-coral-300">
          Step {index + 1} of {total}
        </p>
        <h2 id={titleId} className="mt-0.5 font-display text-xl font-semibold leading-tight text-sand-100">{title}</h2>
      </div>
      <p className="mt-3 text-[15px] font-semibold leading-snug text-sand-200">{body}</p>
      <p className="mt-2.5 rounded-2xl bg-ink-950/50 px-3.5 py-2 text-sm font-bold leading-snug text-mint-400">
        <span className="mr-1.5 text-[10px] font-extrabold uppercase tracking-widest text-sage-500">Why it matters</span>
        {why}
      </p>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-x-3 gap-y-3">
        <div className="flex items-center gap-1" role="presentation" aria-hidden="true">
          {Array.from({ length: total }).map((_, i) => (
            <span
              key={i}
              className={cn(
                "h-1.5 rounded-full transition-all duration-300",
                i === index ? "w-4 bg-coral-500" : i < index ? "w-1.5 bg-sage-500" : "w-1.5 bg-ink-600",
              )}
            />
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {index > 0 && (
            <Button size="sm" variant="outline" onClick={onPrev} aria-label="Back">
              <ArrowLeft /> <span className="hidden min-[400px]:inline">Back</span>
            </Button>
          )}
          {rs.action ? (
            <Button size="sm" onClick={onAction} data-tour-primary>
              {rs.action.label} <ArrowRight />
            </Button>
          ) : (
            <Button size="sm" variant={isLast ? "success" : "primary"} onClick={onNext} data-tour-primary>
              {isLast ? <>Finish <Check /></> : <>Next <ArrowRight /></>}
            </Button>
          )}
        </div>
      </div>
      <div className="mt-2 flex items-center justify-between gap-2">
        <button type="button" onClick={onSkip} className="rounded-lg px-1 py-1 text-xs font-bold text-sage-400 underline-offset-4 hover:text-sand-100 hover:underline focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60">
          {skipLabel}
        </button>
        {rs.action && (
          <button type="button" onClick={onNext} className="rounded-lg px-1 py-1 text-xs font-bold text-sage-400 underline-offset-4 hover:text-sand-100 hover:underline focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60">
            Show me the rest
          </button>
        )}
        <span className="hidden text-[11px] font-semibold text-sage-600 sm:inline">Use ← → keys</span>
      </div>
    </motion.div>
  );
}

// ------------------------------------------------------------------ engine
export default function TourEngine({ request, state, onExit }: Props) {
  const router = useRouter();
  const pathname = usePathname();
  const reduced = useReducedMotion();
  const pathRef = useRef(pathname);
  pathRef.current = pathname;

  const [phase, setPhase] = useState<Phase>("boot");
  const [steps, setSteps] = useState<ResolvedStep[]>([]);
  const [index, setIndex] = useState(0);
  const [travelling, setTravelling] = useState(false);
  const [cardHost, setCardHost] = useState<HTMLElement | null>(null);
  const [shown, setShown] = useState<{ rs: ResolvedStep; tilt: number } | null>(null);
  const [announce, setAnnounce] = useState("");

  const driverRef = useRef<Driver | null>(null);
  const finishedRef = useRef(false);
  const stateRef = useRef(state);
  stateRef.current = state;

  const isMini = request.kind === "mini";
  const firstRun = request.kind === "full" && state.status === "NOT_STARTED";
  const vars = useMemo(() => ({ org: state.organization_name, first: state.first_name || "there" }), [state]);

  // ---- body flags: hide the Ask Silo pill while the tour runs, and tell CSS which step is showing.
  useEffect(() => {
    document.body.setAttribute("data-tour-active", "1");
    return () => {
      document.body.removeAttribute("data-tour-active");
      document.body.removeAttribute("data-tour-step");
    };
  }, []);

  // ---- persistence helpers (never block the UI; the tour must work even if the save fails)
  const save = useCallback(async (patch: Parameters<typeof apiTour.save>[0]) => {
    try {
      return await apiTour.save(patch);
    } catch {
      return null;
    }
  }, []);

  const teardown = useCallback(() => {
    driverRef.current?.destroy();
    driverRef.current = null;
    setCardHost(null);
    setShown(null);
    setTravelling(false);
  }, []);

  const exit = useCallback(async (reason: TourExit["reason"], goto?: string) => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    teardown();
    const before = stateRef.current;
    const wasFinished = before.status === "COMPLETED" || before.status === "SKIPPED";
    const donePages = before.completed_chapters;
    let next: TourState | null = null;
    if (request.kind === "mini") {
      next = await save({ completed_chapters: [...new Set([...donePages, `page:${request.id}`])] });
    } else if (reason === "completed") {
      const chaptersDone = request.kind === "chapter"
        ? [request.chapter]
        : [...new Set(steps.map((s) => s.step.chapter))];
      const merged = [...new Set([...donePages, ...chaptersDone])];
      next = await save(request.kind === "chapter" && before.status !== "NOT_STARTED"
        ? { completed_chapters: merged }
        : { status: "COMPLETED", completed_chapters: merged });
    } else if (reason === "skipped") {
      next = await save(wasFinished ? {} : { status: "SKIPPED" });
    } else {
      next = await save({});
    }
    onExit({ reason, state: next, goto });
  }, [onExit, request, save, steps, teardown]);

  // ---- boot: look at the org once, then pick the step list
  useEffect(() => {
    let alive = true;
    (async () => {
      let employers: { id: string }[] = [];
      try {
        employers = (await api.overview()).employers;
      } catch {
        /* no data is fine: the tour then uses its empty-state copy */
      }
      if (!alive) return;
      const ctx: TourContext = {
        role: (["owner", "admin", "approver", "viewer"].includes(state.role) ? state.role : "viewer") as TourRole,
        employerCount: employers.length,
        firstEmployerId: employers[0]?.id ?? null,
      };
      let list;
      if (request.kind === "mini") {
        list = MINI_TOURS.find((m) => m.id === request.id)?.steps ?? [];
      } else if (request.kind === "chapter") {
        list = stepsForChapter(ctx.role, request.chapter);
      } else {
        list = stepsForRole(ctx.role);
      }
      const resolved = list.map((s) => resolveStep(s, ctx));
      if (resolved.length === 0) {
        void exit("paused");
        return;
      }
      setSteps(resolved);
      let start = 0;
      if (request.kind === "resume" && state.last_step) {
        const at = resolved.findIndex((r) => r.step.id === state.last_step);
        if (at >= 0) start = at;
      }
      setIndex(start);
      setPhase(firstRun ? "welcome" : "steps");
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- present the current step
  useEffect(() => {
    if (phase !== "steps" || steps.length === 0) return;
    const rs = steps[index];
    if (!rs) return;
    const ac = new AbortController();
    const timers: number[] = [];

    (async () => {
      document.body.setAttribute("data-tour-step", rs.step.id);
      if (!isMini && rs.step.id !== stateRef.current.last_step && stateRef.current.status !== "COMPLETED") {
        void save({ status: "IN_PROGRESS", last_step: rs.step.id });
      }

      // 1. get to the right page
      if (rs.route && rs.route !== pathRef.current) {
        driverRef.current?.destroy();
        driverRef.current = null;
        setCardHost(null);
        setShown(null);
        setTravelling(true);
        router.push(rs.route);
        const until = performance.now() + 7000;
        while (!ac.signal.aborted && window.location.pathname !== rs.route && performance.now() < until) {
          await new Promise((r) => window.setTimeout(r, 60));
        }
        if (ac.signal.aborted) return;
      }

      // 2. wait for the spotlight target; a missing one means a centered bubble, never a broken tour
      const fallbacks = rs.step.fallback ?? [];
      let el: HTMLElement | null = rs.target || fallbacks.length ? await waitForElement(rs.target, fallbacks, 6000, ac.signal) : null;
      if (ac.signal.aborted) return;
      setTravelling(false);

      // 3. bring it into view before measuring (the sheet on phones needs it clear of the bubble)
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "auto" });
        await nextFrames(2);
        if (ac.signal.aborted) return;
        if (!document.body.contains(el)) el = findTarget(rs.target, fallbacks);
      }

      const title = fill(rs.title, vars);
      const body = fill(rs.body, vars);
      const why = fill(rs.why, vars);
      const mobile = isMobileViewport();

      let tilt = 0;
      if (el && !mobile) {
        const r = el.getBoundingClientRect();
        tilt = Math.max(-9, Math.min(9, ((r.left + r.width / 2) - window.innerWidth / 2) / 60));
      }

      flushSync(() => {
        setShown({ rs: { ...rs, title, body, why }, tilt });
        setAnnounce(`Step ${index + 1} of ${steps.length}. ${title}. ${body} ${why}`);
      });

      let drv = driverRef.current;
      if (!drv) {
        drv = driver({
          animate: !reduced,
          smoothScroll: false,
          allowClose: false,
          allowScroll: false,
          allowKeyboardControl: false,
          overlayClickBehavior: "none",
          overlayColor: "#0C1D23",
          overlayOpacity: 0.74,
          stagePadding: 10,
          stageRadius: 22,
          popoverOffset: 46,
          showButtons: [],
          onPopoverRender: (popover) => {
            let host = popover.wrapper.querySelector<HTMLElement>(":scope > [data-tour-card]");
            if (!host) {
              host = document.createElement("div");
              host.setAttribute("data-tour-card", "");
              popover.wrapper.appendChild(host);
            }
            popover.wrapper.setAttribute("role", "dialog");
            popover.wrapper.setAttribute("aria-modal", "true");
            popover.wrapper.setAttribute("aria-labelledby", "tour-title");
            const h = host;
            flushSync(() => setCardHost(h));
          },
        });
        driverRef.current = drv;
      }
      drv.highlight({
        element: el ?? undefined,
        popover: { side: rs.step.side ?? "bottom", align: "center", popoverClass: "paysilo-tour" },
      });

      // 4. phones: put the sheet on the side with room, and keep the target clear of it
      const place = () => {
        const wrapper = document.querySelector<HTMLElement>(".driver-popover.paysilo-tour");
        if (!wrapper) return;
        if (mobile) {
          const sheetH = wrapper.getBoundingClientRect().height + 20;
          const live = (el && document.body.contains(el)) ? el : null;
          let top = false;
          if (live) {
            const r = live.getBoundingClientRect();
            const fitsBelow = r.bottom <= window.innerHeight - sheetH - 8 && r.top >= 8;
            const fitsAbove = r.top >= sheetH + 8 && r.bottom <= window.innerHeight - 8;
            top = !fitsBelow && fitsAbove;
            if (!fitsBelow && !fitsAbove) {
              // Tall or off-screen target: scroll it into the free band above the bottom sheet.
              const free = window.innerHeight - sheetH - 16;
              const delta = r.height > free ? r.top - 12 : r.bottom - free;
              window.scrollBy({ top: delta, behavior: "auto" });
            }
          }
          wrapper.classList.toggle("tour-sheet-top", top);
        } else {
          wrapper.classList.remove("tour-sheet-top");
        }
        drv?.refresh();
        const primary = wrapper.querySelector<HTMLElement>("[data-tour-primary]");
        if (primary) primary.focus({ preventScroll: true });
      };
      await nextFrames(2);
      if (ac.signal.aborted) return;
      place();
      // Page entrance animations (staggered cards) can still be moving: re-measure twice.
      timers.push(window.setTimeout(place, 420), window.setTimeout(() => drv?.refresh(), 900));
    })();

    return () => {
      ac.abort();
      timers.forEach((t) => window.clearTimeout(t));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, index, steps]);

  // ---- navigation actions
  const total = steps.length;
  const goNext = useCallback(() => {
    if (index >= total - 1) {
      if (isMini || request.kind === "chapter") {
        void exit("completed");
      } else {
        teardown();
        document.body.removeAttribute("data-tour-step");
        setPhase("finish");
      }
    } else {
      setIndex((i) => i + 1);
    }
  }, [exit, index, isMini, request.kind, teardown, total]);
  const goPrev = useCallback(() => setIndex((i) => Math.max(0, i - 1)), []);
  const skip = useCallback(() => void exit("skipped"), [exit]);
  const doAction = useCallback(async () => {
    const rs = steps[index];
    if (!rs?.action) return;
    // Pause here: resuming the tour later continues right after this step.
    const nextId = steps[index + 1]?.step.id;
    await save({ status: "IN_PROGRESS", last_step: nextId ?? rs.step.id });
    void exit("paused", rs.action.href);
  }, [exit, index, save, steps]);

  // ---- keyboard: arrows, Esc and a focus trap for the bubble
  useEffect(() => {
    if (phase !== "steps") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowRight") { e.preventDefault(); e.stopPropagation(); goNext(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); e.stopPropagation(); goPrev(); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); skip(); }
      else if (e.key === "Tab") trapTab(e, document.querySelector<HTMLElement>(".driver-popover.paysilo-tour"));
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [phase, goNext, goPrev, skip]);

  useEffect(() => () => {
    driverRef.current?.destroy();
    driverRef.current = null;
  }, []);

  return (
    <>
      <div className="tour-live" role="status" aria-live="polite" aria-atomic="true">{announce}</div>
      <AnimatePresence>
        {phase === "welcome" && (
          <WelcomeScreen
            key="welcome"
            state={state}
            onStart={() => {
              void save({ status: "IN_PROGRESS" });
              setPhase("steps");
            }}
            onSkip={() => void exit("skipped")}
          />
        )}
        {phase === "finish" && (
          <FinishScreen key="finish" state={state} onDone={(goto) => void exit("completed", goto)} />
        )}
        {travelling && phase === "steps" && <TravelVeil key="veil" />}
      </AnimatePresence>
      {cardHost && shown && phase === "steps" &&
        createPortal(
          <TourCard
            rs={shown.rs}
            title={shown.rs.title}
            body={shown.rs.body}
            why={shown.rs.why}
            index={index}
            total={total}
            tilt={shown.tilt}
            isLast={index === total - 1}
            skipLabel={isMini ? "Close" : "Skip tour"}
            onNext={goNext}
            onPrev={goPrev}
            onSkip={skip}
            onAction={() => void doAction()}
          />,
          cardHost,
        )}
    </>
  );
}

export { CHAPTERS };
