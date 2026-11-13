"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { OtpInput } from "@/components/auth/otp-input";
import { api, ApiError, type Session } from "@/lib/api";

/** Seconds left until `resend` is allowed again. */
export function useCountdown(initial: number) {
  const [left, setLeft] = useState(initial);
  useEffect(() => {
    if (left <= 0) return;
    const t = setTimeout(() => setLeft((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [left]);
  return [left, setLeft] as const;
}

/** Friendly copy for OTP related API errors. */
export function otpErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case "OTP_INVALID": return "That code is not right. Check it and try again.";
      case "OTP_EXPIRED": return "That code has expired. Request a new one.";
      case "OTP_LOCKED": return "Too many wrong attempts. Request a new code to try again.";
      case "RATE_LIMITED": return err.message || "Too many tries. Wait a few minutes and try again.";
      case "RESEND_COOLDOWN": return err.message || "Please wait a moment before requesting another code.";
    }
    if (err.status === 0) return err.message;
    return err.message || "Something went wrong. Try again.";
  }
  return "Something went wrong. Try again.";
}

export function ResendButton({ email, initial = 60, onError, onSent, send }: {
  email: string; initial?: number; onError?: (msg: string) => void; onSent?: () => void;
  /** Sends a new code; defaults to the verify email resend. */
  send?: (email: string) => Promise<{ resend_in: number }>;
}) {
  const [left, setLeft] = useCountdown(initial);
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  const resend = useCallback(async () => {
    setBusy(true);
    setSent(false);
    try {
      const r = await (send ?? api.resendOtp)(email);
      setLeft(r.resend_in || 60);
      setSent(true);
      onSent?.();
    } catch (err) {
      if (err instanceof ApiError && err.code === "RESEND_COOLDOWN") setLeft(60);
      onError?.(otpErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [email, onError, onSent, send, setLeft]);

  return (
    <div className="flex items-center gap-3 text-sm">
      <button type="button" onClick={() => void resend()} disabled={left > 0 || busy}
              className="font-extrabold text-coral-400 hover:underline disabled:cursor-not-allowed disabled:text-sage-500 disabled:no-underline">
        {busy ? "Sending…" : left > 0 ? `Resend in ${left}s` : "Resend code"}
      </button>
      {sent && left > 0 && <span role="status" className="font-semibold text-sage-300">New code sent.</span>}
    </div>
  );
}

interface PanelProps {
  email: string;
  resendIn?: number;
  onVerified: (session: Session) => void;
  onChangeEmail: () => void;
}

/** Inline "Check your email" screen used after signup and after a login on an unverified account. */
export function VerifyEmailPanel({ email, resendIn = 60, onVerified, onChangeEmail }: PanelProps) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shake, setShake] = useState(0);

  async function verify(c: string) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const session = await api.verifyEmail(email, c);
      onVerified(session);
    } catch (err) {
      setError(otpErrorMessage(err));
      setShake((n) => n + 1);
      setCode("");
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="verify-title" className="grid gap-6">
      <div>
        <h1 id="verify-title" className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Check your email</h1>
        <p className="mt-3 break-words text-sage-300">
          We sent a 6 digit code to <span className="font-extrabold text-sand-100">{email}</span>. It expires in 10 minutes.
        </p>
      </div>

      <OtpInput value={code} onChange={(v) => { setCode(v); if (error) setError(null); }} onComplete={(c) => void verify(c)}
                disabled={busy} error={error} shakeKey={shake} autoFocus />

      {busy && <p role="status" className="flex items-center gap-2 text-sm font-bold text-sage-300"><Loader2 className="size-4 animate-spin" /> Verifying…</p>}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <ResendButton email={email} initial={resendIn} onError={setError} onSent={() => { setError(null); setCode(""); }} />
        <button type="button" onClick={onChangeEmail} className="text-sm font-extrabold text-sage-300 hover:text-sand-100 hover:underline">
          Change email
        </button>
      </div>
    </section>
  );
}
