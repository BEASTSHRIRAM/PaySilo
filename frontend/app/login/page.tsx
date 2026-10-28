"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Eye, EyeOff, Loader2 } from "lucide-react";

import { AuthShell } from "@/components/auth/auth-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError } from "@/lib/api";

function safeNext(next: string | null) {
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/dashboard";
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [passwordFocused, setPasswordFocused] = useState(false);
  const [busy, setBusy] = useState<"login" | "demo" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [demoAvailable, setDemoAvailable] = useState(true);

  // Already signed in? Skip the form.
  useEffect(() => {
    api.me().then(() => router.replace(next)).catch(() => undefined);
  }, [next, router]);

  async function signIn(e: string, p: string, mode: "login" | "demo") {
    setBusy(mode);
    setError(null);
    try {
      await api.login(e, p);
      setSuccess(true);
      setTimeout(() => router.replace(next), 900);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Try again.");
      setBusy(null);
    }
  }

  async function fillDemo(autoSubmit: boolean) {
    setError(null);
    try {
      const creds = await api.demoCredentials();
      setEmail(creds.email);
      setPassword(creds.password);
      if (autoSubmit) await signIn(creds.email, creds.password, "demo");
    } catch (err) {
      setDemoAvailable(false);
      setError(err instanceof ApiError && err.status === 0 ? err.message : "The demo account isn't available on this server.");
    }
  }

  useEffect(() => {
    if (params.get("demo") === "1") void fillDemo(false);
  }, []);

  const mood = success ? "silo-cheer" : passwordFocused && !showPassword ? "silo-shy" : error ? "silo-idle" : "silo-wave";
  const speech = success
    ? "Welcome back! Your silos are exactly where you left them."
    : passwordFocused && !showPassword
      ? "I'm not looking, promise!"
      : error
        ? "Hmm, that didn't work. Let's try again."
        : "Hi, I'm Silo! Ready to run some payroll?";

  return (
    <AuthShell
      mood={mood}
      speech={speech}
      footer={<>New to PaySilo? <Link href="/signup" className="font-extrabold text-coral-400 hover:underline">Create your organization</Link></>}
    >
      <h1 className="font-display text-4xl font-semibold text-sand-100">Log in</h1>
      <p className="mt-2 text-sage-300">Pick up where your agents left off.</p>

      <form
        className="mt-8 grid gap-5"
        onSubmit={(ev) => {
          ev.preventDefault();
          void signIn(email, password, "login");
        }}
      >
        <div className="grid gap-2">
          <Label htmlFor="email">Work email</Label>
          <Input id="email" type="email" autoComplete="email" required value={email}
                 onChange={(ev) => setEmail(ev.target.value)} placeholder="you@agency.com" aria-invalid={!!error} />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="password">Password</Label>
          <div className="relative">
            <Input id="password" type={showPassword ? "text" : "password"} autoComplete="current-password" required
                   value={password} onChange={(ev) => setPassword(ev.target.value)} aria-invalid={!!error}
                   onFocus={() => setPasswordFocused(true)} onBlur={() => setPasswordFocused(false)}
                   className="pr-12" placeholder="••••••••" />
            <button type="button" onClick={() => setShowPassword((v) => !v)}
                    aria-label={showPassword ? "Hide password" : "Show password"}
                    className="absolute right-2 top-1/2 grid size-9 -translate-y-1/2 place-items-center rounded-xl text-sage-400 hover:bg-ink-700 hover:text-sand-100">
              {showPassword ? <EyeOff className="size-5" /> : <Eye className="size-5" />}
            </button>
          </div>
        </div>

        {error && (
          <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
            {error}
          </p>
        )}

        <Button type="submit" size="lg" disabled={!!busy || success}>
          {busy === "login" ? <Loader2 className="animate-spin" /> : null}
          {success ? "Signed in!" : "Log in"}
        </Button>
      </form>

      {demoAvailable && (
        <div className="mt-6">
          <div className="relative my-4 text-center text-xs font-extrabold uppercase tracking-widest text-sage-500">
            <span className="relative z-10 bg-background px-3">or</span>
            <span className="absolute inset-x-0 top-1/2 h-0.5 bg-ink-700" />
          </div>
          <Button type="button" variant="sand" size="lg" className="w-full" disabled={!!busy || success}
                  onClick={() => void fillDemo(true)}>
            {busy === "demo" && <Loader2 className="animate-spin" />}
            Use demo account
          </Button>
          <p className="mt-3 text-center text-sm text-sage-500">
            Signs you in as Vertex Payroll Services, with Acme, Borealis and Cobalt ready to go.
          </p>
        </div>
      )}
    </AuthShell>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
