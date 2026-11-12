"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, ArrowRight, Check, Loader2, ShieldCheck } from "lucide-react";

import { Logo } from "@/components/brand/logo";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiTrust, ApiError, type PayoutOption, type PublicOnboarding } from "@/lib/api";
import { cn } from "@/lib/utils";

const STEPS = ["Welcome", "Payout", "Tax", "Review"] as const;

const FIELD_LABEL: Record<string, string> = {
  iban: "IBAN", swift_code: "SWIFT / BIC code", account_number: "Account number", routing_code: "Routing number",
};
const FIELD_HELP: Record<string, Record<string, string>> = {
  USD_ACH: { account_number: "4 to 17 digits, from your US bank statement.", routing_code: "The 9-digit ABA routing number." },
  GBP_FPS: { account_number: "Your 8-digit UK account number.", routing_code: "Your 6-digit sort code (e.g. 20-00-00)." },
  EUR_SWIFT: { iban: "Starts with your country code, e.g. DE89 3704 ...", swift_code: "8 or 11 characters, e.g. COBADEFFXXX." },
  EUR_SEPA: { iban: "Starts with your country code, e.g. NL91 ABNA ..." },
};
const ROUTING_LABEL: Record<string, string> = { GBP_FPS: "Sort code" };

const mask = (v: string) => (v.replace(/\s/g, "").length > 4 ? `•••• ${v.replace(/\s/g, "").slice(-4)}` : "••••");

export default function OnboardPage() {
  const { token } = useParams<{ token: string }>();
  const [info, setInfo] = useState<PublicOnboarding | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "invalid" | "busy">("loading");
  const [step, setStep] = useState(0);
  const [done, setDone] = useState<string | null>(null);
  const [optionKey, setOptionKey] = useState("");
  const [country, setCountry] = useState("");
  const [vals, setVals] = useState<Record<string, string>>({});
  const [taxStatus, setTaxStatus] = useState<"US_PERSON" | "NON_US" | "">("");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    apiTrust.onboardingInfo(token)
      .then((i) => { setInfo(i); setCountry(i.country); setState("ready"); })
      .catch((e) => setState(e instanceof ApiError && e.status === 429 ? "busy" : "invalid"));
  }, [token]);

  const option: PayoutOption | undefined = info?.payout_options.find((o) => o.key === optionKey);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) => setVals((v) => ({ ...v, [k]: e.target.value }));

  function pick(o: PayoutOption) {
    setOptionKey(o.key);
    setError(null);
    if (o.currency === "USD") setCountry("US");
    else if (o.currency === "GBP") setCountry("GB");
    else if (info && !["US", "GB"].includes(info.country)) setCountry(info.country);
    else setCountry("");
  }

  function payoutProblem(): string | null {
    if (!option) return "Choose how you would like to be paid.";
    if (country.trim().length !== 2) return "Enter your bank's country as a 2-letter code, e.g. DE.";
    for (const f of option.fields) {
      if (f === "swift_code" && option.key !== "EUR_SWIFT") continue;
      if (!(vals[f] ?? "").trim()) return `Enter your ${(f === "routing_code" ? ROUTING_LABEL[option.key] ?? FIELD_LABEL[f] : FIELD_LABEL[f]).toLowerCase()}.`;
    }
    return null;
  }

  function next() {
    setError(null);
    if (step === 1) {
      const p = payoutProblem();
      if (p) return setError(p);
    }
    if (step === 2 && !taxStatus) return setError("Please choose one option.");
    setStep((s) => s + 1);
  }

  async function submit() {
    if (!option || !taxStatus) return;
    setSubmitting(true);
    setError(null);
    try {
      const used = (f: string) => (option.fields.includes(f) ? (vals[f] ?? "").trim() || null : null);
      const res = await apiTrust.onboardingSubmit(token, {
        option: option.key, country: country.trim().toUpperCase(), iban: used("iban"), swift_code: used("swift_code"),
        account_number: used("account_number"), routing_code: used("routing_code"), tax_status: taxStatus, confirmed_accurate: confirmed,
      });
      setDone(res.message);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setState("invalid");
      else if (e instanceof ApiError && e.status === 429) setError("Too many attempts. Please wait a few minutes and try again.");
      else setError(e instanceof ApiError ? e.message : "Something went wrong. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="min-h-dvh bg-ink-950 px-4 py-8 sm:py-12">
      <div className="mx-auto grid w-full max-w-xl gap-6">
        <div className="flex justify-center"><Logo href="/" /></div>

        {state === "loading" && (
          <Card className="grid place-items-center p-10 text-sage-300"><Loader2 className="size-6 animate-spin" /><p className="mt-3 font-semibold">Loading…</p></Card>
        )}

        {state === "busy" && (
          <Card className="p-6 sm:p-8">
            <h1 className="font-display text-2xl font-semibold text-sand-100">Please try again in a few minutes</h1>
            <p className="mt-2 text-sage-300">There have been too many attempts from this connection.</p>
          </Card>
        )}

        {state === "invalid" && (
          <Card className="p-6 sm:p-8">
            <h1 className="font-display text-2xl font-semibold text-sand-100">This link is not valid any more</h1>
            <p className="mt-2 text-sage-300">It may have expired, already been used, or been replaced. Ask your agency to send you a new link.</p>
          </Card>
        )}

        {state === "ready" && info && done && (
          <Card className="p-6 text-center sm:p-8">
            <span className="mx-auto grid size-14 place-items-center rounded-full border-2 border-mint-500/60 bg-mint-500/10 text-mint-400"><Check className="size-7" /></span>
            <h1 className="mt-4 font-display text-2xl font-semibold text-sand-100">All set, {info.first_name}</h1>
            <p className="mt-2 text-sage-300">{done}</p>
            <p className="mt-4 text-sm font-semibold text-sage-500">You can close this page. You will not need this link again.</p>
          </Card>
        )}

        {state === "ready" && info && !done && (
          <Card className="p-6 sm:p-8">
            <ol className="mb-6 flex items-center gap-2" aria-label="Progress">
              {STEPS.map((label, i) => (
                <li key={label} className="flex flex-1 flex-col gap-1.5" aria-current={i === step ? "step" : undefined}>
                  <span className={cn("h-1.5 rounded-full", i <= step ? "bg-coral-500" : "bg-ink-700")} />
                  <span className={cn("text-[11px] font-extrabold uppercase tracking-widest", i === step ? "text-sand-100" : "text-sage-500")}>{label}</span>
                </li>
              ))}
            </ol>

            {error && <p role="alert" className="mb-4 rounded-2xl border-2 border-coral-700/70 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">{error}</p>}

            {step === 0 && (
              <div className="grid gap-4">
                <h1 className="font-display text-2xl font-semibold text-sand-100 sm:text-3xl">Hi {info.first_name}, let&apos;s get you set up to be paid</h1>
                <p className="text-sage-300">
                  <strong className="text-sand-100">{info.agency_name}</strong> pays you on behalf of <strong className="text-sand-100">{info.employer_name}</strong>.
                  This takes about two minutes: your payout details, then a quick tax question.
                </p>
                <p className="flex items-start gap-2 rounded-2xl bg-ink-950/50 p-4 text-sm font-semibold text-sage-300">
                  <ShieldCheck className="mt-0.5 size-5 shrink-0 text-mint-400" />
                  Your agency will check these details with you before your next payment, so a typo or a mix-up can&apos;t send money to the wrong place.
                </p>
                <div><Button onClick={next}>Get started <ArrowRight /></Button></div>
              </div>
            )}

            {step === 1 && (
              <div className="grid gap-5">
                <div>
                  <h2 className="font-display text-2xl font-semibold text-sand-100">How would you like to be paid?</h2>
                  <p className="mt-1 text-sm text-sage-300">Pick the account you want {info.agency_name} to pay into.</p>
                </div>
                <div role="radiogroup" aria-label="Payout method" className="grid gap-2">
                  {info.payout_options.map((o) => (
                    <button key={o.key} type="button" role="radio" aria-checked={o.key === optionKey} onClick={() => pick(o)}
                      className={cn("rounded-2xl border-2 px-4 py-3 text-left font-bold transition-colors",
                        o.key === optionKey ? "border-coral-500 bg-coral-500/10 text-sand-100" : "border-ink-600 bg-ink-900/60 text-sage-300 hover:bg-ink-800")}>
                      {o.label}
                    </button>
                  ))}
                </div>
                {option && (
                  <div className="grid gap-4">
                    {option.fields.map((f) => (
                      <div key={f}>
                        <Label htmlFor={`o-${f}`}>{f === "routing_code" ? ROUTING_LABEL[option.key] ?? FIELD_LABEL[f] : FIELD_LABEL[f]}</Label>
                        <Input id={`o-${f}`} value={vals[f] ?? ""} onChange={set(f)} autoComplete="off" inputMode={f === "account_number" || f === "routing_code" ? "numeric" : "text"} className="mt-1.5" />
                        {FIELD_HELP[option.key]?.[f] && <p className="mt-1 text-xs font-semibold text-sage-500">{FIELD_HELP[option.key][f]}</p>}
                      </div>
                    ))}
                    {option.key === "EUR_SEPA" && (
                      <div>
                        <Label htmlFor="o-swift_code">SWIFT / BIC code (optional)</Label>
                        <Input id="o-swift_code" value={vals.swift_code ?? ""} onChange={set("swift_code")} autoComplete="off" className="mt-1.5" />
                      </div>
                    )}
                    {(option.currency === "EUR") && (
                      <div>
                        <Label htmlFor="o-country">Bank country (2-letter code)</Label>
                        <Input id="o-country" value={country} onChange={(e) => setCountry(e.target.value)} maxLength={2} placeholder="DE" className="mt-1.5 w-28" />
                      </div>
                    )}
                  </div>
                )}
                <div className="flex flex-wrap gap-3">
                  <Button variant="ghost" onClick={() => setStep(0)}><ArrowLeft /> Back</Button>
                  <Button onClick={next}>Continue <ArrowRight /></Button>
                </div>
              </div>
            )}

            {step === 2 && (
              <div className="grid gap-5">
                <div>
                  <h2 className="font-display text-2xl font-semibold text-sand-100">A quick tax question</h2>
                  <p className="mt-1 text-sm text-sage-300">Are you a US person for tax purposes? (A US citizen, US resident or US-based business.)</p>
                </div>
                <div role="radiogroup" aria-label="Tax status" className="grid gap-2">
                  {([["US_PERSON", "Yes, I am a US person"], ["NON_US", "No, I am not a US person"]] as const).map(([v, label]) => (
                    <button key={v} type="button" role="radio" aria-checked={taxStatus === v} onClick={() => setTaxStatus(v)}
                      className={cn("rounded-2xl border-2 px-4 py-3 text-left font-bold transition-colors",
                        taxStatus === v ? "border-coral-500 bg-coral-500/10 text-sand-100" : "border-ink-600 bg-ink-900/60 text-sage-300 hover:bg-ink-800")}>
                      {label}
                    </button>
                  ))}
                </div>
                <p className="rounded-2xl bg-ink-950/50 p-4 text-sm font-semibold text-sage-300">
                  Please do not enter any tax ID here. {info.agency_name} will contact you separately to request the right tax form
                  {taxStatus === "US_PERSON" ? " (usually a W-9)" : taxStatus === "NON_US" ? " (usually a W-8BEN)" : ""}. This is not tax advice.
                </p>
                <div className="flex flex-wrap gap-3">
                  <Button variant="ghost" onClick={() => setStep(1)}><ArrowLeft /> Back</Button>
                  <Button onClick={next}>Continue <ArrowRight /></Button>
                </div>
              </div>
            )}

            {step === 3 && option && (
              <div className="grid gap-5">
                <div>
                  <h2 className="font-display text-2xl font-semibold text-sand-100">Check and send</h2>
                  <p className="mt-1 text-sm text-sage-300">Make sure everything looks right. Typos are the most common reason payments fail.</p>
                </div>
                <dl className="grid gap-2 rounded-2xl bg-ink-950/50 p-4 text-sm">
                  <div className="flex justify-between gap-3"><dt className="font-bold text-sage-400">Name</dt><dd className="font-semibold text-sand-100">{info.contractor_name}</dd></div>
                  <div className="flex justify-between gap-3"><dt className="font-bold text-sage-400">Pay into</dt><dd className="text-right font-semibold text-sand-100">{option.label}</dd></div>
                  {option.fields.map((f) => (
                    <div key={f} className="flex justify-between gap-3">
                      <dt className="font-bold text-sage-400">{f === "routing_code" ? ROUTING_LABEL[option.key] ?? FIELD_LABEL[f] : FIELD_LABEL[f]}</dt>
                      <dd className="font-semibold tabular-nums text-sand-100">{mask(vals[f] ?? "")}</dd>
                    </div>
                  ))}
                  <div className="flex justify-between gap-3"><dt className="font-bold text-sage-400">Bank country</dt><dd className="font-semibold text-sand-100">{country.toUpperCase()}</dd></div>
                  <div className="flex justify-between gap-3"><dt className="font-bold text-sage-400">US person</dt><dd className="font-semibold text-sand-100">{taxStatus === "US_PERSON" ? "Yes" : "No"}</dd></div>
                </dl>
                <p className="text-xs font-semibold text-sage-500">For your safety only the last 4 characters are shown here.</p>
                <label className="flex cursor-pointer items-start gap-3 rounded-2xl border-2 border-ink-600 bg-ink-900/60 p-4">
                  <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} className="mt-1 size-5 accent-coral-500" />
                  <span className="text-sm font-semibold text-sand-100">These details are mine and they are correct.</span>
                </label>
                <div className="flex flex-wrap gap-3">
                  <Button variant="ghost" onClick={() => setStep(1)}><ArrowLeft /> Edit</Button>
                  <Button variant="success" disabled={!confirmed || submitting} onClick={() => void submit()}>
                    {submitting ? <Loader2 className="animate-spin" /> : <Check />} Send my details
                  </Button>
                </div>
              </div>
            )}
          </Card>
        )}
      </div>
    </main>
  );
}
