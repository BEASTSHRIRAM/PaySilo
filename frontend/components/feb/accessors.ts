/** Small typed accessors for backend-owned records (evidence manifest / tap / metal). */

export function asStr(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

export function asBool(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}

export function rec(v: unknown): Record<string, unknown> | null {
  return typeof v === "object" && v !== null && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

/** First non-empty string among the given keys of a record. */
export function pickStr(r: Record<string, unknown> | null, keys: string[]): string | null {
  if (!r) return null;
  for (const k of keys) {
    const v = asStr(r[k]);
    if (v) return v;
  }
  return null;
}

/** First boolean among the given keys of a record. */
export function pickBool(r: Record<string, unknown> | null, keys: string[]): boolean | null {
  if (!r) return null;
  for (const k of keys) {
    const v = asBool(r[k]);
    if (v !== null) return v;
  }
  return null;
}

/** Decimal-string/number to a finite number, or null when unparseable. */
export function num(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** True when a money value is present and not zero. */
export function isNonZero(v: string | number | null | undefined): boolean {
  const n = num(v);
  return n !== null && Math.abs(n) > 0.000001;
}
