"use client";

// Lives in the dashboard layout. Small on purpose: it fetches the member's tour state, decides when
// to offer the tour (first session only, never again after finish/skip), renders the ? help menu,
// the "Take the tour" pill and the one-time page nudges, and loads the heavy engine on demand.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { usePathname, useRouter } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import { Check, CircleHelp, ListChecks, Play, RotateCcw, X } from "lucide-react";

import { Mascot } from "@/components/tour/mascot";
import type { TourExit, TourRequest } from "@/components/tour/tour-engine";
import { Button } from "@/components/ui/button";
import { apiTour, type TourState } from "@/lib/api";
import { CHAPTERS, MINI_TOURS, stepsForChapter, type TourRole } from "@/lib/tour/steps";
import { cn } from "@/lib/utils";

const TourEngine = dynamic(() => import("@/components/tour/tour-engine"), { ssr: false });

interface TourApi {
  state: TourState | null;
  active: boolean;
  start: (request: TourRequest) => void;
}

const TourContext = createContext<TourApi>({ state: null, active: false, start: () => undefined });
export const useTour = () => useContext(TourContext);

const TOUR_ROLES: TourRole[] = ["owner", "admin", "approver", "viewer"];

export function TourProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname() ?? "";
  const [state, setState] = useState<TourState | null>(null);
  const [request, setRequest] = useState<TourRequest | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [nudge, setNudge] = useState<string | null>(null);
  const autoStarted = useRef(false);
  const stateRef = useRef<TourState | null>(null);
  stateRef.current = state;

  useEffect(() => {
    let alive = true;
    apiTour.state().then((s) => alive && setState(s)).catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  const start = useCallback((req: TourRequest) => {
    setNudge(null);
    setToast(null);
    setRequest(req);
  }, []);

  // First session in this org: offer the tour once. Demo accounts and finished/skipped members never auto-start.
  useEffect(() => {
    if (!state || autoStarted.current || request) return;
    if (state.auto_start && pathname.startsWith("/dashboard")) {
      autoStarted.current = true;
      const t = window.setTimeout(() => start({ kind: "full" }), 700);
      return () => window.clearTimeout(t);
    }
  }, [state, pathname, request, start]);

  // One-time page nudges (Funding, Workflows) once the member is past the main tour.
  useEffect(() => {
    setNudge(null);
    if (!state || request || state.status === "NOT_STARTED" || state.status === "IN_PROGRESS") return;
    const mini = MINI_TOURS.find((m) => pathname === m.path);
    if (!mini || state.completed_chapters.includes(`page:${mini.id}`)) return;
    const t = window.setTimeout(() => setNudge(mini.id), 1400);
    return () => window.clearTimeout(t);
  }, [pathname, state, request]);

  const onExit = useCallback((exit: TourExit) => {
    setRequest(null);
    if (exit.state) setState(exit.state);
    if (exit.reason === "skipped") {
      setToast("Tour skipped. You can replay me anytime from the ? button or your profile menu.");
      window.setTimeout(() => setToast(null), 6000);
    }
    if (exit.goto) router.push(exit.goto);
  }, [router]);

  const dismissNudge = useCallback(async (id: string) => {
    setNudge(null);
    const cur = stateRef.current;
    if (!cur) return;
    try {
      setState(await apiTour.save({ completed_chapters: [...new Set([...cur.completed_chapters, `page:${id}`])] }));
    } catch {
      /* best effort */
    }
  }, []);

  const api = useMemo<TourApi>(() => ({ state, active: request !== null, start }), [state, request, start]);

  return (
    <TourContext.Provider value={api}>
      {children}
      {state && !request && <TourHelp state={state} onStart={start} />}
      <AnimatePresence>
        {nudge && !request && (
          <Nudge
            key={nudge}
            label={MINI_TOURS.find((m) => m.id === nudge)?.label ?? ""}
            count={MINI_TOURS.find((m) => m.id === nudge)?.steps.length ?? 0}
            onShow={() => start({ kind: "mini", id: nudge })}
            onLater={() => void dismissNudge(nudge)}
          />
        )}
        {toast && <ToastPill key="toast" message={toast} onClose={() => setToast(null)} />}
      </AnimatePresence>
      {request && state && (
        <TourEngine
          key={`${request.kind}-${"chapter" in request ? request.chapter : "id" in request ? request.id : ""}`}
          request={request}
          state={state}
          onExit={onExit}
        />
      )}
    </TourContext.Provider>
  );
}

// ------------------------------------------------------------------ small pieces
function ToastPill({ message, onClose }: { message: string; onClose: () => void }) {
  return (
    <motion.div
      role="status"
      className="glass fixed bottom-20 left-4 right-4 z-[60] flex items-center gap-3 rounded-full border-2 border-sand-200/15 py-2 pl-2 pr-3 shadow-2xl sm:bottom-5 sm:left-auto sm:right-[10.5rem] sm:max-w-md"
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 12 }}
    >
      <Mascot pose="idle" size={40} />
      <p className="flex-1 text-sm font-bold leading-snug text-sand-200">{message}</p>
      <button type="button" onClick={onClose} aria-label="Dismiss" className="rounded-full p-1.5 text-sage-400 hover:bg-ink-700 hover:text-sand-100">
        <X className="size-4" />
      </button>
    </motion.div>
  );
}

function Nudge({ label, count, onShow, onLater }: { label: string; count: number; onShow: () => void; onLater: () => void }) {
  return (
    <motion.div
      role="dialog"
      aria-label={`Quick look at ${label}`}
      className="glass fixed bottom-20 left-4 right-4 z-[60] rounded-4xl border-2 border-sand-200/15 p-4 shadow-2xl sm:bottom-5 sm:left-auto sm:right-[10.5rem] sm:w-[340px] lg:left-auto"
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 14 }}
      transition={{ type: "spring", stiffness: 260, damping: 24 }}
    >
      <div className="flex items-center gap-3">
        <Mascot pose="wave" size={52} />
        <div className="min-w-0">
          <p className="font-display text-base font-semibold leading-tight text-sand-100">First time on {label}?</p>
          <p className="text-sm font-semibold text-sage-300">A {count}-step look takes about 20 seconds.</p>
        </div>
      </div>
      <div className="mt-3 flex items-center justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onLater}>Not now</Button>
        <Button size="sm" onClick={onShow}><Play /> Show me</Button>
      </div>
    </motion.div>
  );
}

function TourHelp({ state, onStart }: { state: TourState; onStart: (r: TourRequest) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const role = (TOUR_ROLES.includes(state.role as TourRole) ? state.role : "viewer") as TourRole;

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const chapters = CHAPTERS.filter((c) => stepsForChapter(role, c.id).length > 0);
  const offer = (state.status === "SKIPPED" || state.outdated) && (state.is_demo || state.outdated);
  const resumable = state.status === "IN_PROGRESS" && !!state.last_step;
  const begin = (r: TourRequest) => {
    setOpen(false);
    onStart(r);
  };

  return (
    <div ref={ref} className="fixed right-[4.25rem] top-4 z-50 flex items-center gap-2 lg:right-[5.75rem]">
      {offer && (
        <button
          type="button"
          onClick={() => begin({ kind: "full" })}
          className="glass hidden items-center gap-2 rounded-full border border-sand-200/15 py-1.5 pl-1.5 pr-3.5 text-sm font-bold text-sand-100 transition-colors hover:border-coral-400/60 sm:inline-flex"
        >
          <Mascot pose="idle" size={28} className="!shadow-none" />
          {state.outdated ? "What's new: take the tour" : "Take the tour"}
        </button>
      )}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label="Help and product tour"
        title="Help and product tour"
        className="glass grid size-11 place-items-center rounded-full border border-sand-200/15 text-sage-300 transition-colors hover:text-sand-100 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60"
      >
        <CircleHelp className="size-5" />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            role="menu"
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            className="glass absolute right-0 top-full mt-2 w-[min(20rem,calc(100vw-2rem))] rounded-4xl border border-sand-200/10 p-3 shadow-2xl"
          >
            <div className="flex items-center gap-3 rounded-3xl bg-ink-950/40 p-3">
              <Mascot pose="wave" size={44} />
              <p className="text-sm font-bold leading-snug text-sand-200">Need a hand? Pick a part of the tour to replay.</p>
            </div>
            {resumable && (
              <button role="menuitem" type="button" onClick={() => begin({ kind: "resume" })}
                className="mt-2 flex w-full items-center gap-3 rounded-3xl px-4 py-2.5 text-left font-bold text-sand-100 hover:bg-sand-200/10">
                <Play className="size-5 text-coral-400" /> Resume where I left off
              </button>
            )}
            <button role="menuitem" type="button" onClick={() => begin({ kind: "full" })}
              className="mt-1 flex w-full items-center gap-3 rounded-3xl px-4 py-2.5 text-left font-bold text-sand-100 hover:bg-sand-200/10">
              <RotateCcw className="size-5 text-sage-400" /> Replay tour
            </button>
            <p className="px-4 pb-1 pt-2 text-[11px] font-extrabold uppercase tracking-widest text-sage-500">Chapters</p>
            <ul>
              {chapters.map((c) => (
                <li key={c.id}>
                  <button role="menuitem" type="button" onClick={() => begin({ kind: "chapter", chapter: c.id })}
                    className="flex w-full items-center gap-3 rounded-3xl px-4 py-2 text-left hover:bg-sand-200/10">
                    <span className={cn("grid size-5 shrink-0 place-items-center rounded-full border-2",
                      state.completed_chapters.includes(c.id) ? "border-mint-500 bg-mint-500 text-ink-950" : "border-ink-600")}>
                      {state.completed_chapters.includes(c.id) && <Check className="size-3" strokeWidth={3} />}
                    </span>
                    <span className="min-w-0">
                      <span className="block text-sm font-bold text-sand-100">{c.label}</span>
                      <span className="block truncate text-xs font-semibold text-sage-500">{c.blurb}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {state.checklist_dismissed && (state.role === "owner" || state.role === "admin") && (
              <button role="menuitem" type="button"
                onClick={async () => { await apiTour.save({ checklist_dismissed: false }).catch(() => null); setOpen(false); window.location.assign("/dashboard"); }}
                className="mt-1 flex w-full items-center gap-3 rounded-3xl px-4 py-2.5 text-left font-bold text-sand-100 hover:bg-sand-200/10">
                <ListChecks className="size-5 text-sage-400" /> Show the Get started list
              </button>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
