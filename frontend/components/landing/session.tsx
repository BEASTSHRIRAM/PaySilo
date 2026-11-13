"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

type State = { status: "unknown" } | { status: "out" } | { status: "in"; initials: string };

let cached: State = { status: "unknown" };
let inflight: Promise<State> | null = null;

function initialsOf(name?: string, email?: string) {
  const src = (name || email || "").trim();
  const parts = src.split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "U";
}

function load(): Promise<State> {
  inflight ??= api
    .me()
    .then((s): State => ({ status: "in", initials: initialsOf(s.user?.full_name, s.user?.email) }))
    .catch((): State => ({ status: "out" }))
    .then((s) => (cached = s));
  return inflight;
}

/** Session state for the landing page. 401 or a network error counts as logged out. */
export function useLandingSession(): State {
  const [state, setState] = useState<State>({ status: "unknown" });
  useEffect(() => {
    let alive = true;
    load().then((s) => alive && setState(s));
    return () => {
      alive = false;
    };
  }, []);
  return state;
}

function Avatar({ initials }: { initials: string }) {
  return (
    <span aria-hidden className="grid size-7 place-items-center rounded-full bg-sand-200 text-[11px] font-extrabold text-ink-800">
      {initials}
    </span>
  );
}

/** Navbar: Log in + Get started, or Go to dashboard. Same footprint while the session is unknown. */
export function NavActions() {
  const s = useLandingSession();
  if (s.status === "unknown") return <span aria-hidden className="hidden h-10 w-[214px] sm:block" />;
  if (s.status === "in")
    return (
      <Link
        href="/dashboard"
        className="hidden h-10 items-center gap-2 rounded-full bg-coral-500 pl-1.5 pr-5 text-sm font-extrabold text-sand-50 shadow-[0_3px_0_0_theme(colors.coral.700)] transition-[transform,box-shadow] hover:brightness-110 active:translate-y-[3px] active:shadow-none sm:inline-flex"
      >
        <Avatar initials={s.initials} />
        Go to dashboard
      </Link>
    );
  return (
    <>
      <Link
        href="/login"
        className="hidden h-10 items-center rounded-full border-2 border-sand-200/25 px-5 text-sm font-bold text-sand-100 transition-colors hover:border-sand-200/60 hover:bg-sand-200/5 sm:inline-flex"
      >
        Log in
      </Link>
      <Link
        href="/signup"
        className="hidden h-10 items-center rounded-full bg-coral-500 px-5 text-sm font-extrabold text-sand-50 shadow-[0_3px_0_0_theme(colors.coral.700)] transition-[transform,box-shadow] hover:brightness-110 active:translate-y-[3px] active:shadow-none sm:inline-flex"
      >
        Get started
      </Link>
    </>
  );
}

/** Hamburger menu footer buttons (mobile). */
export function MenuActions({ onNavigate }: { onNavigate: () => void }) {
  const s = useLandingSession();
  const base = "flex h-11 items-center justify-center rounded-full text-sm font-extrabold";
  if (s.status === "unknown") return <div aria-hidden className="mt-3 h-11 sm:hidden" />;
  if (s.status === "in")
    return (
      <div className="mt-3 sm:hidden">
        <Link href="/dashboard" onClick={onNavigate} className={cn(base, "gap-2 bg-coral-500 text-sand-50")}>
          <Avatar initials={s.initials} /> Go to dashboard
        </Link>
      </div>
    );
  return (
    <div className="mt-3 grid grid-cols-2 gap-2 sm:hidden">
      <Link href="/login" onClick={onNavigate} className={cn(base, "border-2 border-sand-200/25 font-bold text-sand-100")}>
        Log in
      </Link>
      <Link href="/signup" onClick={onNavigate} className={cn(base, "bg-coral-500 text-sand-50")}>
        Get started
      </Link>
    </div>
  );
}

/** Hero buttons + hint line. */
export function HeroActions() {
  const s = useLandingSession();
  return (
    <>
      <div className="rise mt-9 flex min-h-14 flex-wrap items-center gap-4 [animation-delay:240ms]">
        {s.status === "unknown" ? (
          <span aria-hidden className="h-14 w-[210px]" />
        ) : (
          <Button asChild size="lg">
            <Link href={s.status === "in" ? "/dashboard" : "/signup"}>
              {s.status === "in" ? "Go to dashboard" : "Start free"} <ArrowRight />
            </Link>
          </Button>
        )}
        <Button asChild size="lg" variant="outline">
          <a href="#pipeline">See it run</a>
        </Button>
      </div>
      <p className="rise mt-6 min-h-5 text-sm font-semibold text-sage-500 [animation-delay:320ms]">
        {s.status === "out" && (
          <>
            No card needed.{" "}
            <Link href="/login?demo=1" className="font-bold text-sand-200 underline decoration-coral-500 decoration-2 underline-offset-4 hover:text-sand-50">
              Open the demo
            </Link>{" "}
            on the Airwallex simulator.
          </>
        )}
      </p>
    </>
  );
}

/** Final CTA buttons. */
export function CtaActions() {
  const s = useLandingSession();
  return (
    <div className="relative mt-8 flex min-h-14 flex-wrap justify-center gap-4">
      {s.status === "in" ? (
        <Button asChild size="lg" variant="sand">
          <Link href="/dashboard">
            Go to dashboard <ArrowRight />
          </Link>
        </Button>
      ) : s.status === "out" ? (
        <>
          <Button asChild size="lg" variant="sand">
            <Link href="/signup">
              Start free <ArrowRight />
            </Link>
          </Button>
          <Button asChild size="lg" className="bg-ink-800 text-sand-100 [--duo-edge:theme(colors.ink.950)]">
            <Link href="/login?demo=1">Try the demo</Link>
          </Button>
        </>
      ) : null}
    </div>
  );
}

/** Footer account links. */
export function FooterAuth() {
  const s = useLandingSession();
  if (s.status === "unknown") return <span aria-hidden className="h-5 w-28" />;
  if (s.status === "in")
    return (
      <Link className="hover:text-sand-100" href="/dashboard">
        Dashboard
      </Link>
    );
  return (
    <>
      <Link className="hover:text-sand-100" href="/login">Log in</Link>
      <Link className="hover:text-sand-100" href="/signup">Sign up</Link>
    </>
  );
}
