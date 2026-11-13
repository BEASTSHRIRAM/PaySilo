"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import { ArrowRight, Menu, X } from "lucide-react";

import { Logo, SiloMark } from "@/components/brand/logo";
import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/#pipeline", label: "How it runs", hint: "Eight agents, one signature" },
  { href: "/#features", label: "What you get", hint: "Silos, held lines, proofs, workflows" },
  { href: "/#trust", label: "Trust", hint: "The agent never holds the pen" },
  { href: "/#partners", label: "Partners", hint: "Airwallex, Visa, Bedrock, LangGraph, Claude, Metal" },
];

/** Floating, fully rounded glass navbar: logo left, pills + hamburger right. */
export function Navbar() {
  const [open, setOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!panelRef.current?.contains(t) && !buttonRef.current?.contains(t)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  return (
    <header className="fixed inset-x-0 top-0 z-50 px-3 pt-3 sm:px-5 sm:pt-4">
      <nav
        aria-label="Main"
        className={cn(
          "glass mx-auto flex h-16 max-w-6xl items-center justify-between rounded-full border border-sand-200/10 pl-3 pr-2 transition-shadow duration-300 sm:pl-4",
          scrolled ? "shadow-[0_18px_50px_-18px_rgba(0,0,0,0.75)]" : "shadow-[0_10px_30px_-20px_rgba(0,0,0,0.6)]",
        )}
      >
        <Logo />

        <div className="flex items-center gap-2">
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
          <button
            ref={buttonRef}
            type="button"
            aria-label={open ? "Close menu" : "Open menu"}
            aria-expanded={open}
            aria-controls="site-menu"
            onClick={() => setOpen((v) => !v)}
            className="grid size-11 place-items-center rounded-full bg-sand-200/10 text-sand-100 ring-1 ring-sand-200/15 transition-colors hover:bg-sand-200/20"
          >
            <span className="sr-only">Menu</span>
            {open ? <X className="size-5" /> : <Menu className="size-5" />}
          </button>
        </div>
      </nav>

      <AnimatePresence>
        {open && (
          <motion.div
            id="site-menu"
            ref={panelRef}
            initial={{ opacity: 0, y: -10, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 420, damping: 32 }}
            className="glass mx-auto mt-2 max-w-6xl origin-top-right overflow-hidden rounded-4xl border border-sand-200/10 p-3 shadow-[0_30px_80px_-30px_rgba(0,0,0,0.8)] sm:ml-auto sm:mr-0 sm:max-w-sm"
          >
            <ul className="grid gap-1">
              {LINKS.map((link) => (
                <li key={link.href}>
                  <Link
                    href={link.href}
                    onClick={() => setOpen(false)}
                    className="group flex items-center justify-between rounded-3xl px-4 py-3 transition-colors hover:bg-sand-200/10"
                  >
                    <span>
                      <span className="block font-bold text-sand-100">{link.label}</span>
                      <span className="block text-sm text-sage-400">{link.hint}</span>
                    </span>
                    <ArrowRight className="size-4 text-coral-400 opacity-0 transition-all group-hover:translate-x-0.5 group-hover:opacity-100" />
                  </Link>
                </li>
              ))}
            </ul>
            <div className="mt-2 flex items-center gap-3 rounded-3xl bg-ink-950/40 p-3">
              <SiloMark className="size-10 shrink-0" />
              <p className="text-sm text-sage-300">
                Hi, I&apos;m <span className="font-bold text-sand-100">Silo</span>. I never mix one company&apos;s money with another&apos;s.
              </p>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 sm:hidden">
              <Link href="/login" onClick={() => setOpen(false)}
                className="flex h-11 items-center justify-center rounded-full border-2 border-sand-200/25 text-sm font-bold text-sand-100">
                Log in
              </Link>
              <Link href="/signup" onClick={() => setOpen(false)}
                className="flex h-11 items-center justify-center rounded-full bg-coral-500 text-sm font-extrabold text-sand-50">
                Get started
              </Link>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </header>
  );
}
