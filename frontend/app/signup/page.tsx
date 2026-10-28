"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import { ArrowLeft, ArrowRight, Building2, Check, Eye, EyeOff, Loader2, UserRound, X } from "lucide-react";

import { AuthShell } from "@/components/auth/auth-shell";
import { LottieAnimation } from "@/components/brand/lottie";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/select";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

const COUNTRIES: [string, string][] = [
  ["IN", "India"], ["GB", "United Kingdom"], ["US", "United States"], ["DE", "Germany"], ["NL", "Netherlands"],
  ["FR", "France"], ["ES", "Spain"], ["SE", "Sweden"], ["IE", "Ireland"], ["SG", "Singapore"], ["AE", "United Arab Emirates"],
  ["AU", "Australia"], ["HK", "Hong Kong"], ["CA", "Canada"],
];
const CURRENCIES = ["USD", "EUR", "GBP", "INR", "SGD", "AUD", "AED", "HKD"];
const SIZES = ["1-10", "11-50", "51-200", "201-1000", "1000+"] as const;
const INDUSTRIES = ["Payroll bureau", "Staffing agency", "Employer of record", "Accounting firm", "HR platform", "Other"];

type Org = { name: string; legal_name: string; country: string; base_currency: string; industry: string; size_band: string; website: string };
type Admin = { full_name: string; email: string; password: string; confirm: string };
type Errors = Record<string, string>;

const STEP_FIELDS: Record<number, string[]> = {
  0: ["organization.name", "organization.legal_name", "organization.country", "organization.base_currency", "organization.website", "organization.size_band", "organization.industry"],
  1: ["admin.full_name", "admin.email", "admin.password", "admin.confirm"],
};

function passwordScore(p: string) {
  let score = 0;
  if (p.length >= 8) score++;
  if (p.length >= 12) score++;
  if (/[A-Za-z]/.test(p) && /\d/.test(p)) score++;
  if (/[^A-Za-z0-9]/.test(p) || /[A-Z]/.test(p)) score++;
  return score;
}

function validateOrg(o: Org): Errors {
  const e: Errors = {};
  if (o.name.trim().length < 2) e["organization.name"] = "Tell us your organization's name.";
  if (!o.country) e["organization.country"] = "Pick a country.";
  if (!o.base_currency) e["organization.base_currency"] = "Pick a base currency.";
  if (o.website && !/^https?:\/\/\S+\.\S+/.test(o.website)) e["organization.website"] = "Use a full URL, like https://agency.com";
  return e;
}

function validateAdmin(a: Admin): Errors {
  const e: Errors = {};
  if (a.full_name.trim().length < 2) e["admin.full_name"] = "What should we call you?";
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(a.email)) e["admin.email"] = "That email doesn't look right.";
  if (a.password.length < 8) e["admin.password"] = "Use at least 8 characters.";
  else if (!(/[A-Za-z]/.test(a.password) && /\d/.test(a.password))) e["admin.password"] = "Mix in at least one letter and one number.";
  else if (new TextEncoder().encode(a.password).length > 72) e["admin.password"] = "That's too long (72 bytes max).";
  if (a.confirm !== a.password) e["admin.confirm"] = "Passwords don't match.";
  return e;
}

function FieldError({ msg }: { msg?: string }) {
  return msg ? <p className="text-sm font-bold text-coral-300">{msg}</p> : null;
}

function Tile({ selected, onClick, children }: { selected: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={selected}
      className={cn(
        "rounded-2xl border-2 px-3 py-2.5 text-sm font-extrabold transition-all active:translate-y-[3px] active:shadow-none",
        selected
          ? "border-sage-400 bg-sage-500/20 text-sand-100 shadow-[0_3px_0_0_theme(colors.sage.600)]"
          : "border-ink-600 bg-ink-850 text-sage-300 shadow-[0_3px_0_0_theme(colors.ink.600)] hover:border-ink-500 hover:text-sand-100",
      )}>
      {children}
    </button>
  );
}

export default function SignupPage() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [org, setOrg] = useState<Org>({ name: "", legal_name: "", country: "IN", base_currency: "USD", industry: "", size_band: "", website: "" });
  const [admin, setAdmin] = useState<Admin>({ full_name: "", email: "", password: "", confirm: "" });
  const [errors, setErrors] = useState<Errors>({});
  const [agree, setAgree] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [passwordFocused, setPasswordFocused] = useState(false);
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const score = useMemo(() => passwordScore(admin.password), [admin.password]);
  const progress = done ? 100 : ((step + (step === 2 && agree ? 0.5 : 0)) / 3) * 100;

  function next() {
    const e = step === 0 ? validateOrg(org) : step === 1 ? validateAdmin(admin) : {};
    setErrors(e);
    if (Object.keys(e).length === 0) setStep((s) => s + 1);
  }

  async function submit() {
    setBusy(true);
    setServerError(null);
    try {
      await api.signup({
        organization: {
          name: org.name.trim(), legal_name: org.legal_name.trim() || null, country: org.country,
          base_currency: org.base_currency, industry: org.industry || null, size_band: org.size_band || null,
          website: org.website.trim() || null,
        },
        admin: { full_name: admin.full_name.trim(), email: admin.email.trim(), password: admin.password },
      });
      setDone(true);
      setTimeout(() => router.replace("/dashboard"), 2600);
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fields);
        setServerError(err.message);
        const badStep = Object.keys(err.fields).some((k) => STEP_FIELDS[0].includes(k)) ? 0
          : Object.keys(err.fields).some((k) => STEP_FIELDS[1].includes(k)) || err.status === 409 ? 1 : 2;
        setStep(badStep);
      } else {
        setServerError("Something went wrong. Try again.");
      }
    } finally {
      setBusy(false);
    }
  }

  const mood = done ? "silo-cheer" : passwordFocused && !showPassword ? "silo-shy" : step === 0 ? "silo-wave" : "silo-idle";
  const speech = done
    ? `Woohoo! ${org.name || "Your agency"} has its own PaySilo.`
    : passwordFocused && !showPassword ? "Eyes closed. Type away!"
      : step === 0 ? "Let's set up your agency. Each client you add gets its own silo."
        : step === 1 ? "Now, who's in charge? You'll be the owner."
          : "Looks great! One tap and you're in.";

  if (done) {
    return (
      <AuthShell mood="silo-cheer" speech={speech}>
        <div className="relative text-center">
          <LottieAnimation name="burst" loop={false} className="pointer-events-none absolute -top-16 left-1/2 size-96 -translate-x-1/2" />
          <div className="mx-auto grid size-20 place-items-center rounded-full bg-mint-500 text-ink-950 shadow-[0_5px_0_0_theme(colors.mint.700)]">
            <Check className="size-10" strokeWidth={3} />
          </div>
          <h1 className="mt-6 font-display text-4xl font-semibold text-sand-100">You&apos;re all set!</h1>
          <p className="mt-3 text-sage-300">Taking you to your dashboard…</p>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell mood={mood} speech={speech}
      footer={<>Already have an account? <Link href="/login" className="font-extrabold text-coral-400 hover:underline">Log in</Link></>}>
      <div className="flex items-center gap-3">
        <Link href="/" aria-label="Back to home" className="grid size-9 place-items-center rounded-full text-sage-400 hover:bg-ink-700 hover:text-sand-100">
          <X className="size-5" />
        </Link>
        <div className="h-4 flex-1 overflow-hidden rounded-full bg-ink-700" role="progressbar" aria-valuenow={Math.round(progress)} aria-valuemin={0} aria-valuemax={100}>
          <motion.div className="relative h-full rounded-full bg-coral-500" animate={{ width: `${Math.max(progress, 6)}%` }}
                      transition={{ type: "spring", stiffness: 160, damping: 22 }}>
            <span className="absolute inset-x-2 top-1 h-1 rounded-full bg-sand-50/35" />
          </motion.div>
        </div>
        <span className="text-sm font-extrabold text-sage-400">{step + 1}/3</span>
      </div>

      <AnimatePresence mode="wait">
        <motion.div key={step} initial={{ opacity: 0, x: 24 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -24 }}
                    transition={{ duration: 0.22 }} className="mt-8">
          {step === 0 && (
            <section aria-labelledby="org-title" className="grid gap-5">
              <div>
                <p className="inline-flex items-center gap-2 text-sm font-extrabold uppercase tracking-widest text-coral-400"><Building2 className="size-4" /> Organization</p>
                <h1 id="org-title" className="mt-2 font-display text-3xl font-semibold text-sand-100">Tell us about your agency</h1>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="org-name">Organization name</Label>
                <Input id="org-name" value={org.name} onChange={(e) => setOrg({ ...org, name: e.target.value })} placeholder="Vertex Payroll Services"
                       aria-invalid={!!errors["organization.name"]} autoFocus />
                <FieldError msg={errors["organization.name"]} />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="org-legal">Legal name <span className="font-semibold text-sage-500">(optional)</span></Label>
                <Input id="org-legal" value={org.legal_name} onChange={(e) => setOrg({ ...org, legal_name: e.target.value })} placeholder="Vertex Payroll Services Pvt Ltd" />
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="grid gap-2">
                  <Label htmlFor="org-country">Country</Label>
                  <NativeSelect id="org-country" value={org.country} onChange={(e) => setOrg({ ...org, country: e.target.value })}>
                    {COUNTRIES.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
                  </NativeSelect>
                  <FieldError msg={errors["organization.country"]} />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="org-ccy">Base currency</Label>
                  <NativeSelect id="org-ccy" value={org.base_currency} onChange={(e) => setOrg({ ...org, base_currency: e.target.value })}>
                    {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
                  </NativeSelect>
                </div>
              </div>
              <fieldset className="grid gap-2">
                <legend className="mb-2 text-sm font-bold text-sage-300">What best describes you?</legend>
                <div className="flex flex-wrap gap-2">
                  {INDUSTRIES.map((i) => <Tile key={i} selected={org.industry === i} onClick={() => setOrg({ ...org, industry: org.industry === i ? "" : i })}>{i}</Tile>)}
                </div>
              </fieldset>
              <fieldset className="grid gap-2">
                <legend className="mb-2 text-sm font-bold text-sage-300">Team size</legend>
                <div className="grid grid-cols-5 gap-2">
                  {SIZES.map((s) => <Tile key={s} selected={org.size_band === s} onClick={() => setOrg({ ...org, size_band: org.size_band === s ? "" : s })}>{s}</Tile>)}
                </div>
              </fieldset>
              <div className="grid gap-2">
                <Label htmlFor="org-web">Website <span className="font-semibold text-sage-500">(optional)</span></Label>
                <Input id="org-web" type="url" value={org.website} onChange={(e) => setOrg({ ...org, website: e.target.value })} placeholder="https://vertexpayroll.com"
                       aria-invalid={!!errors["organization.website"]} />
                <FieldError msg={errors["organization.website"]} />
              </div>
            </section>
          )}

          {step === 1 && (
            <section aria-labelledby="admin-title" className="grid gap-5">
              <div>
                <p className="inline-flex items-center gap-2 text-sm font-extrabold uppercase tracking-widest text-coral-400"><UserRound className="size-4" /> Owner account</p>
                <h1 id="admin-title" className="mt-2 font-display text-3xl font-semibold text-sand-100">Create your login</h1>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="full-name">Full name</Label>
                <Input id="full-name" autoComplete="name" value={admin.full_name} onChange={(e) => setAdmin({ ...admin, full_name: e.target.value })}
                       placeholder="Priya Raman" aria-invalid={!!errors["admin.full_name"]} autoFocus />
                <FieldError msg={errors["admin.full_name"]} />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="admin-email">Work email</Label>
                <Input id="admin-email" type="email" autoComplete="email" value={admin.email} onChange={(e) => setAdmin({ ...admin, email: e.target.value })}
                       placeholder="priya@vertexpayroll.com" aria-invalid={!!errors["admin.email"]} />
                <FieldError msg={errors["admin.email"]} />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="admin-password">Password</Label>
                <div className="relative">
                  <Input id="admin-password" type={showPassword ? "text" : "password"} autoComplete="new-password" className="pr-12"
                         value={admin.password} onChange={(e) => setAdmin({ ...admin, password: e.target.value })}
                         onFocus={() => setPasswordFocused(true)} onBlur={() => setPasswordFocused(false)}
                         aria-invalid={!!errors["admin.password"]} placeholder="At least 8 characters" />
                  <button type="button" onClick={() => setShowPassword((v) => !v)} aria-label={showPassword ? "Hide password" : "Show password"}
                          className="absolute right-2 top-1/2 grid size-9 -translate-y-1/2 place-items-center rounded-xl text-sage-400 hover:bg-ink-700 hover:text-sand-100">
                    {showPassword ? <EyeOff className="size-5" /> : <Eye className="size-5" />}
                  </button>
                </div>
                <div className="flex gap-1.5" aria-hidden>
                  {[0, 1, 2, 3].map((i) => (
                    <span key={i} className={cn("h-2 flex-1 rounded-full transition-colors",
                      i < score ? (score <= 1 ? "bg-coral-500" : score === 2 ? "bg-sand-400" : "bg-mint-500") : "bg-ink-700")} />
                  ))}
                </div>
                <FieldError msg={errors["admin.password"]} />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="admin-confirm">Confirm password</Label>
                <Input id="admin-confirm" type={showPassword ? "text" : "password"} autoComplete="new-password"
                       value={admin.confirm} onChange={(e) => setAdmin({ ...admin, confirm: e.target.value })}
                       onFocus={() => setPasswordFocused(true)} onBlur={() => setPasswordFocused(false)}
                       aria-invalid={!!errors["admin.confirm"]} />
                <FieldError msg={errors["admin.confirm"]} />
              </div>
            </section>
          )}

          {step === 2 && (
            <section aria-labelledby="review-title" className="grid gap-5">
              <h1 id="review-title" className="font-display text-3xl font-semibold text-sand-100">Ready to launch?</h1>
              <dl className="grid gap-3 rounded-3xl border-2 border-ink-700 bg-ink-850 p-5 text-sm">
                {[
                  ["Organization", org.name],
                  ["Legal name", org.legal_name || "—"],
                  ["Country", COUNTRIES.find(([c]) => c === org.country)?.[1] ?? org.country],
                  ["Base currency", org.base_currency],
                  ["Type", org.industry || "—"],
                  ["Team size", org.size_band || "—"],
                  ["Owner", `${admin.full_name} · ${admin.email}`],
                ].map(([k, v]) => (
                  <div key={k} className="flex justify-between gap-4">
                    <dt className="font-bold text-sage-400">{k}</dt>
                    <dd className="text-right font-bold text-sand-100">{v}</dd>
                  </div>
                ))}
              </dl>
              <label className="flex cursor-pointer items-start gap-3 rounded-2xl border-2 border-ink-700 p-4">
                <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)}
                       className="mt-0.5 size-5 accent-coral-500" />
                <span className="text-sm font-semibold text-sage-300">
                  I understand PaySilo keeps each employer&apos;s funds strictly separate, and that payouts need a signed approval.
                </span>
              </label>
            </section>
          )}
        </motion.div>
      </AnimatePresence>

      {serverError && (
        <p role="alert" className="mt-5 rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">{serverError}</p>
      )}

      <div className="mt-8 flex items-center justify-between gap-3">
        {step > 0 ? (
          <Button type="button" variant="outline" onClick={() => { setErrors({}); setStep((s) => s - 1); }}>
            <ArrowLeft /> Back
          </Button>
        ) : <span />}
        {step < 2 ? (
          <Button type="button" onClick={next}>Continue <ArrowRight /></Button>
        ) : (
          <Button type="button" variant="primary" disabled={!agree || busy} onClick={() => void submit()}>
            {busy ? <Loader2 className="animate-spin" /> : <Check />} Create organization
          </Button>
        )}
      </div>
    </AuthShell>
  );
}
