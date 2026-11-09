"use client";

/**
 * Hand-rolled SVG charts (no chart library in this stack):
 * - MatchedRing: a two-arc progress ring. The matched share is drawn in
 *   mint/sage; any unmatched remainder is drawn in coral so a shortfall
 *   is visible at a glance and can never be hidden by the headline number.
 * - CurrencyDonut: stroke-segment donut over per-currency closing cash.
 */

export const DONUT_PALETTE = [
  "#90AEAD", // sage
  "#E64833", // coral
  "#F3D7B0", // sand-300
  "#874F41", // clay
  "#5BBF97", // mint
  "#3D6E7E", // ink-500
  "#EC6A52", // coral-400
  "#B9CFCE", // sage-300
];

export function MatchedRing({ percent, size = 168 }: { percent: number; size?: number }) {
  const pct = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0));
  const stroke = 14;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const matchedLen = (pct / 100) * c;
  const full = pct >= 99.995;
  return (
    <div className="relative inline-grid place-items-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img"
           aria-label={`Matched ${pct.toFixed(1)} percent`}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#244855" strokeWidth={stroke} />
        {/* unmatched remainder, in coral, starting where the matched arc ends */}
        {!full && pct > 0 && (
          <circle
            cx={size / 2} cy={size / 2} r={r} fill="none"
            stroke="#E64833" strokeWidth={stroke}
            strokeDasharray={`${c - matchedLen} ${matchedLen}`}
            strokeDashoffset={-matchedLen}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
          />
        )}
        {pct > 0 && (
          <circle
            cx={size / 2} cy={size / 2} r={r} fill="none"
            stroke={full ? "#5BBF97" : "#90AEAD"} strokeWidth={stroke} strokeLinecap="round"
            strokeDasharray={`${matchedLen} ${c - matchedLen}`}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
            style={{ transition: "stroke-dasharray 600ms ease" }}
          />
        )}
        {pct === 0 && (
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#E64833" strokeWidth={stroke} />
        )}
      </svg>
      <div className="absolute inset-0 grid place-content-center text-center">
        <p className="font-display text-3xl font-semibold tabular-nums text-sand-100">
          {pct.toFixed(pct % 1 === 0 ? 0 : 1)}%
        </p>
        <p className="text-xs font-extrabold text-sage-400">matched</p>
      </div>
    </div>
  );
}

export interface DonutSlice { label: string; value: number; color: string }

export function CurrencyDonut({
  slices,
  size = 196,
  centerLabel,
  centerValue,
}: {
  slices: DonutSlice[];
  size?: number;
  centerLabel: string;
  centerValue: string;
}) {
  const stroke = 26;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const total = slices.reduce((a, s) => a + Math.max(0, s.value), 0);
  let acc = 0;
  return (
    <div className="relative inline-grid place-items-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={centerLabel}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#244855" strokeWidth={stroke} />
        {total > 0 &&
          slices.map((s) => {
            const frac = Math.max(0, s.value) / total;
            const len = frac * c;
            const offset = -acc;
            acc += len;
            if (len <= 0) return null;
            return (
              <circle
                key={s.label}
                cx={size / 2} cy={size / 2} r={r} fill="none"
                stroke={s.color} strokeWidth={stroke}
                strokeDasharray={`${Math.max(0, len - 2)} ${c - len + 2}`}
                strokeDashoffset={offset}
                transform={`rotate(-90 ${size / 2} ${size / 2})`}
              />
            );
          })}
      </svg>
      <div className="absolute inset-0 grid place-content-center px-8 text-center">
        <p className="text-[11px] font-extrabold text-sage-400">{centerLabel}</p>
        <p className="font-display text-lg font-semibold tabular-nums leading-tight text-sand-100">{centerValue}</p>
      </div>
    </div>
  );
}
