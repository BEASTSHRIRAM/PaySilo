"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";

if (typeof window !== "undefined") gsap.registerPlugin(ScrollTrigger);

export { gsap, ScrollTrigger };

export const REDUCED_QUERY = "(prefers-reduced-motion: reduce)";

export function prefersReducedMotion() {
  return typeof window !== "undefined" && window.matchMedia(REDUCED_QUERY).matches;
}

/** Keep ScrollTrigger measurements correct after resize and after web fonts land. */
export function useRefreshOnLayout() {
  useEffect(() => {
    let t: ReturnType<typeof setTimeout>;
    const refresh = () => {
      clearTimeout(t);
      t = setTimeout(() => ScrollTrigger.refresh(), 150);
    };
    window.addEventListener("resize", refresh);
    window.addEventListener("load", refresh);
    document.fonts?.ready.then(refresh);
    return () => {
      clearTimeout(t);
      window.removeEventListener("resize", refresh);
      window.removeEventListener("load", refresh);
    };
  }, []);
}

/**
 * Mount children only while they are on screen (used for Lottie so nothing plays off screen).
 * Keeps the box size so nothing shifts.
 */
export function InView({ children, className, margin = "120px" }: { children: ReactNode; className?: string; margin?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [on, setOn] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([e]) => setOn(e.isIntersecting), { rootMargin: margin });
    io.observe(el);
    return () => io.disconnect();
  }, [margin]);
  return (
    <div ref={ref} className={className}>
      {on ? children : null}
    </div>
  );
}

/**
 * Build a paused GSAP timeline inside a gsap.context, play it once when the element scrolls into view,
 * and replay it on hover or keyboard focus. Skipped entirely under reduced motion so the markup stays in
 * its final, readable state.
 */
export function usePlayOnView(build: (root: HTMLElement, q: gsap.utils.SelectorFunc) => gsap.core.Timeline, start = "top 82%") {
  const ref = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const root = ref.current;
    if (!root || prefersReducedMotion()) return;
    const ctx = gsap.context(() => {
      const q = gsap.utils.selector(root);
      const tl = build(root, q).pause();
      // IntersectionObserver so tiles inside horizontal carousels play when they scroll into view sideways too.
      const io = new IntersectionObserver(
        ([e]) => {
          if (e.isIntersecting) {
            tl.play(0);
            io.disconnect();
          }
        },
        { threshold: 0.35 },
      );
      io.observe(root);
      void start;
      const replay = () => {
        if (tl.progress() === 1) tl.restart();
      };
      root.addEventListener("pointerenter", replay);
      root.addEventListener("focusin", replay);
      return () => io.disconnect();
    }, root);
    return () => ctx.revert();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return ref;
}
