"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, ArrowRight, CalendarClock, History, Link2, Loader2, Pencil, Play, Plus, ShieldCheck, UserX } from "lucide-react";

import { AppBar } from "@/components/dashboard/app-bar";
import { PayrollScheduleDialog } from "@/components/workflows/payroll-schedule-dialog";
import { PageError, PageNotice, SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { Badge, toneForStatus } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/select";
import {
  EditContractorPanel, HistoryPanel, OnboardingLinkPanel, TrustBadges, VerifyBankDialog,
} from "@/components/contractors/trust-panels";
import {
  api, apiTrust, ApiError,
  type Contractor, type ContractorIn, type EmployerDetail, type RunSummary, type Session, type SettlementMode,
} from "@/lib/api";
import { AnimatedMoney, Stagger } from "@/lib/gsap-anim";
import { useDataChanged } from "@/lib/events";
import { runStatusInfo, EDITOR_ROLES, DECIDER_ROLES } from "@/lib/status";
import { formatMoney } from "@/lib/utils";

const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

interface FundingEvent { amount: string; currency: string; at: Date }

function FieldError({ errors, name }: { errors: Record<string, string>; name: string }) {
  const msg = errors[name];
  return msg ? <p className="mt-1 text-sm font-bold text-coral-300">{msg}</p> : null;
}

const MODE_OPTIONS: { value: SettlementMode; title: string; body: string }[] = [
  {
    value: "ALL_OR_NOTHING",
    title: "All or nothing",
    body: "If the wallet cannot cover the whole batch (plus fees, the USD 149 run fee and the USD 1,000 reserve), nobody is paid and the run is held until it is funded.",
  },
  {
    value: "PARTIAL",
    title: "Partial",
    body: "Pay what fits, lowest priority number first, then smallest first, and hold the rest.",
  },
];

function SettlementModeCard({
  mode, canEdit, saving, onChange,
}: { mode: SettlementMode; canEdit: boolean; saving: boolean; onChange: (m: SettlementMode) => void }) {
  return (
    <Card className="grid gap-3 p-4">
      <div>
        <p className="font-display text-lg font-semibold text-sand-100">If the wallet is short</p>
        <p className="text-sm text-sage-400">
          {canEdit ? "Choose what happens when this silo cannot cover a payroll batch. It applies to runs started from now on." : "Owners and admins can change this. It applies to runs started from now on."}
        </p>
      </div>
      <div role="radiogroup" aria-label="If the wallet is short" className="grid gap-3 sm:grid-cols-2">
        {MODE_OPTIONS.map((o) => {
          const selected = mode === o.value;
          return (
            <div key={o.value} className={`grid content-start gap-2 rounded-2xl border-2 p-3 ${selected ? "border-sage-500 bg-sage-500/10" : "border-ink-600 bg-ink-900/40"}`}>
              {canEdit ? (
                <Button
                  type="button" role="radio" aria-checked={selected} size="sm"
                  variant={selected ? "sage" : "outline"} className="w-full sm:w-fit"
                  disabled={saving} onClick={() => { if (!selected) onChange(o.value); }}
                >
                  {o.title}
                </Button>
              ) : (
                <p className="font-extrabold text-sand-100">{o.title}{selected ? <span className="ml-2 text-xs font-bold uppercase text-sage-400">Current</span> : null}</p>
              )}
              <p className="text-sm font-semibold text-sage-300">{o.body}</p>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

export default function EmployerPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [employer, setEmployer] = useState<EmployerDetail | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [fundingLog, setFundingLog] = useState<FundingEvent[]>([]);
  const [fundAmount, setFundAmount] = useState("");
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [funding, setFunding] = useState(false);
  const [starting, setStarting] = useState(false);
  const canEdit = !!session && EDITOR_ROLES.includes(session.role);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [me, emp] = await Promise.all([api.me(), api.employer(id)]);
      setSession(me);
      setEmployer(emp);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace(`/login?next=/dashboard/employers/${id}`);
        return;
      }
      setLoadError(errMsg(err));
    }
    setLoading(false);
  }, [id, router]);

  const loadRuns = useCallback(async () => {
    setRunsError(null);
    try {
      setRuns(await api.runs(id));
    } catch (err) {
      setRunsError(errMsg(err));
    }
  }, [id]);

  useEffect(() => {
    void load();
    void loadRuns();
  }, [load, loadRuns]);

  // A decision made elsewhere (e.g. the chat panel) refreshes balances and run history.
  useDataChanged(() => {
    void load();
    void loadRuns();
  });

  async function fund(ev: React.FormEvent) {
    ev.preventDefault();
    if (!employer || !(Number(fundAmount) > 0)) return;
    setFunding(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.fund(employer.id, fundAmount);
      setEmployer((prev) => (prev ? { ...prev, balances: updated.balances } : prev));
      setFundingLog((log) => [{ amount: fundAmount, currency: employer.funding_currency, at: new Date() }, ...log]);
      setNotice(`Added ${formatMoney(fundAmount, employer.funding_currency)} to ${employer.name}.`);
      setFundAmount("");
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setFunding(false);
    }
  }

  const [savingMode, setSavingMode] = useState(false);
  async function changeMode(mode: SettlementMode) {
    if (!employer) return;
    setSavingMode(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.setSettlementMode(employer.id, mode);
      setEmployer((prev) => (prev ? { ...prev, settlement_mode: updated.settlement_mode } : prev));
      setNotice(updated.settlement_mode === "PARTIAL"
        ? "Short wallet: PaySilo will now pay what fits and hold the rest."
        : "Short wallet: PaySilo will now pay nobody and hold the run until it is funded.");
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setSavingMode(false);
    }
  }

  async function startRun() {
    if (!employer) return;
    const active = employer.contractors.filter((c) => c.is_active);
    if (active.length === 0 || !active.some((c) => c.default_amount)) {
      setNotice("Add at least one active contractor with a default amount before running payroll.");
      return;
    }
    setStarting(true);
    setError(null);
    setNotice(null);
    try {
      const run = await api.startRun(employer.id, active.filter((c) => c.default_amount).map((c) => ({ contractor_id: c.id })));
      router.push(`/dashboard/runs/${run.id}`);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setStarting(false);
    }
  }

  async function onContractorAdded(c: Contractor) {
    setEmployer((prev) => (prev ? { ...prev, contractors: [...prev.contractors, c], contractor_count: prev.contractor_count + 1 } : prev));
    setNotice(`${c.full_name} added to ${employer?.name ?? "this silo"}.`);
  }

  async function deactivate(c: Contractor) {
    if (!employer) return;
    setError(null);
    try {
      await api.deactivateContractor(employer.id, c.id);
      setEmployer((prev) =>
        prev
          ? {
              ...prev,
              contractors: prev.contractors.map((x) => (x.id === c.id ? { ...x, is_active: false } : x)),
              contractor_count: Math.max(0, prev.contractor_count - 1),
            }
          : prev,
      );
      setNotice(`${c.full_name} deactivated. Past runs are unchanged.`);
    } catch (err) {
      setError(errMsg(err));
    }
  }

  const balances = employer ? Object.entries(employer.balances) : [];

  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}

      <main className="mx-auto mt-8 grid max-w-6xl gap-8 px-4 sm:px-6">
        <div>
          <Link href="/dashboard" className="inline-flex items-center gap-2 font-bold text-sage-300 hover:text-sand-100">
            <ArrowLeft className="size-4" /> All employers
          </Link>
        </div>

        {loading && <SectionLoading label="Loading this silo…" />}
        {loadError && <SectionError message={loadError} onRetry={() => void load()} />}
        {error && <PageError message={error} />}
        {notice && <PageNotice message={notice} />}

        {employer && (
          <>
            <section className="grid gap-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">{employer.name}</h1>
                  <p className="mt-1 text-sage-300">
                    {employer.legal_name ? `${employer.legal_name} · ` : ""}
                    {employer.country}
                    {employer.contact_name ? ` · ${employer.contact_name}` : ""}
                    {employer.contact_email ? ` · ${employer.contact_email}` : ""}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="sand">{employer.settlement_mode === "PARTIAL" ? "Partial payout" : "All or nothing"}</Badge>
                  <Badge tone={toneForStatus(employer.status)}>{employer.status}</Badge>
                </div>
              </div>

              <SettlementModeCard mode={employer.settlement_mode} canEdit={canEdit} saving={savingMode} onChange={(m) => void changeMode(m)} />

              <Card className="flex flex-wrap items-center justify-between gap-3 p-4">
                <div className="min-w-0">
                  <p className="text-sm font-bold text-sage-400">Connected account</p>
                  {employer.airwallex_account_id ? (
                    <p className="truncate font-semibold text-sand-100">
                      Connected <span className="text-sage-400">· account …{employer.airwallex_account_id.slice(-6)}</span>
                    </p>
                  ) : (
                    <p className="font-semibold text-sand-200">Not connected yet — PaySilo provisions it when the silo is created.</p>
                  )}
                </div>
                <Badge tone={employer.airwallex_account_id ? "success" : "warning"}>
                  {employer.airwallex_account_id ? "Connected" : "Pending"}
                </Badge>
              </Card>

              <Stagger className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {balances.length === 0 && (
                  <Card className="p-5">
                    <p className="text-sm font-bold text-sage-400">Balance</p>
                    <p className="mt-1 font-display text-2xl font-semibold text-sand-100">No funds yet</p>
                    <p className="mt-1 text-sm text-sage-400">Add funds below to start paying contractors.</p>
                  </Card>
                )}
                {balances.map(([currency, amount]) => (
                  <Card key={currency} className="p-5">
                    <p className="text-sm font-bold text-sage-400">{currency} balance · held in this silo only</p>
                    <AnimatedMoney value={amount} currency={currency} className="mt-1 block font-display text-3xl font-semibold tabular-nums text-sand-100" />
                  </Card>
                ))}
                <Card className="p-5">
                  <p className="text-sm font-bold text-sage-400">Contractors</p>
                  <p className="mt-1 font-display text-3xl font-semibold tabular-nums text-sand-100">{employer.contractor_count}</p>
                  <p className="mt-1 text-sm text-sage-400">Active on this silo</p>
                </Card>
              </Stagger>

              {canEdit && (
                <div className="flex flex-wrap items-center gap-3">
                  <Button onClick={() => void startRun()} disabled={starting || employer.contractor_count === 0}>
                    {starting ? <Loader2 className="animate-spin" /> : <Play />} Run payroll
                  </Button>
                  <Button variant="outline" onClick={() => setScheduleOpen(true)}>
                    <CalendarClock /> Create payroll schedule
                  </Button>
                  <form onSubmit={(e) => void fund(e)} className="flex flex-wrap gap-2">
                    <Input
                      type="number" min="0" step="0.01" inputMode="decimal" value={fundAmount}
                      onChange={(e) => setFundAmount(e.target.value)}
                      aria-label={`Amount to add (${employer.funding_currency})`}
                      placeholder={`Add funds (${employer.funding_currency})`} className="w-44"
                    />
                    <Button type="submit" variant="sage" disabled={!(Number(fundAmount) > 0) || funding}>
                      {funding ? <Loader2 className="animate-spin" /> : <Plus />} Add funds
                    </Button>
                  </form>
                </div>
              )}
            </section>

            <PayrollScheduleDialog open={scheduleOpen} onClose={() => setScheduleOpen(false)}
              employers={[{ id: employer.id, name: employer.name }]} employerId={employer.id} />

            <ContractorsSection
              employer={employer} canEdit={canEdit} session={session}
              onAdded={(c) => void onContractorAdded(c)} onDeactivate={(c) => void deactivate(c)}
              onUpdated={(c, note) => {
                setEmployer((prev) => (prev ? { ...prev, contractors: prev.contractors.map((x) => (x.id === c.id ? c : x)) } : prev));
                setNotice(note);
              }}
            />

            <section>
              <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">Run history</h2>
              {runs === null && !runsError && <SectionLoading label="Loading runs…" />}
              {runsError && <SectionError message={runsError} onRetry={() => void loadRuns()} />}
              {runs !== null && runs.length === 0 && (
                <SectionEmpty title="No runs yet" body="Runs for this employer will appear here. Start one with Run payroll above." />
              )}
              {runs !== null && runs.length > 0 && (
                <div className="overflow-hidden rounded-4xl border-2 border-ink-700">
                  <div className="hidden grid-cols-[1fr_auto_auto_auto] gap-4 bg-ink-900/70 px-5 py-3 text-xs font-extrabold uppercase tracking-widest text-sage-500 md:grid">
                    <span>Run</span><span>Status</span><span className="text-right">Required</span><span />
                  </div>
                  <ul className="divide-y-2 divide-ink-700">
                    {runs.map((r) => (
                      <li key={r.id}>
                        <Link
                          href={`/dashboard/runs/${r.id}`}
                          className="grid gap-2 bg-ink-850/80 px-5 py-4 transition-colors hover:bg-ink-800 md:grid-cols-[1fr_auto_auto_auto] md:items-center md:gap-4"
                        >
                          <span className="min-w-0">
                            <span className="flex items-center gap-2 font-bold text-sand-100">
                              <span className="truncate">{r.period_label ?? "Payroll run"}</span>
                              {r.parent_run_id && <Badge tone="sage" className="px-2 py-0 text-[10px]">Continuation</Badge>}
                            </span>
                            <span className="block text-xs font-semibold text-sage-500">{new Date(r.created_at).toLocaleString()}</span>
                          </span>
                          <Badge tone={runStatusInfo(r).tone}>{runStatusInfo(r).label}</Badge>
                          <span className="font-display font-semibold tabular-nums text-sand-100 md:text-right">
                            {r.required_usd ? formatMoney(r.required_usd) : "—"}
                          </span>
                          <ArrowRight className="hidden size-5 text-sage-400 md:block" />
                        </Link>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>

            <section className="grid grid-cols-1 gap-6 lg:grid-cols-2 lg:items-start">
              <div className="min-w-0">
                <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">Ledger position</h2>
                <Card className="p-5">
                  <p className="text-sm text-sage-300">
                    Balances above are derived from this silo&apos;s own ledger — money here can never be spent on another employer.
                  </p>
                  {balances.length === 0 ? (
                    <p className="mt-3 text-sm font-semibold text-sage-500">No ledger entries yet for this silo.</p>
                  ) : (
                    <ul className="mt-3 grid grid-cols-1 gap-2">
                      {balances.map(([currency, amount]) => (
                        <li key={currency} className="flex items-baseline justify-between gap-3 rounded-2xl bg-ink-950/40 px-4 py-3">
                          <span className="min-w-0 truncate text-sm font-bold text-sage-300">{employer.name} · cash · {currency}</span>
                          <span className="shrink-0 font-display font-semibold tabular-nums text-sand-100">{formatMoney(amount, currency)}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <p className="mt-3 text-xs font-semibold text-sage-500">
                    A per-entry ledger statement is not exposed by the API yet, so individual entries cannot be listed here.
                  </p>
                </Card>
              </div>

              <div>
                <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">Funding history</h2>
                {fundingLog.length === 0 ? (
                  <SectionEmpty
                    title="No funding recorded in this visit"
                    body="Funding events from this page will appear here. A full funding history is not exposed by the API yet."
                    lottie={false}
                  />
                ) : (
                  <Card className="p-5">
                    <ul className="grid grid-cols-1 gap-2">
                      {fundingLog.map((f, i) => (
                        <li key={i} className="flex items-baseline justify-between gap-3 rounded-2xl bg-ink-950/40 px-4 py-3">
                          <span className="text-sm font-bold text-sage-300">Deposit · {f.at.toLocaleTimeString()}</span>
                          <span className="font-display font-semibold tabular-nums text-mint-400">+{formatMoney(f.amount, f.currency)}</span>
                        </li>
                      ))}
                    </ul>
                  </Card>
                )}
              </div>
            </section>
          </>
        )}
      </main>
    </div>
  );
}

type RowPanel = { id: string; kind: "edit" | "link" | "history" };

function ContractorsSection({
  employer, canEdit, session, onAdded, onDeactivate, onUpdated,
}: {
  employer: EmployerDetail; canEdit: boolean; session: Session | null;
  onAdded: (c: Contractor) => void; onDeactivate: (c: Contractor) => void;
  onUpdated: (c: Contractor, note: string) => void;
}) {
  const [panel, setPanel] = useState<RowPanel | null>(null);
  const [verifying, setVerifying] = useState<Contractor | null>(null);
  const [watch, setWatch] = useState<Set<string>>(new Set());
  const canVerify = !!session && DECIDER_ROLES.includes(session.role);
  useEffect(() => {
    apiTrust.complianceOverview()
      .then((o) => setWatch(new Set(o.watch_1099.map((w) => w.contractor_id))))
      .catch(() => undefined); // badges are a nicety: the list still works without them
  }, [employer.contractors]);
  const toggle = (id: string, kind: RowPanel["kind"]) => setPanel((p) => (p && p.id === id && p.kind === kind ? null : { id, kind }));
  const [showForm, setShowForm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [form, setForm] = useState({
    full_name: "", email: "", country: "", currency: "USD", payout_method: "SWIFT" as "SWIFT" | "LOCAL",
    iban: "", swift_code: "", account_number: "", routing_code: "", default_amount: "",
  });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setSaving(true);
    setFieldErrors({});
    setFormError(null);
    const body: ContractorIn = {
      full_name: form.full_name.trim(),
      email: form.email.trim() || null,
      country: form.country.trim().toUpperCase(),
      currency: form.currency.trim().toUpperCase(),
      payout_method: form.payout_method,
      iban: form.iban.trim() || null,
      swift_code: form.swift_code.trim() || null,
      account_number: form.account_number.trim() || null,
      routing_code: form.routing_code.trim() || null,
      default_amount: form.default_amount.trim() || null,
    };
    try {
      const created = await api.addContractor(employer.id, body);
      onAdded(created);
      setShowForm(false);
      setForm({ full_name: "", email: "", country: "", currency: "USD", payout_method: "SWIFT", iban: "", swift_code: "", account_number: "", routing_code: "", default_amount: "" });
    } catch (err) {
      if (err instanceof ApiError) {
        setFieldErrors(err.fields);
        setFormError(err.message);
      } else {
        setFormError("Could not add the contractor. Try again.");
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <section id="contractors">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-display text-2xl font-semibold text-sand-100">Contractors</h2>
        {canEdit && !showForm && (
          <Button variant="outline" size="sm" onClick={() => setShowForm(true)}>
            <Plus /> Add contractor
          </Button>
        )}
      </div>
      {!canEdit && (
        <p className="mb-4 rounded-2xl border-2 border-ink-700 bg-ink-900/50 px-4 py-3 text-sm font-semibold text-sage-300">
          Your role is read-only here. Ask an owner or admin to add or deactivate contractors.
        </p>
      )}

      {canEdit && showForm && (
        <Card className="mb-4 p-5">
          <form onSubmit={(e) => void submit(e)} className="grid gap-4" noValidate>
            {formError && <PageError message={formError} />}
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="c-name">Full name</Label>
                <Input id="c-name" value={form.full_name} onChange={set("full_name")} required className="mt-1.5" aria-invalid={!!fieldErrors.full_name} />
                <FieldError errors={fieldErrors} name="full_name" />
              </div>
              <div>
                <Label htmlFor="c-email">Email (optional)</Label>
                <Input id="c-email" type="email" value={form.email} onChange={set("email")} className="mt-1.5" aria-invalid={!!fieldErrors.email} />
                <FieldError errors={fieldErrors} name="email" />
              </div>
              <div>
                <Label htmlFor="c-country">Country (2-letter code)</Label>
                <Input id="c-country" value={form.country} onChange={set("country")} placeholder="IN" maxLength={2} required className="mt-1.5" aria-invalid={!!fieldErrors.country} />
                <FieldError errors={fieldErrors} name="country" />
              </div>
              <div>
                <Label htmlFor="c-currency">Pay currency</Label>
                <Input id="c-currency" value={form.currency} onChange={set("currency")} maxLength={3} required className="mt-1.5" aria-invalid={!!fieldErrors.currency} />
                <FieldError errors={fieldErrors} name="currency" />
              </div>
              <div>
                <Label htmlFor="c-method">Payout method</Label>
                <NativeSelect id="c-method" value={form.payout_method} onChange={set("payout_method")} className="mt-1.5">
                  <option value="SWIFT">SWIFT</option>
                  <option value="LOCAL">Local</option>
                </NativeSelect>
              </div>
              <div>
                <Label htmlFor="c-amount">Default amount (optional)</Label>
                <Input id="c-amount" type="number" min="0" step="0.01" inputMode="decimal" value={form.default_amount} onChange={set("default_amount")} className="mt-1.5" aria-invalid={!!fieldErrors.default_amount} />
                <FieldError errors={fieldErrors} name="default_amount" />
              </div>
              <div>
                <Label htmlFor="c-iban">IBAN</Label>
                <Input id="c-iban" value={form.iban} onChange={set("iban")} className="mt-1.5" aria-invalid={!!fieldErrors.iban} />
                <FieldError errors={fieldErrors} name="iban" />
              </div>
              <div>
                <Label htmlFor="c-swift">SWIFT / BIC code</Label>
                <Input id="c-swift" value={form.swift_code} onChange={set("swift_code")} className="mt-1.5" aria-invalid={!!fieldErrors.swift_code} />
                <FieldError errors={fieldErrors} name="swift_code" />
              </div>
              <div>
                <Label htmlFor="c-account">Account number (if no IBAN)</Label>
                <Input id="c-account" value={form.account_number} onChange={set("account_number")} className="mt-1.5" aria-invalid={!!fieldErrors.account_number} />
                <FieldError errors={fieldErrors} name="account_number" />
              </div>
              <div>
                <Label htmlFor="c-routing">Routing code (optional)</Label>
                <Input id="c-routing" value={form.routing_code} onChange={set("routing_code")} className="mt-1.5" />
              </div>
            </div>
            <p className="text-sm text-sage-400">Provide an IBAN or an account number. SWIFT payouts also need a SWIFT/BIC code.</p>
            <div className="flex flex-wrap gap-3">
              <Button type="submit" variant="sage" disabled={saving}>
                {saving ? <Loader2 className="animate-spin" /> : <Plus />} Add contractor
              </Button>
              <Button type="button" variant="ghost" onClick={() => setShowForm(false)}>Cancel</Button>
            </div>
          </form>
        </Card>
      )}

      {employer.contractors.length === 0 ? (
        <SectionEmpty title="No contractors yet" body="Add the people this employer pays. Each contractor belongs to this silo only." />
      ) : (
        <div className="overflow-hidden rounded-4xl border-2 border-ink-700">
          <div className="hidden grid-cols-[1.4fr_1fr_auto_auto_auto] gap-4 bg-ink-900/70 px-5 py-3 text-xs font-extrabold uppercase tracking-widest text-sage-500 lg:grid">
            <span>Contractor</span><span>Payout</span><span className="text-right">Default</span><span>Status</span><span />
          </div>
          <ul className="divide-y-2 divide-ink-700">
            {employer.contractors.map((c) => (
              <li key={c.id} className="bg-ink-850/80">
                <div className="grid gap-2 px-5 py-4 lg:grid-cols-[1.4fr_1fr_auto_auto_auto] lg:items-center lg:gap-4">
                  <span className="min-w-0">
                    <span className="block truncate font-bold text-sand-100">{c.full_name}</span>
                    <span className="block truncate text-xs font-semibold text-sage-500">
                      {c.email ?? "No email"} · {c.country}
                    </span>
                    {c.is_active && <TrustBadges c={c} watch1099={watch.has(c.id)} />}
                  </span>
                  <span className="text-sm font-semibold text-sage-300">
                    {c.payout_method}
                    {c.iban_last4 ? ` · IBAN …${c.iban_last4}` : c.account_last4 ? ` · acct …${c.account_last4}` : ""}
                    {c.swift_code ? ` · ${c.swift_code}` : ""}
                  </span>
                  <span className="font-display font-semibold tabular-nums text-sand-100 lg:text-right">
                    {c.default_amount ? formatMoney(c.default_amount, c.currency) : "—"}
                  </span>
                  <Badge tone={c.is_active ? "success" : "neutral"}>{c.is_active ? "Active" : "Inactive"}</Badge>
                  <span>
                    {canEdit && c.is_active &&
                      (confirmingId === c.id ? (
                        <span className="flex gap-2">
                          <Button size="sm" variant="primary" onClick={() => { setConfirmingId(null); onDeactivate(c); }}>Confirm</Button>
                          <Button size="sm" variant="ghost" onClick={() => setConfirmingId(null)}>Keep</Button>
                        </span>
                      ) : (
                        <Button size="sm" variant="ghost" onClick={() => setConfirmingId(c.id)}>
                          <UserX /> Deactivate
                        </Button>
                      ))}
                  </span>
                </div>
                {c.is_active && (
                  <div className="flex flex-wrap gap-2 px-5 pb-4">
                    {c.bank_verification_status === "PENDING_VERIFICATION" && canVerify && (
                      <Button size="sm" variant="sage" onClick={() => setVerifying(c)}><ShieldCheck /> Verify bank change</Button>
                    )}
                    {canEdit && <Button size="sm" variant="outline" onClick={() => toggle(c.id, "edit")}><Pencil /> Edit</Button>}
                    {canEdit && <Button size="sm" variant="outline" onClick={() => toggle(c.id, "link")}><Link2 /> Onboarding link</Button>}
                    <Button size="sm" variant="ghost" onClick={() => toggle(c.id, "history")}><History /> History</Button>
                  </div>
                )}
                {panel?.id === c.id && (
                  <div className="px-3 pb-4 sm:px-5">
                    {panel.kind === "edit" && (
                      <EditContractorPanel employerId={employer.id} contractor={c} onClose={() => setPanel(null)}
                        onSaved={(updated, note) => { setPanel(null); onUpdated(updated, note); }} />
                    )}
                    {panel.kind === "link" && <OnboardingLinkPanel employerId={employer.id} contractor={c} onClose={() => setPanel(null)} />}
                    {panel.kind === "history" && <HistoryPanel employerId={employer.id} contractor={c} onClose={() => setPanel(null)} />}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {verifying && session && (
        <VerifyBankDialog
          employerId={employer.id} contractor={verifying} currentUserId={session.user.id}
          onClose={() => setVerifying(null)}
          onVerified={(c) => { setVerifying(null); onUpdated(c, `Bank details for ${c.full_name} verified.`); }}
        />
      )}
    </section>
  );
}
