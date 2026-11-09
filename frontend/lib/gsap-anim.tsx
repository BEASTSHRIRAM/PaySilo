"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import gsap from "gsap";

import { formatMoney } from "@/lib/utils";

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Tweens a number from its previous value (or from 0 on first mount) to
 * `target`, returning the value to display. Instant under reduced motion.
 */
function useCountTween(target: number, duration: number): number {
  const [display, setDisplay] = useState(target);
  const fromRef = useRef<number | null>(null);
  const shownRef = useRef(target);

  useEffect(() => {
    const first = fromRef.current === null;
    const from = first ? 0 : shownRef.current;
    fromRef.current = target;
    if (!Number.isFinite(target) || prefersReducedMotion() || from === target) {
      shownRef.current = target;
      setDisplay(target);
      return;
    }
    const state = { v: from };
    const tween = gsap.to(state, {
      v: target,
      duration,
      ease: "power2.out",
      onUpdate: () => {
        shownRef.current = state.v;
        setDisplay(state.v);
      },
      onComplete: () => {
        shownRef.current = target;
        setDisplay(target);
      },
    });
    return () => {
      tween.kill();
    };
  }, [target, duration]);

  return display;
}

/** Counts a money value from zero on mount, then from the previous value on change (instant under reduced motion). */
export function AnimatedMoney({ value, currency = "USD", className }: { value: string; currency?: string; className?: string }) {
  const target = Number(value);
  const shown = useCountTween(target, 0.9);
  const text = Number.isFinite(target) && shown !== target ? formatMoney(shown.toFixed(2), currency) : formatMoney(value, currency);
  return <span className={className}>{text}</span>;
}

/** Counts an integer from zero on mount, then from the previous value on change (instant under reduced motion). */
export function AnimatedNumber({ value, className }: { value: number; className?: string }) {
  const shown = useCountTween(value, 0.8);
  return <span className={className}>{Math.round(shown)}</span>;
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
