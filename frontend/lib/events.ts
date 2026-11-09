"use client";

import { useEffect, useRef } from "react";

/** Fired after any action that changes server data (a decision, a run, funding). */
export const DATA_CHANGED_EVENT = "paysilo:data-changed";

export function notifyDataChanged(detail?: { runId?: string }): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(DATA_CHANGED_EVENT, { detail }));
}

/** Subscribe to data-changed signals. The latest callback is always used; subscribers must only re-fetch, never emit. */
export function useDataChanged(callback: () => void): void {
  const ref = useRef(callback);
  ref.current = callback;
  useEffect(() => {
    const handler = () => ref.current();
    window.addEventListener(DATA_CHANGED_EVENT, handler);
    return () => window.removeEventListener(DATA_CHANGED_EVENT, handler);
  }, []);
}
