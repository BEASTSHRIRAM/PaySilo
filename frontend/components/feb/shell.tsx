"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { AppBar } from "@/components/dashboard/app-bar";
import type { Session } from "@/lib/api";

/** Shared dashboard chrome for the v3 FEB screens: AppBar + centred column + heading. */
export function FebShell({
  session,
  title,
  subtitle,
  backHref = "/dashboard",
  backLabel = "Dashboard",
  actions,
  children,
}: {
  session: Session | null;
  title: string;
  subtitle: string;
  backHref?: string;
  backLabel?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}
      <main className="mx-auto mt-8 grid max-w-6xl gap-8 px-4 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Link
            href={backHref}
            className="inline-flex items-center gap-2 font-bold text-sage-300 hover:text-sand-100"
          >
            <ArrowLeft className="size-4" /> {backLabel}
          </Link>
          {actions}
        </div>
        <section>
          <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">{title}</h1>
          <p className="mt-1 max-w-2xl text-sage-300">{subtitle}</p>
        </section>
        {children}
      </main>
    </div>
  );
}

/** Label + native select pair used by the run / employer pickers. */
export function PickerField({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="grid min-w-56 flex-1 gap-1.5">
      <span className="text-sm font-extrabold text-sage-400">{label}</span>
      {children}
    </label>
  );
}
