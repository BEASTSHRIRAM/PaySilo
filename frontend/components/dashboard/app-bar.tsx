"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import {
  BookOpen, CalendarDays, FileUp, Landmark, LayoutDashboard,
  LogOut, Menu, Play, Scale, ShieldCheck, TriangleAlert, X,
} from "lucide-react";

import { Logo } from "@/components/brand/logo";
import { api, type Session } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * v3 navigation. Groups follow the money: Overview / Payroll /
 * Money movement / Trust & audit. Every dashboard page renders this
 * bar, so the full v3 route set is reachable from anywhere.
 */
export const NAV_GROUPS: { label: string; links: { href: string; label: string; icon: typeof LayoutDashboard }[] }[] = [
  {
    label: "Overview",
    links: [{ href: "/dashboard", label: "Command Center", icon: LayoutDashboard }],
  },
  {
    label: "Payroll",
    links: [
      { href: "/dashboard/imports", label: "Imports", icon: FileUp },
      { href: "/dashboard/calendar", label: "Calendar", icon: CalendarDays },
      { href: "/dashboard/execution", label: "Execution", icon: Play },
    ],
  },
  {
    label: "Money movement",
    links: [
      { href: "/dashboard/funding", label: "Funding", icon: Landmark },
      { href: "/dashboard/exceptions", label: "Exceptions", icon: TriangleAlert },
      { href: "/dashboard/reconciliation", label: "Reconciliation", icon: Scale },
    ],
  },
  {
    label: "Trust & audit",
    links: [
      { href: "/dashboard/journals", label: "Journals", icon: BookOpen },
      { href: "/dashboard/evidence", label: "Evidence", icon: ShieldCheck },
    ],
  },
];

function isActive(pathname: string, href: string): boolean {
  if (href === "/dashboard") return pathname === "/dashboard";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function DashboardNav() {
  const pathname = usePathname() ?? "/dashboard";
  return (
    <nav aria-label="Dashboard sections" className="glass mt-2 overflow-x-auto rounded-full border border-sand-200/10 px-2 py-1.5">
      <ul className="flex min-w-max items-center gap-1">
        {NAV_GROUPS.map((group, gi) => (
          <li key={group.label} className="flex items-center gap-1">
            {gi > 0 && <span aria-hidden className="mx-1 h-6 w-px bg-sand-200/15" />}
            <span className="hidden px-1 text-[11px] font-extrabold uppercase tracking-widest text-sage-500 xl:inline">
              {group.label}
            </span>
            {group.links.map(({ href, label, icon: Icon }) => {
              const active = isActive(pathname, href);
              return (
                <Link
                  key={href}
                  href={href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex items-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1.5 text-sm font-bold transition-colors",
                    active
                      ? "bg-coral-500 text-sand-50 [--duo-edge:theme(colors.coral.700)] shadow-duo-sm"
                      : "text-sage-300 hover:bg-sand-200/10 hover:text-sand-100",
                  )}
                >
                  <Icon className="size-4" /> {label}
                </Link>
              );
            })}
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** Same curved glass bar as the marketing site, with the org + account menu. */
export function AppBar({ session }: { session: Session }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

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

  async function logout() {
    await api.logout().catch(() => undefined);
    router.replace("/login");
  }

  const initials = session.user.full_name.split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();

  return (
    <header className="sticky top-0 z-40 px-3 pt-3 sm:px-5">
      <div ref={ref} className="relative mx-auto max-w-6xl">
        <nav className="glass flex h-16 items-center justify-between rounded-full border border-sand-200/10 pl-3 pr-2 shadow-[0_14px_40px_-20px_rgba(0,0,0,0.8)] sm:pl-4">
          <Logo href="/dashboard" />
          <div className="flex items-center gap-2">
            <span className="hidden rounded-full border-2 border-sand-200/20 px-4 py-1.5 text-sm font-bold text-sand-100 sm:inline">
              {session.organization.name}
              {session.organization.is_demo && <span className="ml-2 text-xs font-extrabold uppercase text-coral-400">demo</span>}
            </span>
            <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-label="Account menu"
                    className="flex items-center gap-2 rounded-full bg-sand-200/10 py-1 pl-1 pr-3 ring-1 ring-sand-200/15 hover:bg-sand-200/20">
              <span className="grid size-9 place-items-center rounded-full bg-coral-500 text-sm font-black text-sand-50">{initials}</span>
              {open ? <X className="size-5 text-sand-100" /> : <Menu className="size-5 text-sand-100" />}
            </button>
          </div>
        </nav>
        <DashboardNav />
        <AnimatePresence>
          {open && (
            <motion.div initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }}
                        className="glass absolute right-0 mt-2 w-72 rounded-4xl border border-sand-200/10 p-3 shadow-2xl">
              <div className="rounded-3xl bg-ink-950/40 p-4">
                <p className="font-extrabold text-sand-100">{session.user.full_name}</p>
                <p className="text-sm text-sage-400">{session.user.email}</p>
                <p className="mt-2 text-xs font-extrabold uppercase tracking-wider text-coral-400">
                  {session.role} · {session.organization.name}
                </p>
              </div>
              <button type="button" onClick={() => void logout()}
                      className="mt-2 flex w-full items-center gap-3 rounded-3xl px-4 py-3 text-left font-bold text-sand-100 hover:bg-sand-200/10">
                <LogOut className="size-5 text-sage-400" /> Log out
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </header>
  );
}
