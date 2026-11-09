"use client";

import { useEffect, useRef, type ReactNode } from "react";
import gsap from "gsap";

import { formatMoney } from "@/lib/utils";

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Counts a money value up from zero on mount (instant under reduced motion). */
export function AnimatedMoney({ value, currency = "USD", className }: { value: string; currency?: string; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const target = Number(value);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (!Number.isFinite(target) || prefersReducedMotion()) {
      el.textContent = formatMoney(value, currency);
      return;
    }
    const state = { v: 0 };
    const tween = gsap.to(state, {
      v: target,
      duration: 0.9,
      ease: "power2.out",
      onUpdate: () => {
        el.textContent = formatMoney(state.v.toFixed(2), currency);
      },
      onComplete: () => {
        el.textContent = formatMoney(value, currency);
      },
    });
    return () => {
      tween.kill();
    };
  }, [target, value, currency]);

  return (
    <span ref={ref} className={className}>
      {formatMoney(value, currency)}
    </span>
  );
}

/** Counts an integer up from zero on mount (instant under reduced motion). */
export function AnimatedNumber({ value, className }: { value: number; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (prefersReducedMotion()) {
      el.textContent = String(value);
      return;
    }
    const state = { v: 0 };
    const tween = gsap.to(state, {
      v: value,
      duration: 0.8,
      ease: "power2.out",
      onUpdate: () => {
        el.textContent = String(Math.round(state.v));
      },
    });
    return () => {
      tween.kill();
    };
  }, [value]);

  return (
    <span ref={ref} className={className}>
      {value}
    </span>
  );
}

/** Staggers its direct children in on mount. A no-op under reduced motion. */
export function Stagger({ children, className }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || prefersReducedMotion() || el.children.length === 0) return;
    const tween = gsap.from(el.children, {
      opacity: 0,
      y: 14,
      duration: 0.45,
      stagger: 0.07,
      ease: "power2.out",
      clearProps: "opacity,transform",
    });
    return () => {
      tween.kill();
    };
  }, []);

  return (
    <div ref={ref} className={className}>
      {children}
    </div>
  );
}

/** Reveals a vertical timeline's items one by one. A no-op under reduced motion. */
export function TimelineReveal({ children, className }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLOListElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || prefersReducedMotion() || el.children.length === 0) return;
    const tween = gsap.from(el.children, {
      opacity: 0,
      x: -10,
      duration: 0.35,
      stagger: 0.06,
      ease: "power1.out",
      clearProps: "opacity,transform",
    });
    return () => {
      tween.kill();
    };
  }, []);

  return (
    <ol ref={ref} className={className}>
      {children}
    </ol>
  );
}
