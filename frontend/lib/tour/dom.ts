// DOM helpers for the product tour: robust target lookup and waiting.

/** First VISIBLE element for a `data-tour` key (the sidebar and the mobile pill nav can both exist). */
export function findTarget(key: string | undefined, fallbacks: string[] = []): HTMLElement | null {
  const selectors = [...(key ? [`[data-tour="${key}"]`] : []), ...fallbacks];
  for (const sel of selectors) {
    let nodes: NodeListOf<HTMLElement>;
    try {
      nodes = document.querySelectorAll<HTMLElement>(sel);
    } catch {
      continue;
    }
    for (const el of Array.from(nodes)) {
      const r = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      if (r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none") return el;
    }
  }
  return null;
}

/**
 * Resolve with the target once it is in the DOM and visible, or null on timeout. Never rejects, so a
 * missing target degrades to a centered bubble instead of breaking the tour.
 */
export function waitForElement(
  key: string | undefined,
  fallbacks: string[] = [],
  timeout = 5000,
  signal?: AbortSignal,
): Promise<HTMLElement | null> {
  return new Promise((resolve) => {
    if (!key && fallbacks.length === 0) return resolve(null);
    const started = performance.now();
    let done = false;
    const finish = (el: HTMLElement | null) => {
      if (done) return;
      done = true;
      observer.disconnect();
      window.clearInterval(timer);
      resolve(el);
    };
    const check = () => {
      const el = findTarget(key, fallbacks);
      if (el) return finish(el);
      if (signal?.aborted || performance.now() - started > timeout) finish(null);
    };
    const observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true });
    const timer = window.setInterval(check, 120);
    check();
  });
}

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export const isMobileViewport = () => typeof window !== "undefined" && window.matchMedia("(max-width: 639px)").matches;

/** Resolves after the page has been still (no layout-affecting scroll) for a few frames. */
export function nextFrames(n = 2): Promise<void> {
  return new Promise((resolve) => {
    const tick = (left: number) => (left <= 0 ? resolve() : requestAnimationFrame(() => tick(left - 1)));
    tick(n);
  });
}
