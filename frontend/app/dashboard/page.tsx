"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Play, Plus } from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { AppBar } from "@/components/dashboard/app-bar";
import { RunPanel } from "@/components/dashboard/run-panel";
import { Badge, toneForStatus } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, ApiError, type Employer, type Overview, type RunDetail, type RunSummary, type Session } from "@/lib/api";
import { formatMoney } from "@/lib/utils";

const DECIDERS = ["owner", "admin", "approver"];
const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4">
      <p className="text-sm font-bold text-sage-400">{label}</p>
      <p className="mt-1 font-display text-3xl font-semibold tabular-nums text-sand-100">{value}</p>
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
          <h3 className="font-display text-xl font-semibold text-sand-100">{employer.name}</h3>
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
      const active = detail.contractors.filter((c) => c.is_active);
      if (active.length === 0 || !active.some((c) => c.default_amount)) {
        setNotice(`${employer.name} has no active contractors with a default amount to pay.`);
        return;
      }
      const run = await api.startRun(employer.id, active.map((c) => ({ contractor_id: c.id })));
      setSelected(run);
      await refresh();
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setStartingId(null);
    }
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

  async function onRunChange(run: RunDetail) {
    setSelected(run);
    await refresh().catch((err) => setError(errMsg(err)));
  }

  const fees = overview ? Object.entries(overview.platform_fees) : [];
  const feeText = fees.length === 0 ? formatMoney(0) : fees.map(([c, a]) => formatMoney(a, c)).join(" + ");
  const canDecide = !!session && DECIDERS.includes(session.role);

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
              <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Dashboard</h1>
              <p className="mt-1 text-sage-300">Every employer&apos;s money stays in its own silo.</p>
              <div className="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-5">
                <Stat label="Employers" value={overview.employers.length} />
                <Stat label="Runs" value={overview.runs_total} />
                <Stat label="Settled" value={overview.runs_settled} />
                <Stat label="Waiting for you" value={overview.runs_waiting} />
                <div className="col-span-2 lg:col-span-1">
                  <Stat label="Platform fees" value={feeText} />
                </div>
              </div>
            </section>

            <section>
              <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">Employers</h2>
              {overview.employers.length === 0 ? (
                <Card className="flex flex-col items-center gap-2 p-8 text-center">
                  <LottieAnimation name="silo-idle" className="size-40" label="Silo" />
                  <p className="font-bold text-sand-100">No employers yet</p>
                  <p className="text-sm text-sage-400">Employers you add will show up here with their own balances.</p>
                </Card>
              ) : (
                <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                  {overview.employers.map((e) => (
                    <EmployerCard key={e.id} employer={e} busy={startingId === e.id} onRun={(x) => void startRun(x)} onFund={fund} />
                  ))}
                </div>
              )}
            </section>

            <section className="grid gap-6 lg:grid-cols-[minmax(0,20rem)_1fr] lg:items-start">
              <div>
                <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">Recent runs</h2>
                {runs.length === 0 ? (
                  <Card className="flex flex-col items-center gap-2 p-6 text-center">
                    <LottieAnimation name="silo-idle" className="size-28" label="Silo" />
                    <p className="text-sm font-semibold text-sage-400">No runs yet. Start one from an employer above.</p>
                  </Card>
                ) : (
                  <ul className="grid gap-2">
                    {runs.map((r) => (
                      <li key={r.id}>
                        <button type="button" onClick={() => void openRun(r.id)} aria-current={selected?.id === r.id}
                                className="w-full rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4 text-left transition-colors hover:border-sage-600 aria-[current=true]:border-coral-500">
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-bold text-sand-100">{r.employer_name}</span>
                            <Badge tone={toneForStatus(r.status)}>{r.status.replaceAll("_", " ")}</Badge>
                          </div>
                          <p className="mt-1 text-xs font-semibold text-sage-500">
                            {r.period_label ? `${r.period_label} · ` : ""}{new Date(r.created_at).toLocaleString()}
                          </p>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <Card className="p-5 sm:p-6">
                {selected ? (
                  <RunPanel key={selected.id} run={selected} canDecide={canDecide} onChange={(r) => void onRunChange(r)} />
                ) : (
                  <p className="py-10 text-center font-semibold text-sage-500">Select a run to see its assessment, approvals and timeline.</p>
                )}
              </Card>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
