"use client";

import { useEffect, useRef } from "react";

import { cn } from "@/lib/utils";

const LENGTH = 6;

interface Props {
  value: string;
  onChange: (value: string) => void;
  /** Called once when all 6 digits are present. */
  onComplete?: (code: string) => void;
  disabled?: boolean;
  error?: string | null;
  autoFocus?: boolean;
  /** Bump this number to make the boxes shake (e.g. after a wrong code). */
  shakeKey?: number;
}

/** Six single digit boxes with auto advance, paste support and arrow key navigation. */
export function OtpInput({ value, onChange, onComplete, disabled, error, autoFocus, shakeKey }: Props) {
  const refs = useRef<(HTMLInputElement | null)[]>([]);
  const digits = Array.from({ length: LENGTH }, (_, i) => value[i] ?? "");

  useEffect(() => {
    if (autoFocus) refs.current[0]?.focus();
  }, [autoFocus]);

  function focusAt(i: number) {
    const el = refs.current[Math.max(0, Math.min(LENGTH - 1, i))];
    el?.focus();
    el?.select();
  }

  function commit(next: string[]) {
    const code = next.join("").slice(0, LENGTH);
    onChange(code);
    if (code.length === LENGTH && /^\d{6}$/.test(code)) onComplete?.(code);
  }

  function handleChange(i: number, raw: string) {
    const clean = raw.replace(/\D/g, "");
    if (!clean) return;
    const next = [...digits];
    if (clean.length > 1) {
      // Autofill or fast typing: spread across the following boxes.
      clean.slice(0, LENGTH - i).split("").forEach((d, k) => { next[i + k] = d; });
      commit(next);
      focusAt(Math.min(i + clean.length, LENGTH - 1));
      return;
    }
    next[i] = clean;
    commit(next);
    if (i < LENGTH - 1) focusAt(i + 1);
  }

  function handleKeyDown(i: number, e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Backspace") {
      e.preventDefault();
      const next = [...digits];
      if (next[i]) {
        next[i] = "";
        onChange(next.join("").replace(/\s/g, ""));
      } else if (i > 0) {
        next[i - 1] = "";
        onChange(next.join(""));
        focusAt(i - 1);
      }
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      focusAt(i - 1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      focusAt(i + 1);
    }
  }

  function handlePaste(e: React.ClipboardEvent<HTMLInputElement>) {
    const clean = e.clipboardData.getData("text").replace(/\D/g, "").slice(0, LENGTH);
    if (!clean) return;
    e.preventDefault();
    commit(clean.split(""));
    focusAt(Math.min(clean.length, LENGTH - 1));
  }

  return (
    <div>
      <div key={shakeKey ?? 0} className={cn("flex w-full max-w-full justify-between gap-2 sm:gap-3", error && "animate-otp-shake")} role="group" aria-label="Verification code">
        {digits.map((d, i) => (
          <input
            key={i}
            ref={(el) => { refs.current[i] = el; }}
            type="text"
            inputMode="numeric"
            autoComplete={i === 0 ? "one-time-code" : "off"}
            maxLength={LENGTH}
            pattern="[0-9]*"
            value={d}
            disabled={disabled}
            aria-label={`Digit ${i + 1} of ${LENGTH}`}
            aria-invalid={!!error}
            onChange={(e) => handleChange(i, e.target.value)}
            onKeyDown={(e) => handleKeyDown(i, e)}
            onPaste={handlePaste}
            onFocus={(e) => e.target.select()}
            className={cn(
              "h-14 w-0 min-w-0 flex-1 rounded-2xl border-2 bg-ink-900/80 text-center font-display text-2xl font-semibold text-sand-100 transition-colors focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25 disabled:cursor-not-allowed disabled:opacity-50 sm:h-16 sm:text-3xl",
              error ? "border-coral-500 focus-visible:ring-coral-500/25" : d ? "border-sage-500" : "border-ink-600",
            )}
          />
        ))}
      </div>
      {error && <p role="alert" className="mt-3 text-sm font-bold text-coral-300">{error}</p>}
    </div>
  );
}
