"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Check, Eye, EyeOff, Loader2 } from "lucide-react";

import { AuthShell } from "@/components/auth/auth-shell";
import { OtpInput } from "@/components/auth/otp-input";
import { otpErrorMessage, ResendButton } from "@/components/auth/verify-email-panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError } from "@/lib/api";

type Step = "email" | "reset" | "done";

function passwordProblem(p: string): string | null {
  if (p.length < 8) return "Use at least 8 characters.";
  if (!(/[A-Za-z]/.test(p) && /\d/.test(p))) return "Mix in at least one letter and one number.";
  if (new TextEncoder().encode(p).length > 72) return "That is too long (72 bytes max).";
  return null;
}

export default function ForgotPasswordPage() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [resendIn, setResendIn] = useState(60);
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [codeError, setCodeError] = useState<string | null>(null);
  const [shake, setShake] = useState(0);

  async function sendCode() {
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())) {
      setError("That email does not look right.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api.forgotPassword(email.trim());
      setResendIn(r.resend_in || 60);
      setStep("reset");
    } catch (err) {
      setError(otpErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setError(null);
    setCodeError(null);
    if (!/^\d{6}$/.test(code)) {
      setCodeError("Enter the 6 digit code from your email.");
      setShake((n) => n + 1);
      return;
    }
    const problem = passwordProblem(password);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    try {
      await api.resetPassword(email.trim(), code, password);
      setStep("done");
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        setError(err.fields["body.new_password"] || err.fields["new_password"] || err.message);
      } else {
        setCodeError(otpErrorMessage(err));
        setShake((n) => n + 1);
        setCode("");
      }
    } finally {
      setBusy(false);
    }
  }

  const speech = step === "done" ? "All set. Your new password is ready." : step === "reset" ? "Check your inbox. The code is on its way." : "It happens. Let's get you back in.";

  return (
    <AuthShell mood={step === "done" ? "silo-cheer" : "silo-idle"} speech={speech}
      footer={<>Remembered it? <Link href="/login" className="font-extrabold text-coral-400 hover:underline">Log in</Link></>}>
      {step === "email" && (
        <form className="grid gap-5" onSubmit={(e) => { e.preventDefault(); void sendCode(); }}>
          <div>
            <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Reset your password</h1>
            <p className="mt-3 text-sage-300">Enter your work email and we will send you a 6 digit code.</p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="fp-email">Work email</Label>
            <Input id="fp-email" type="email" autoComplete="email" required autoFocus value={email}
                   onChange={(e) => setEmail(e.target.value)} placeholder="you@agency.com" aria-invalid={!!error} />
          </div>
          {error && <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">{error}</p>}
          <Button type="submit" size="lg" disabled={busy}>
            {busy && <Loader2 className="animate-spin" />} Send code
          </Button>
        </form>
      )}

      {step === "reset" && (
        <form className="grid gap-6" onSubmit={(e) => { e.preventDefault(); void reset(); }}>
          <div>
            <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Check your email</h1>
            <p className="mt-3 break-words text-sage-300">
              If an account exists for <span className="font-extrabold text-sand-100">{email.trim()}</span>, we sent a code. It expires in 10 minutes.
            </p>
          </div>
          <OtpInput value={code} onChange={(v) => { setCode(v); setCodeError(null); }} disabled={busy} error={codeError} shakeKey={shake} autoFocus />
          <div className="grid gap-2">
            <Label htmlFor="fp-password">New password</Label>
            <div className="relative">
              <Input id="fp-password" type={showPassword ? "text" : "password"} autoComplete="new-password" className="pr-12"
                     value={password} onChange={(e) => { setPassword(e.target.value); setError(null); }}
                     placeholder="At least 8 characters" aria-invalid={!!error} />
              <button type="button" onClick={() => setShowPassword((v) => !v)} aria-label={showPassword ? "Hide password" : "Show password"}
                      className="absolute right-2 top-1/2 grid size-9 -translate-y-1/2 place-items-center rounded-xl text-sage-400 hover:bg-ink-700 hover:text-sand-100">
                {showPassword ? <EyeOff className="size-5" /> : <Eye className="size-5" />}
              </button>
            </div>
            {error && <p role="alert" className="text-sm font-bold text-coral-300">{error}</p>}
          </div>
          <Button type="submit" size="lg" disabled={busy}>
            {busy && <Loader2 className="animate-spin" />} Reset password
          </Button>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <ResendButton email={email.trim()} initial={resendIn} send={api.forgotPassword}
                          onError={setCodeError} onSent={() => { setCodeError(null); setCode(""); }} />
            <button type="button" onClick={() => { setStep("email"); setCode(""); setError(null); setCodeError(null); }}
                    className="text-sm font-extrabold text-sage-300 hover:text-sand-100 hover:underline">
              Change email
            </button>
          </div>
        </form>
      )}

      {step === "done" && (
        <div className="grid gap-5 text-center">
          <div className="mx-auto grid size-20 place-items-center rounded-full bg-mint-500 text-ink-950 shadow-[0_5px_0_0_theme(colors.mint.700)]">
            <Check className="size-10" strokeWidth={3} />
          </div>
          <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Password updated</h1>
          <p className="text-sage-300">You can now sign in with your new password.</p>
          <Button size="lg" onClick={() => router.push("/login")}>Sign in</Button>
        </div>
      )}
    </AuthShell>
  );
}
