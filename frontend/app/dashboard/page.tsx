"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowUpRight, CheckCircle2, ClipboardList, FileUp, Landmark, Loader2,
  PauseCircle, Play, Plus, Search, Timer, TriangleAlert,
} from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { AppBar } from "@/components/dashboard/app-bar";
import { RunPanel } from "@/components/dashboard/run-panel";
import { Badge, toneForStatus } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/select";
import { api, ApiError, type Employer, type Overview, type RunDetail, type RunSummary, type Session } from "@/lib/api";
import { useDataChanged } from "@/lib/events";
import { AnimatedNumber, Stagger } from "@/lib/gsap-anim";
import { DECIDER_ROLES, isMoneyMoved, runStatusInfo } from "@/lib/status";
import { cn, formatMoney } from "@/lib/utils";

const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

/*
 * Stat derivations — every figure below comes from the runs list the API
 * returns (RunSummary) or the overview counters. PaySilo's run lifecycle
 * has no DRAFT state (statuses run RECEIVED -> PRICED -> RESERVED ->
 * APPROVED -> SENT -> SETTLED, with HELD_* / REFUSED / FAILED exits), so
 * the reference layout's "Draft" card is shown honestly as "In progress"
 * (RECEIVED + PRICED) rather than inventing a draft count.
 */
const isInProgress = (r: RunSummary) => r.status === "RECEIVED" || r.status === "PRICED";
const isPendingApproval = (r: RunSummary) => r.pending?.kind === "approval" || r.status === "RESERVED";
const isHeld = (r: RunSummary) => r.status.startsWith("HELD") || r.status === "PARTIALLY_PAID" || r.pending?.kind === "escalation";
// Money moved (incl. partial payouts and post-payout follow-ups): the run fee was charged.
const isSettled = (r: RunSummary) => isMoneyMoved(r.status);

const STATUS_ORDER = [
  "RECEIVED", "PRICED", "RESERVED", "APPROVED", "SENT", "SETTLED",
  "PARTIALLY_PAID", "SENT_FEE_FAILED", "SENT_UNRECONCILED", "ATTEST_PENDING",
  "HELD_SHORTFALL", "HELD_BY_HUMAN", "APPROVAL_REJECTED", "REFUSED", "FAILED",
];

function StatCard({ icon: Icon, label, sub, children, accent }: {
  icon: typeof ClipboardList; label: string; sub?: string; accent: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4">
      <div className="flex items-center gap-2.5">
        <span className={cn("grid size-9 shrink-0 place-items-center rounded-xl text-sand-50", accent)}>
          <Icon className="size-5" />
        </span>
        <p className="text-sm font-bold leading-tight text-sage-300">{label}</p>
      </div>
      <p className="mt-3 font-display text-3xl font-semibold tabular-nums text-sand-100">{children}</p>
      {sub && <p className="mt-1 text-xs font-semibold leading-snug text-sage-500">{sub}</p>}
    </div>
  );
}

function EmployerCard({ employer, onRun, onFund, busy }: {
  employer: Employer; busy: boolean;
  onRun: (e: Employer) => void; onFund: (e: Employer, amount: string) => Promise<void>;
}) {
  const [funding, setFunding] = useState(false);
  const [amount, setAmount] = useState("");
  const [saving, setSaving] = useState(false);
  const balances = Object.entries(employer.balances);
  const valid = Number(amount) > 0;

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    if (!valid) return;
    setSaving(true);
    try {
      await onFund(employer, amount);
      setFunding(false);
      setAmount("");
    } catch {
      // the page shows the error; keep the form open
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="flex flex-col gap-4 p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-xl font-semibold text-sand-100">
            <Link href={`/dashboard/employers/${employer.id}`} className="hover:underline hover:underline-offset-4">
              {employer.name}
            </Link>
          </h3>
          <p className="text-sm font-semibold text-sage-500">
            {employer.country} · {employer.contractor_count} contractor{employer.contractor_count === 1 ? "" : "s"}
          </p>
        </div>
        <Badge tone={toneForStatus(employer.status)}>{employer.status}</Badge>
      </div>

      <div className="grid gap-1.5 rounded-2xl bg-ink-950/40 p-3">
        {balances.length === 0 && <p className="text-sm font-semibold text-sage-500">No funds yet</p>}
        {balances.map(([cur, amt]) => (
          <div key={cur} className="flex items-baseline justify-between">
            <span className="text-sm font-bold text-sage-400">{cur}</span>
            <span className="font-display text-lg font-semibold tabular-nums text-sand-100">{formatMoney(amt, cur)}</span>
          </div>
        ))}
      </div>

      {funding ? (
        <form onSubmit={(e) => void submit(e)} className="flex gap-2">
          <Input type="number" min="0" step="0.01" inputMode="decimal" autoFocus value={amount}
                 aria-label={`Amount to add for ${employer.name}`}
                 onChange={(e) => setAmount(e.target.value)} placeholder={`Amount (${employer.funding_currency})`} />
          <Button type="submit" variant="sage" disabled={!valid || saving}>
            {saving ? <Loader2 className="animate-spin" /> : "Add"}
          </Button>
          <Button type="button" variant="ghost" onClick={() => setFunding(false)}>Cancel</Button>
        </form>
      ) : (
        <div className="flex flex-wrap gap-3">
          <Button onClick={() => onRun(employer)} disabled={busy || employer.contractor_count === 0}>
            {busy ? <Loader2 className="animate-spin" /> : <Play />} Run payroll
          </Button>
          <Button variant="outline" onClick={() => setFunding(true)}><Plus /> Add funds</Button>
          <Button variant="ghost" asChild>
            <Link href="/dashboard/funding">Funding plan</Link>
          </Button>
          <Button variant="ghost" asChild>
            <Link href={`/dashboard/employers/${employer.id}`}>View silo <ArrowUpRight /></Link>
          </Button>
        </div>
      )}
    </Card>
  );
}

export default function DashboardPage() {
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<RunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [startingId, setStartingId] = useState<string | null>(null);
  const [chooserOpen, setChooserOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("ALL");

  const refresh = useCallback(async () => {
    const [o, r] = await Promise.all([api.overview(), api.runs()]);
    setOverview(o);
    setRuns(r);
  }, []);

  useEffect(() => {
    (async () => {
      try {
        setSession(await api.me());
        await refresh();
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          router.replace("/login?next=/dashboard");
          return;
        }
        setError(errMsg(err));
      }
      setLoading(false);
    })();
  }, [refresh, router]);

  async function startRun(employer: Employer) {
    setStartingId(employer.id);
    setError(null);
    setNotice(null);
    try {
      const detail = await api.employer(employer.id);
      // Same rule as the employer page: only contractors with a default amount are runnable.
      const active = detail.contractors.filter((c) => c.is_active);
      const payable = active.filter((c) => c.default_amount);
      const skipped = active.filter((c) => !c.default_amount);
      if (payable.length === 0) {
        setNotice(`${employer.name} has nothing to run: ${active.length === 0 ? "no active contractors" : "none of its active contractors has a default amount"}. Add a default amount to a contractor, or import a payroll sheet with amounts.`);
        return;
      }
      const run = await api.startRun(employer.id, payable.map((c) => ({ contractor_id: c.id })));
      setSelected(run);
      setChooserOpen(false);
      if (skipped.length > 0) {
        setNotice(`Skipped ${skipped.length} contractor${skipped.length === 1 ? "" : "s"} without a default amount: ${skipped.map((c) => c.full_name).join(", ")}.`);
      }
      await refresh();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setStartingId(null);
    }
  }

  function onRunPayroll() {
    const employers = overview?.employers ?? [];
    if (employers.length === 1) {
      void startRun(employers[0]);
      return;
    }
    setChooserOpen((v) => !v);
  }

  async function fund(employer: Employer, amount: string) {
    setError(null);
    setNotice(null);
    try {
      await api.fund(employer.id, amount);
      await refresh();
      setNotice(`Added ${formatMoney(amount, employer.funding_currency)} to ${employer.name}.`);
    } catch (err) {
      setError(errMsg(err));
      throw err;
    }
  }

  async function openRun(id: string) {
    setError(null);
    try {
      setSelected(await api.run(id));
    } catch (err) {
      setError(errMsg(err));
    }
  }

  // A decision made elsewhere (e.g. the chat panel) refreshes the list and the open run.
  useDataChanged(() => {
    void refresh().catch((err) => setError(errMsg(err)));
    setSelected((cur) => {
      if (cur) api.run(cur.id).then(setSelected).catch(() => { /* keep what is shown */ });
      return cur;
    });
  });

  async function onRunChange(run: RunDetail) {
    setSelected(run);
    await refresh().catch((err) => setError(errMsg(err)));
  }

  const stats = useMemo(() => {
    const settled = runs.filter(isSettled);
    const withRequired = settled.filter((r) => r.required_usd !== null && r.required_usd !== "");
    const finalized = withRequired.reduce((sum, r) => sum + (Number(r.required_usd) || 0), 0);
    return {
      total: runs.length,
      inProgress: runs.filter(isInProgress).length,
      pendingApproval: runs.filter(isPendingApproval).length,
      held: runs.filter(isHeld).length,
      settledCount: settled.length,
      finalized,
      finalizedMissing: settled.length - withRequired.length,
    };
  }, [runs]);

  const statusOptions = useMemo(() => {
    const present = new Set(runs.map((r) => r.status));
    const ordered = STATUS_ORDER.filter((s) => present.has(s));
    for (const s of present) if (!ordered.includes(s)) ordered.push(s);
    return ordered;
  }, [runs]);

  const visibleRuns = useMemo(() => {
    const q = query.trim().toLowerCase();
    return runs.filter((r) => {
      if (statusFilter !== "ALL" && r.status !== statusFilter) return false;
      if (!q) return true;
      return (
        r.employer_name.toLowerCase().includes(q) ||
        (r.period_label ?? "").toLowerCase().includes(q) ||
        r.id.toLowerCase().startsWith(q) ||
        r.status.toLowerCase().includes(q)
      );
    });
  }, [runs, query, statusFilter]);

  const fees = overview ? Object.entries(overview.platform_fees) : [];
  const feeText = fees.length === 0 ? formatMoney(0) : fees.map(([c, a]) => formatMoney(a, c)).join(" + ");
  const canDecide = !!session && DECIDER_ROLES.includes(session.role);

  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}

      <main className="mx-auto mt-8 grid max-w-6xl gap-8 px-4 sm:px-6">
        {loading && (
          <div className="flex items-center justify-center gap-3 py-24 font-bold text-sage-400">
            <Loader2 className="size-5 animate-spin" /> Loading your silos…
          </div>
        )}

        {error && (
          <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">{error}</p>
        )}
        {notice && (
          <p role="status" className="rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-4 py-3 text-sm font-bold text-sage-300">{notice}</p>
        )}

        {overview && (
          <>
            <section>
              <div className="flex flex-wrap items-end justify-between gap-4">
                <div>
                  <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Command Center</h1>
                  <p className="mt-1 text-sage-300">
                    {overview.employers.length} employer{overview.employers.length === 1 ? "" : "s"} · platform fees collected {feeText}.
                    Every employer&apos;s money stays in its own silo.
                  </p>
                </div>
                <div className="flex flex-wrap gap-3">
                  <Button variant="outline" asChild>
                    <Link href="/dashboard/imports"><FileUp /> Import payroll</Link>
                  </Button>
                  <Button onClick={onRunPayroll} disabled={overview.employers.length === 0 || !!startingId}>
                    {startingId ? <Loader2 className="animate-spin" /> : <Play />} Run Payroll
                  </Button>
                </div>
              </div>

              {chooserOpen && overview.employers.length > 1 && (
                <Card className="mt-4 p-4">
                  <p className="text-sm font-bold text-sage-300">Run payroll for which employer? Each run uses that employer&apos;s active contractors and default amounts. You can also ask Silo in the chat panel, or import a sheet.</p>
                  <ul className="mt-3 grid gap-2 sm:grid-cols-2">
                    {overview.employers.map((e) => {
                      const firstBalance = Object.entries(e.balances)[0];
                      return (
                        <li key={e.id} className="flex items-center justify-between gap-3 rounded-2xl bg-ink-950/40 px-4 py-3">
                          <span className="min-w-0">
                            <span className="block truncate font-bold text-sand-100">{e.name}</span>
                            <span className="block text-xs font-semibold text-sage-500">
                              {e.contractor_count} contractor{e.contractor_count === 1 ? "" : "s"}
                              {firstBalance ? ` · ${formatMoney(firstBalance[1], firstBalance[0])} available` : " · no funds yet"}
                            </span>
                          </span>
                          <Button size="sm" onClick={() => void startRun(e)} disabled={startingId === e.id || e.contractor_count === 0}>
                            {startingId === e.id ? <Loader2 className="animate-spin" /> : <Play />} Run
                          </Button>
                        </li>
                      );
                    })}
                  </ul>
                </Card>
              )}

              <Stagger className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
                <StatCard icon={ClipboardList} label="Total Runs" accent="bg-ink-600" sub="All runs in this organization">
                  <AnimatedNumber value={stats.total} />
                </StatCard>
                <StatCard icon={Timer} label="In progress" accent="bg-clay-500" sub="Received or priced. PaySilo has no draft state, so no draft count is shown.">
                  <AnimatedNumber value={stats.inProgress} />
                </StatCard>
                <StatCard icon={PauseCircle} label="Pending Approval" accent="bg-sand-400 !text-ink-900" sub="Reserved runs waiting on a signed decision">
                  <AnimatedNumber value={stats.pendingApproval} />
                </StatCard>
                <StatCard icon={TriangleAlert} label="Held" accent="bg-coral-500" sub="Shortfall or held by a person — needs funding or review">
                  <AnimatedNumber value={stats.held} />
                </StatCard>
                <StatCard icon={CheckCircle2} label="Finalized Net Pay" accent="bg-mint-500 !text-ink-950"
                          sub={stats.finalizedMissing > 0
                            ? `Settled required total (USD) across ${stats.settledCount} settled runs; ${stats.finalizedMissing} settled run(s) report no required total and are excluded.`
                            : `Settled required total (USD) across ${stats.settledCount} settled run${stats.settledCount === 1 ? "" : "s"}`}>
                  {formatMoney(stats.finalized.toFixed(2))}
                </StatCard>
              </Stagger>
            </section>

            <section aria-label="Payroll runs">
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <h2 className="font-display text-2xl font-semibold text-sand-100">Payroll runs</h2>
                <Link href="/dashboard/funding" className="inline-flex items-center gap-1.5 text-sm font-bold text-sage-300 hover:text-sand-100">
                  <Landmark className="size-4" /> Check funding before the next run
                </Link>
              </div>

              <div className="mb-4 flex flex-wrap gap-3">
                <label className="relative min-w-56 flex-1">
                  <span className="sr-only">Search payroll runs</span>
                  <Search className="pointer-events-none absolute left-3.5 top-1/2 size-5 -translate-y-1/2 text-sage-400" />
                  <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search runs by employer, period or status…" className="pl-11" />
                </label>
                <div className="w-56">
                  <label className="sr-only" htmlFor="run-status-filter">Filter by status</label>
                  <NativeSelect id="run-status-filter" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                    <option value="ALL">All statuses</option>
                    {statusOptions.map((s) => (
                      <option key={s} value={s}>{runStatusInfo({ status: s }).label}</option>
                    ))}
                  </NativeSelect>
                </div>
              </div>

              {runs.length === 0 ? (
                <Card className="flex flex-col items-center gap-2 p-8 text-center">
                  <LottieAnimation name="silo-idle" className="size-32" label="Silo" />
                  <p className="font-bold text-sand-100">No runs yet</p>
                  <p className="max-w-md text-sm text-sage-400">Start a run from an employer below, import a payroll sheet, or ask Silo to run payroll for you.</p>
                </Card>
              ) : visibleRuns.length === 0 ? (
                <Card className="p-8 text-center">
                  <p className="font-bold text-sand-100">No runs match this search</p>
                  <p className="mt-1 text-sm text-sage-400">Try a different employer, period or status.</p>
                  <Button variant="ghost" className="mt-2" onClick={() => { setQuery(""); setStatusFilter("ALL"); }}>Clear search and filter</Button>
                </Card>
              ) : (
                <div className="overflow-x-auto rounded-4xl border-2 border-ink-700">
                  <table className="w-full min-w-[760px] border-collapse text-left">
                    <thead>
                      <tr className="bg-ink-900/70 text-xs font-extrabold uppercase tracking-widest text-sage-500">
                        <th scope="col" className="px-5 py-3">Run</th>
                        <th scope="col" className="px-5 py-3">Employer</th>
                        <th scope="col" className="px-5 py-3">Created</th>
                        <th scope="col" className="px-5 py-3">Status</th>
                        <th scope="col" className="px-5 py-3 text-right">Required</th>
                        <th scope="col" className="px-5 py-3 text-right">Shortfall</th>
                        <th scope="col" className="px-5 py-3"><span className="sr-only">Open</span></th>
                      </tr>
                    </thead>
                    <tbody className="divide-y-2 divide-ink-700">
                      {visibleRuns.map((r) => (
                        <tr key={r.id} className={cn("bg-ink-850/80 transition-colors hover:bg-ink-800", selected?.id === r.id && "bg-ink-800")}>
                          <td className="px-5 py-3.5">
                            <button type="button" onClick={() => void openRun(r.id)} className="font-bold text-sand-100 hover:underline hover:underline-offset-4">
                              {r.period_label ?? "Payroll run"}
                            </button>
                            <span className="block text-xs font-semibold text-sage-500">#{r.id.slice(0, 8)} · via {r.source}</span>
                          </td>
                          <td className="px-5 py-3.5 font-semibold text-sand-200">{r.employer_name}</td>
                          <td className="px-5 py-3.5 text-sm font-semibold text-sage-400">{new Date(r.created_at).toLocaleDateString()}{" "}
                            <span className="text-sage-500">{new Date(r.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
                          </td>
                          <td className="px-5 py-3.5">
                            <Badge tone={runStatusInfo(r).tone}>{runStatusInfo(r).label}</Badge>
                            {runStatusInfo(r).detail && <span className="mt-1 block max-w-52 text-xs font-semibold text-sand-300">{runStatusInfo(r).detail}</span>}
                            {r.pending && <span className="mt-1 block max-w-44 text-xs font-semibold text-sand-300">{r.pending.message}</span>}
                          </td>
                          <td className="px-5 py-3.5 text-right font-display font-semibold tabular-nums text-sand-100">
                            {r.required_usd ? formatMoney(r.required_usd) : "—"}
                          </td>
                          <td className={cn("px-5 py-3.5 text-right font-display font-semibold tabular-nums", r.shortfall_usd && Number(r.shortfall_usd) > 0 ? "text-coral-300" : "text-sage-400")}>
                            {r.shortfall_usd && Number(r.shortfall_usd) > 0 ? formatMoney(r.shortfall_usd) : "—"}
                          </td>
                          <td className="px-5 py-3.5 text-right">
                            <Link href={`/dashboard/runs/${r.id}`} aria-label={`Open the full page for this ${r.employer_name} run`}
                                  className="inline-grid size-9 place-items-center rounded-full border-2 border-ink-600 text-sage-300 transition-colors hover:border-sage-500 hover:text-sand-100">
                              <ArrowUpRight className="size-4" />
                            </Link>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <p className="mt-2 text-xs font-semibold text-sage-500">
                Required and shortfall are USD figures stored on each run by the pricing engine. Select a run name to inspect its assessment below.
              </p>

              <Card className="mt-5 p-5 sm:p-6">
                {selected ? (
                  <RunPanel key={selected.id} run={selected} canDecide={canDecide} onChange={(r) => void onRunChange(r)} />
                ) : (
                  <p className="py-10 text-center font-semibold text-sage-500">Select a run to see its assessment, approvals and timeline.</p>
                )}
              </Card>
            </section>

            <section>
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <h2 className="font-display text-2xl font-semibold text-sand-100">Employers</h2>
                <Button variant="outline" size="sm" asChild>
                  <Link href="/dashboard/employers/new"><Plus /> Add employer</Link>
                </Button>
              </div>
              {overview.employers.length === 0 ? (
                <Card className="flex flex-col items-center gap-2 p-8 text-center">
                  <LottieAnimation name="silo-idle" className="size-40" label="Silo" />
                  <p className="font-bold text-sand-100">No employers yet</p>
                  <p className="text-sm text-sage-400">Employers you add will show up here with their own balances.</p>
                  <Button asChild className="mt-2">
                    <Link href="/dashboard/employers/new"><Plus /> Add your first employer</Link>
                  </Button>
                </Card>
              ) : (
                <Stagger className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                  {overview.employers.map((e) => (
                    <EmployerCard key={e.id} employer={e} busy={startingId === e.id} onRun={(x) => void startRun(x)} onFund={fund} />
                  ))}
                </Stagger>
              )}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
