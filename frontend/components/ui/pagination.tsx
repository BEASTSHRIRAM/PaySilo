"use client";

import * as React from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

export const PAGE_SIZES = [5, 10, 25] as const;

function readParam(key: string): number {
  if (typeof window === "undefined") return 1;
  const n = parseInt(new URLSearchParams(window.location.search).get(key) ?? "", 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}

function writeParam(key: string, page: number) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (page <= 1) url.searchParams.delete(key);
  else url.searchParams.set(key, String(page));
  window.history.replaceState(window.history.state, "", url.toString());
}

export interface PaginationState<T> {
  /** Items for the current page. */
  pageItems: T[];
  page: number;
  pageCount: number;
  total: number;
  pageSize: number;
  from: number;
  to: number;
  setPage: (p: number) => void;
  setPageSize: (n: number) => void;
  /** Attach to the list container so the top scrolls into view on page change. */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  listRef: React.RefObject<any>;
}

/**
 * Client-side pagination. `key` namespaces the `?key=` URL param (e.g. "runsPage").
 * The page resets to 1 whenever `resetKey` changes (pass the filter/search value).
 * Hydrates the page from the URL after mount (SSR-safe).
 */
export function usePagination<T>(
  items: T[],
  pageSize = 10,
  opts: { key?: string; resetKey?: unknown } = {},
): PaginationState<T> {
  const { key = "page", resetKey } = opts;
  const [size, setSize] = React.useState(pageSize);
  const [page, setPageState] = React.useState(1);
  const listRef = React.useRef<HTMLElement>(null);
  const hydrated = React.useRef(false);
  const lastReset = React.useRef<unknown>(resetKey);
  const total = items.length;
  const pageCount = Math.max(1, Math.ceil(total / size));

  // Hydrate from the URL once, after mount.
  React.useEffect(() => {
    setPageState(readParam(key));
    hydrated.current = true;
  }, [key]);

  // Filter/search changed: back to page 1.
  React.useEffect(() => {
    if (lastReset.current !== resetKey) {
      lastReset.current = resetKey;
      setPageState(1);
    }
  }, [resetKey]);

  // Clamp when data shrinks. Only once data has loaded (total > 0) so a deep
  // link survives the initial empty render.
  const current = total > 0 ? Math.min(page, pageCount) : page;
  React.useEffect(() => {
    if (!hydrated.current) return;
    if (total > 0 && page !== current) setPageState(current);
  }, [total, page, current]);

  React.useEffect(() => {
    if (!hydrated.current) return;
    writeParam(key, current);
  }, [key, current]);

  const scrollTop = () => {
    const el = listRef.current;
    if (!el) return;
    const top = el.getBoundingClientRect().top;
    if (top < 80 || top > window.innerHeight * 0.6) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };

  const setPage = (p: number) => {
    setPageState(Math.min(Math.max(1, p), pageCount));
    scrollTop();
  };
  const setPageSize = (n: number) => {
    setSize(n);
    setPageState(1);
  };

  const start = (current - 1) * size;
  return {
    pageItems: items.slice(start, start + size),
    page: current,
    pageCount,
    total,
    pageSize: size,
    from: total === 0 ? 0 : start + 1,
    to: Math.min(start + size, total),
    setPage,
    setPageSize,
    listRef,
  };
}

/** [1, "...", 4, 5, 6, "...", 12] style window. */
function pageWindow(page: number, count: number): (number | "gap")[] {
  if (count <= 7) return Array.from({ length: count }, (_, i) => i + 1);
  const set = new Set<number>([1, count, page - 1, page, page + 1]);
  if (page <= 3) [2, 3, 4].forEach((n) => set.add(n));
  if (page >= count - 2) [count - 1, count - 2, count - 3].forEach((n) => set.add(n));
  const nums = Array.from(set)
    .filter((n) => n >= 1 && n <= count)
    .sort((a, b) => a - b);
  const out: (number | "gap")[] = [];
  nums.forEach((n, i) => {
    if (i > 0 && n - nums[i - 1] > 1) out.push("gap");
    out.push(n);
  });
  return out;
}

const navBtn =
  "inline-flex h-9 items-center gap-1 rounded-xl border-2 border-ink-600 bg-ink-800/70 px-3 text-xs font-extrabold uppercase tracking-wide text-sand-200 shadow-[0_3px_0_0_theme(colors.ink.600)] transition hover:bg-ink-700 active:translate-y-[3px] active:shadow-none focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60 disabled:pointer-events-none disabled:opacity-40";

export function Pagination({
  state,
  label = "items",
  sizes = PAGE_SIZES,
  className,
}: {
  state: Pick<
    PaginationState<unknown>,
    "page" | "pageCount" | "total" | "pageSize" | "from" | "to" | "setPage" | "setPageSize"
  >;
  label?: string;
  sizes?: readonly number[];
  className?: string;
}) {
  const { page, pageCount, total, pageSize, from, to, setPage, setPageSize } = state;
  // Hide when everything fits on one page at the smallest offered size.
  if (total <= Math.min(...sizes)) return null;
  const fitsOnePage = pageCount <= 1;

  return (
    <nav
      aria-label={`${label} pagination`}
      className={cn("mt-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-3", className)}
    >
      <p className="text-xs font-semibold text-sage-300" aria-live="polite">
        Showing {from}–{to} of {total}
      </p>

      <div className="flex items-center gap-2">
        <label className="flex items-center gap-2 text-xs font-semibold text-sage-300">
          <span className="sr-only sm:not-sr-only">Per page</span>
          <select
            value={pageSize}
            onChange={(e) => setPageSize(Number(e.target.value))}
            aria-label={`${label} per page`}
            className="h-9 rounded-xl border-2 border-ink-600 bg-ink-900/80 px-2 text-xs font-bold text-sand-100 focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25"
          >
            {sizes.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>

        {!fitsOnePage && (
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              className={navBtn}
              disabled={page <= 1}
              onClick={() => setPage(page - 1)}
              aria-label="Previous page"
            >
              <ChevronLeft className="size-4" />
              <span className="hidden sm:inline">Prev</span>
            </button>

            <span className="px-1 text-xs font-bold text-sand-200 sm:hidden" aria-hidden="true">
              Page {page} of {pageCount}
            </span>

            <ul className="hidden items-center gap-1 sm:flex">
              {pageWindow(page, pageCount).map((p, i) =>
                p === "gap" ? (
                  <li key={`e${i}`} aria-hidden="true" className="px-1 text-xs font-bold text-sage-400">
                    ...
                  </li>
                ) : (
                  <li key={p}>
                    <button
                      type="button"
                      onClick={() => setPage(p)}
                      aria-label={`Page ${p}`}
                      aria-current={p === page ? "page" : undefined}
                      className={cn(
                        "h-9 min-w-9 rounded-xl px-2 text-xs font-extrabold transition focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60",
                        p === page
                          ? "bg-coral-500 text-sand-50"
                          : "border-2 border-ink-600 bg-ink-800/70 text-sand-200 hover:bg-ink-700",
                      )}
                    >
                      {p}
                    </button>
                  </li>
                ),
              )}
            </ul>

            <button
              type="button"
              className={navBtn}
              disabled={page >= pageCount}
              onClick={() => setPage(page + 1)}
              aria-label="Next page"
            >
              <span className="hidden sm:inline">Next</span>
              <ChevronRight className="size-4" />
            </button>
          </div>
        )}
      </div>
    </nav>
  );
}
