"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowUpRight, CalendarClock, Check, ChevronDown, Copy, Download, FileText, Landmark, Loader2, Mail, Plus,
  RefreshCw, Send, X,
} from "lucide-react";

import { AppBar } from "@/components/dashboard/app-bar";
import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Pagination, usePagination } from "@/components/ui/pagination";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/select";
import {
  api, apiFunding, ApiError,
  type FundingPlan, type OrgFundingPlan, type PayFrequency, type Session, type SweepResponse, type WireDetails,
} from "@/lib/api";
import { daysPhrase, FREQUENCY_LABEL, fmtDay, STATUS_LABEL, STATUS_TONE } from "@/lib/funding-ui";
import { cn, formatMoney } from "@/lib/utils";

const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");
const EDITOR_ROLES = new Set(["owner", "admin"]);
const ROLE_NOTE = "Only owners and admins can change schedules, add funds or send emails.";

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked: the value stays selectable on screen */
    }
  }
  return (
    <button
      type="button" onClick={() => void copy()} aria-label={`Copy ${label}`}
      className="inline-flex size-8 shrink-0 items-center justify-center rounded-full text-sage-300 hover:bg-ink-700 hover:text-sand-100 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/50"
    >
      {copied ? <Check className="size-4 text-mint-400" /> : <Copy className="size-4" />}
    </button>
  );
}

function WireBlock({ wire }: { wire: WireDetails | null }) {
  const rows: { label: string; value: string | null | undefined }[] = wire?.status === "READY"
    ? [
        { label: "Account name", value: wire.account_name },
        { label: "Account number", value: wire.account_number },
        ...wire.routing.filter((r) => r.type !== "swift").map((r) => ({ label: r.label, value: r.value })),
        { label: "SWIFT/BIC", value: wire.swift_code },
        { label: "Bank", value: wire.bank_name },
        { label: "Reference", value: wire.reference },
      ]
    : [];
  return (
    <div className="rounded-3xl bg-ink-950/40 p-4">
      <h3 className="flex items-center gap-2 font-display text-lg font-semibold text-sand-100">
        <Landmark className="size-5 text-sage-400" /> Send funds to
      </h3>
      {!wire && <p className="mt-2 text-sm text-sage-400">This employer has no receiving account yet.</p>}
      {wire && wire.status !== "READY" && (
        <p className="mt-2 text-sm font-semibold text-sage-400">{wire.message ?? "Wire details are not available yet."}</p>
      )}
      {rows.length > 0 && (
        <dl className="mt-2 divide-y divide-ink-800">
          {rows.filter((r) => r.value).map((r) => (
            <div key={r.label} className="flex items-center justify-between gap-2 py-1.5">
              <div className="min-w-0">
                <dt className="text-xs font-bold uppercase tracking-wide text-sage-500">{r.label}</dt>
                <dd className="break-words font-mono text-sm font-semibold text-sand-100">{r.value}</dd>
              </div>
              <CopyButton value={r.value as string} label={r.label} />
            </div>
          ))}
        </dl>
      )}
      {wire?.status === "READY" && (
        <p className="mt-2 text-xs font-semibold leading-relaxed text-sage-500">
          Funds land in this employer&apos;s own wallet only. Put the reference on the transfer so it is matched quickly.
        </p>
      )}
    </div>
  );
}

function ScheduleEditor({ plan, onSaved, onCancel }: { plan: FundingPlan; onSaved: (p: FundingPlan) => void; onCancel: () => void }) {
  const [freq, setFreq] = useState<string>(plan.pay_frequency ?? "");
  const [date, setDate] = useState(plan.scheduled_pay_date ?? "");
  const [lead, setLead] = useState(String(plan.funding_lead_days));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(ev: React.FormEvent) {
    ev.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const next = await apiFunding.updateSchedule(plan.employer_id, {
        pay_frequency: (freq || null) as PayFrequency | null,
        next_pay_date: date || null,
        funding_lead_days: Number(lead),
      });
      onSaved(next);
    } catch (err) {
      setError(errMsg(err));
      setSaving(false);
    }
  }

  const id = `sched-${plan.employer_id}`;
  return (
    <form onSubmit={(e) => void save(e)} className="grid gap-3 rounded-3xl bg-ink-950/40 p-4 sm:grid-cols-3">
      <div className="grid gap-1.5">
        <Label htmlFor={`${id}-freq`}>How often</Label>
        <NativeSelect id={`${id}-freq`} value={freq} onChange={(e) => setFreq(e.target.value)}>
          <option value="">Not set</option>
          {Object.entries(FREQUENCY_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </NativeSelect>
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor={`${id}-date`}>Next pay date</Label>
        <Input id={`${id}-date`} type="date" value={date} onChange={(e) => setDate(e.target.value)} />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor={`${id}-lead`}>Fund this many business days before</Label>
        <Input id={`${id}-lead`} type="number" min={0} max={10} step={1} value={lead} onChange={(e) => setLead(e.target.value)} />
      </div>
      {error && <p role="alert" className="text-sm font-bold text-coral-300 sm:col-span-3">{error}</p>}
      <div className="flex flex-wrap gap-2 sm:col-span-3">
        <Button type="submit" size="sm" variant="sage" disabled={saving || lead === ""}>
          {saving ? <Loader2 className="animate-spin" /> : <Check />} Save schedule
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}><X /> Cancel</Button>
      </div>
    </form>
  );
}

function Fact({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "bad" | "good" }) {
  return (
    <div className="min-w-0 rounded-2xl bg-ink-950/40 px-4 py-3">
      <p className="text-xs font-bold uppercase tracking-wide text-sage-500">{label}</p>
      <p className={cn("mt-0.5 break-words font-display text-xl font-semibold tabular-nums",
        tone === "bad" ? "text-coral-300" : tone === "good" ? "text-mint-400" : "text-sand-100")}>{value}</p>
      {sub && <p className="text-xs font-semibold text-sage-500">{sub}</p>}
    </div>
  );
}

function PlanCard({ plan, canEdit, onChanged, notify }: {
  plan: FundingPlan; canEdit: boolean; onChanged: (p?: FundingPlan) => void; notify: (m: string, error?: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const linesPager = usePagination(plan.lines, 5, { syncUrl: false, resetKey: plan.employer_id });
  const priced = plan.status !== "RATE_UNAVAILABLE";
  const hasLines = plan.lines.length > 0;
  const wallet = Object.entries(plan.wallet);
  const late = plan.status === "OVERDUE";
  const gapTone = plan.is_short ? "bad" : "good";

  async function addFunds(ev: React.FormEvent) {
    ev.preventDefault();
    if (!(Number(amount) > 0)) return;
    setBusy("fund");
    try {
      await api.fund(plan.employer_id, amount);
      notify(`Added ${formatMoney(amount)} (simulated) to ${plan.employer_name}.`);
      setAmount("");
      onChanged();
    } catch (err) {
      notify(errMsg(err), true);
    } finally {
      setBusy(null);
    }
  }

  async function remind(kind: "reminder" | "statement") {
    setBusy(kind);
    try {
      const n = await apiFunding.sendReminder(plan.employer_id, kind);
      notify(`${kind === "statement" ? "Funding statement" : "Funding reminder"} to ${n.recipient} ${n.status === "SENT" ? "sent" : "queued"}. Open Notifications to see its delivery status.`);
    } catch (err) {
      notify(errMsg(err), true);
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card data-tour="funding-plan" className="grid gap-5 p-5 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="font-display text-2xl font-semibold text-sand-100">
            <Link href={`/dashboard/employers/${plan.employer_id}`} className="hover:underline hover:underline-offset-4">{plan.employer_name}</Link>
          </h2>
          <p className="mt-1 max-w-2xl text-sm font-semibold text-sage-300">{plan.message}</p>
        </div>
        <Badge tone={STATUS_TONE[plan.status]}>{STATUS_LABEL[plan.status]}</Badge>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Fact label="Pay date" value={fmtDay(plan.pay_date)}
          sub={plan.pay_frequency ? FREQUENCY_LABEL[plan.pay_frequency] : plan.pay_date ? "One-off" : "Set a schedule below"} />
        <Fact label="Fund by" value={fmtDay(plan.fund_by)} tone={late ? "bad" : undefined}
          sub={plan.fund_by ? `${daysPhrase(plan.days_until_fund_by)} · ${plan.funding_lead_days} business day${plan.funding_lead_days === 1 ? "" : "s"} before pay` : undefined} />
        <Fact label="Needs in wallet" value={priced ? formatMoney(plan.required_in_wallet_usd) : "—"} sub="pay + fees + run fee + reserve" />
        <Fact label="Has now" value={priced ? formatMoney(plan.available_usd) : "—"} sub="this employer's wallet only" />
      </div>

      {priced && hasLines && (
        <div className={cn("flex flex-wrap items-center justify-between gap-3 rounded-3xl border-2 p-4",
          plan.is_short ? "border-coral-600/60 bg-coral-500/10" : "border-mint-700/60 bg-mint-500/10")}>
          <div>
            <p className="text-sm font-bold text-sage-300">{plan.is_short ? "Gap to close" : "Gap"}</p>
            <p className={cn("font-display text-2xl font-semibold tabular-nums", gapTone === "bad" ? "text-coral-300" : "text-mint-400")}>
              {formatMoney(plan.gap_usd)}
            </p>
          </div>
          {plan.is_short && (
            <div className="text-right">
              <p className="text-sm font-bold text-sage-300">Recommended transfer</p>
              <p className="font-display text-2xl font-semibold tabular-nums text-sand-100">{formatMoney(plan.recommended_transfer_usd)}</p>
              <p className="text-xs font-semibold text-sage-500">gap + 3% FX buffer on non-USD pay, rounded up to $50</p>
            </div>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {wallet.length === 0 && <span className="rounded-full bg-ink-950/50 px-3 py-1.5 text-sm font-bold text-sage-400">Wallet is empty</span>}
        {wallet.map(([cur, amt]) => (
          <span key={cur} className="rounded-full bg-ink-950/50 px-3 py-1.5 text-sm font-bold text-sand-100">
            <span className="text-sage-400">{cur}</span> {formatMoney(amt, cur)}
          </span>
        ))}
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="grid min-w-0 content-start gap-4">
          <div>
            <button
              type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open}
              className="flex items-center gap-2 rounded-xl text-sm font-extrabold text-sand-200 hover:text-sand-50 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/50"
            >
              <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
              What this pays ({plan.lines.length} contractor{plan.lines.length === 1 ? "" : "s"}
              {plan.needs_amount.length > 0 ? `, ${plan.needs_amount.length} need an amount` : ""})
            </button>
            {open && (
              <div className="mt-3 overflow-x-auto rounded-3xl border-2 border-ink-700">
                <table className="w-full min-w-[520px] border-collapse text-left text-sm">
                  <thead>
                    <tr className="bg-ink-900/70 text-xs font-extrabold uppercase tracking-widest text-sage-500">
                      <th scope="col" className="px-3 py-2">Contractor</th>
                      <th scope="col" className="px-3 py-2 text-right">Amount</th>
                      <th scope="col" className="px-3 py-2">Method</th>
                      <th scope="col" className="px-3 py-2 text-right">Fee</th>
                      <th scope="col" className="px-3 py-2 text-right">USD value</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y-2 divide-ink-700">
                    {linesPager.pageItems.map((l) => (
                      <tr key={l.contractor_id} className="bg-ink-900/40">
                        <td className="px-3 py-2 font-bold text-sand-100">{l.name}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-sand-200">{formatMoney(l.amount, l.currency)}</td>
                        <td className="px-3 py-2 text-sage-300">{l.currency} {l.method}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-sand-200">{formatMoney(l.fee, l.currency)}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-sand-100">{formatMoney(l.amount_usd)}</td>
                      </tr>
                    ))}
                    {linesPager.page === linesPager.pageCount && plan.needs_amount.map((l) => (
                      <tr key={l.contractor_id} className="bg-ink-900/40">
                        <td className="px-3 py-2 font-bold text-sand-100">{l.name}</td>
                        <td colSpan={4} className="px-3 py-2 text-sm font-bold text-coral-300">
                          Needs an amount: set a default amount on the employer page.
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {priced && hasLines && (
                  <dl className="grid gap-1 border-t-2 border-ink-700 bg-ink-900/60 px-3 py-3 text-sm">
                    {[
                      ["Pay + payout fees", plan.payroll_usd], ["PaySilo run fee", plan.run_fee_usd],
                      ["Reserve kept in the wallet", plan.reserve_floor_usd], ["Needs in wallet", plan.required_in_wallet_usd],
                    ].map(([k, v]) => (
                      <div key={k} className="flex justify-between gap-3">
                        <dt className="font-semibold text-sage-300">{k}</dt>
                        <dd className="font-bold tabular-nums text-sand-100">{formatMoney(v)}</dd>
                      </div>
                    ))}
                  </dl>
                )}
                <div className="border-t-2 border-ink-700 bg-ink-900/40 px-3 pb-3"><Pagination state={linesPager} label="Contractors" /></div>
              </div>
            )}
          </div>

          {editing ? (
            <ScheduleEditor plan={plan} onCancel={() => setEditing(false)} onSaved={(p) => { setEditing(false); notify("Schedule saved."); onChanged(p); }} />
          ) : (
            <div>
              <Button variant="outline" size="sm" disabled={!canEdit} onClick={() => setEditing(true)} title={canEdit ? undefined : ROLE_NOTE}>
                <CalendarClock /> Edit schedule
              </Button>
            </div>
          )}

          <div className="flex flex-wrap items-end gap-3">
            <form onSubmit={(e) => void addFunds(e)} className="grid gap-1.5">
              <Label htmlFor={`fund-${plan.employer_id}`}>Add funds (simulated, USD)</Label>
              <div className="flex gap-2">
                <Input id={`fund-${plan.employer_id}`} type="number" min="0" step="0.01" inputMode="decimal" placeholder="0.00"
                  className="w-40" value={amount} disabled={!canEdit} onChange={(e) => setAmount(e.target.value)} />
                <Button type="submit" size="md" variant="sage" disabled={!canEdit || !(Number(amount) > 0) || busy === "fund"}>
                  {busy === "fund" ? <Loader2 className="animate-spin" /> : <Plus />} Add funds
                </Button>
              </div>
            </form>
            {priced && plan.is_short && Number(plan.recommended_transfer_usd) > 0 && canEdit && (
              <Button type="button" variant="ghost" size="sm" onClick={() => setAmount(Number(plan.recommended_transfer_usd).toFixed(2))}>
                Use recommended {formatMoney(plan.recommended_transfer_usd)}
              </Button>
            )}
          </div>

          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={!canEdit || busy !== null || !plan.is_short} onClick={() => void remind("reminder")}
              title={canEdit ? (plan.is_short ? undefined : "Nothing to ask for: this wallet already covers the payroll.") : ROLE_NOTE}>
              {busy === "reminder" ? <Loader2 className="animate-spin" /> : <Send />} Send funding reminder
            </Button>
            <Button size="sm" variant="outline" asChild>
              <a href={apiFunding.statementCsvUrl(plan.employer_id)} download><Download /> Statement (CSV)</a>
            </Button>
            <Button size="sm" variant="outline" asChild>
              <Link href={`/dashboard/funding/${plan.employer_id}/statement`}><FileText /> Printable statement</Link>
            </Button>
            <Button size="sm" variant="ghost" disabled={!canEdit || busy !== null || !hasLines} onClick={() => void remind("statement")}
              title={canEdit ? undefined : ROLE_NOTE}>
              {busy === "statement" ? <Loader2 className="animate-spin" /> : <Mail />} Email statement
            </Button>
          </div>
          {!canEdit && <p className="text-xs font-semibold text-sage-500">{ROLE_NOTE}</p>}
          {canEdit && !plan.contact_email && (
            <p className="text-xs font-semibold text-coral-300">No contact email on file for this employer, so reminders cannot be sent.</p>
          )}
        </div>

        <WireBlock wire={plan.wire} />
      </div>
    </Card>
  );
}

export default function FundingPage() {
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [data, setData] = useState<OrgFundingPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<{ message: string; error: boolean } | null>(null);
  const [sweep, setSweep] = useState<SweepResponse | null>(null);
  const [sweeping, setSweeping] = useState(false);
  const plansPager = usePagination(data?.plans ?? [], 5, { key: "plansPage" });

  const load = useCallback(async (initial = false) => {
    setError(null);
    try {
      const [me, plan] = await Promise.all([initial ? api.me() : Promise.resolve(null), apiFunding.plan()]);
      if (me) setSession(me);
      setData(plan);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login?next=/dashboard/funding");
        return;
      }
      setError(errMsg(err));
    }
    setLoading(false);
  }, [router]);

  useEffect(() => { void load(true); }, [load]);

  async function refresh() {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  }

  async function runSweep() {
    setSweeping(true);
    setSweep(null);
    try {
      setSweep(await apiFunding.runSweep());
    } catch (err) {
      setToast({ message: errMsg(err), error: true });
    } finally {
      setSweeping(false);
    }
  }

  const canEdit = !!session && EDITOR_ROLES.has(session.role);
  const s = data?.summary;

  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}

      <main className="mx-auto mt-8 grid max-w-6xl gap-6 px-4 sm:px-6">
        <section data-tour="page-header" className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Funding planner</h1>
            <p className="mt-1 max-w-2xl text-sage-300">
              What each employer must send, by when. Amounts use the same fee and reserve rules as the real run;
              each employer is checked against its own wallet only.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => void refresh()} disabled={refreshing || loading}>
              <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
            </Button>
            <Button data-tour="funding-sweep" onClick={() => void runSweep()} disabled={!canEdit || sweeping || loading}
              title={canEdit ? undefined : ROLE_NOTE}>
              {sweeping ? <Loader2 className="animate-spin" /> : <Send />} Run reminder sweep
            </Button>
          </div>
        </section>

        {loading && <SectionLoading label="Planning funding…" />}
        {error && <SectionError message={error} onRetry={() => void load(true)} />}
        {toast && (
          <div role={toast.error ? "alert" : "status"} className={cn("flex items-start justify-between gap-3 rounded-2xl border-2 px-4 py-3 text-sm font-bold",
            toast.error ? "border-coral-600/60 bg-coral-500/10 text-coral-300" : "border-sage-600/60 bg-sage-500/10 text-sage-300")}>
            <span>{toast.message}</span>
            <button type="button" aria-label="Dismiss" onClick={() => setToast(null)} className="shrink-0 rounded-full p-0.5 hover:bg-ink-700"><X className="size-4" /></button>
          </div>
        )}
        {sweep && (
          <Card className="grid gap-3 p-5" role="status">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="font-display text-lg font-semibold text-sand-100">Reminder sweep finished</h2>
                <p className="text-sm font-semibold text-sage-300">
                  {sweep.queued} email{sweep.queued === 1 ? "" : "s"} queued, {sweep.skipped} skipped.
                  Running it again will not send duplicates.
                </p>
              </div>
              <button type="button" aria-label="Dismiss sweep result" onClick={() => setSweep(null)} className="shrink-0 rounded-full p-1 hover:bg-ink-700"><X className="size-4 text-sage-300" /></button>
            </div>
            <ul className="divide-y divide-ink-800">
              {sweep.results.map((r) => (
                <li key={r.employer_id} className="flex flex-wrap items-baseline justify-between gap-x-3 py-1.5 text-sm">
                  <span className="font-bold text-sand-100">{r.employer_name}</span>
                  <span className={cn("font-semibold", r.action === "queued" ? "text-mint-400" : "text-sage-400")}>
                    {r.action === "queued" ? "Queued" : "Skipped"}: {r.reason}
                    {r.auto_continuation && <span className="block text-mint-400">Held payments prepared for approval: {r.auto_continuation.reason}</span>}
                  </span>
                </li>
              ))}
            </ul>
            <Button variant="outline" size="sm" asChild className="w-fit">
              <Link href="/dashboard/notifications">Open notifications <ArrowUpRight /></Link>
            </Button>
          </Card>
        )}

        {!loading && data && s && (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              {[
                { label: "Funded", value: String(s.funded), tone: "text-mint-400", sub: "wallet covers the next run" },
                { label: "Short", value: String(s.short), tone: s.short > 0 ? "text-sand-300" : "text-sand-100", sub: "wallet is below what is needed" },
                { label: "Overdue", value: String(s.overdue), tone: s.overdue > 0 ? "text-coral-300" : "text-sand-100", sub: "past the fund-by date" },
                { label: "Still to collect", value: formatMoney(s.total_to_collect_usd), tone: "text-sand-100", sub: "sum of recommended transfers" },
              ].map((x) => (
                <div key={x.label} className="rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4">
                  <p className="text-sm font-bold text-sage-400">{x.label}</p>
                  <p className={cn("mt-1 font-display text-3xl font-semibold tabular-nums", x.tone)}>{x.value}</p>
                  <p className="mt-1 text-xs font-semibold text-sage-500">{x.sub}</p>
                </div>
              ))}
            </div>

            {data.plans.length === 0 ? (
              <SectionEmpty title="No employers yet" body="Add an employer to plan its funding." />
            ) : (
              <div ref={plansPager.listRef} className="grid scroll-mt-24 gap-6">
                {plansPager.pageItems.map((p) => (
                  <PlanCard key={p.employer_id} plan={p} canEdit={canEdit}
                    notify={(message, isError = false) => setToast({ message, error: isError })}
                    onChanged={() => void load()} />
                ))}
              </div>
            )}
            <Pagination state={plansPager} label="Funding plans" className="-mt-2" />
            <div className="grid gap-2 rounded-3xl border-2 border-ink-700 bg-ink-850/60 p-4 text-sm font-semibold leading-relaxed text-sage-300">
              <p className="font-extrabold text-sand-100">How automatic continuation works</p>
              <p>
                When money arrives for an employer and its wallet can now cover payments that were held back, PaySilo
                prepares a continuation run for exactly those payments and emails the approvers a link to review it.
                It never approves or pays anything by itself: the run waits until a person signs it. It prepares at most one
                continuation per held run, and skips it if a later payroll already paid those contractors.
              </p>
              <p>If held payments sit untouched for more than a day, approvers get a reminder. Funding reminders only ask the employer to send money.</p>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
