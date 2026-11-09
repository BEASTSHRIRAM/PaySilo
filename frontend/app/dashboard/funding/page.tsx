"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowUpRight, CircleCheck, FileUp, Landmark, Loader2, Play, Plus, RefreshCw, TriangleAlert,
} from "lucide-react";

import { AppBar } from "@/components/dashboard/app-bar";
import { PageNotice, SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { Badge, toneForStatus } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  api, ApiError,
  type Assessment, type Employer, type Overview, type RunDetail, type RunSummary, type Session,
} from "@/lib/api";
import { AnimatedMoney } from "@/lib/gsap-anim";
import { cn, formatMoney } from "@/lib/utils";

const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");
const num = (v: string | null | undefined): number => {
  const n = Number(v);
  return v !== null && v !== undefined && v !== "" && Number.isFinite(n) ? n : 0;
};
const has = (v: string | null | undefined): boolean => v !== null && v !== undefined && v !== "";

const TERMINAL = new Set(["SETTLED", "FAILED", "REFUSED", "APPROVAL_REJECTED"]);

/*
 * Which run funds next? The employer's most recent run that is not in a
 * terminal state; if every run is terminal, the most recent run overall
 * (its assessment still shows the last funding picture). Runs are sorted
 * by created_at here — the API's list order is not relied on.
 */
function pickFundingRun(runs: RunSummary[]): RunSummary | null {
  if (runs.length === 0) return null;
  const sorted = [...runs].sort((a, b) => b.created_at.localeCompare(a.created_at));
  return sorted.find((r) => !TERMINAL.has(r.status)) ?? sorted[0];
}

interface EmployerFunding {
  employer: Employer;
  run: RunSummary | null;
  detail: RunDetail | null;
  error: string | null;
}

function EquationRow({ label, detail, value, currency = "USD", strong, muted }: {
  label: string; detail?: string; value: string | null; currency?: string; strong?: boolean; muted?: boolean;
}) {
  return (
    <div className={cn("flex items-baseline justify-between gap-3 py-2", strong && "border-t-2 border-ink-600 pt-3")}>
      <span className="min-w-0">
        <span className={cn("block text-sm", strong ? "font-extrabold text-sand-100" : "font-bold text-sage-300")}>{label}</span>
        {detail && <span className="block text-xs font-semibold text-sage-500">{detail}</span>}
      </span>
      {has(value) ? (
        <AnimatedMoney
          value={value as string}
          currency={currency}
          className={cn("font-display font-semibold tabular-nums", strong ? "text-xl text-sand-100" : "text-base", muted ? "text-sage-300" : "text-sand-100")}
        />
      ) : (
        <span className="font-display text-base font-semibold text-sage-500" title="Not returned by the API for this run">—</span>
      )}
    </div>
  );
}

function FundingCard({ data, onFunded }: { data: EmployerFunding; onFunded: (msg: string) => void }) {
  const { employer, run, detail, error } = data;
  const a: Assessment | null = detail?.assessment ?? null;
  const [amount, setAmount] = useState("");
  const [funding, setFunding] = useState(false);
  const [fundError, setFundError] = useState<string | null>(null);
  const balances = Object.entries(employer.balances);
  const valid = Number(amount) > 0;

  async function submitFund(ev: React.FormEvent) {
    ev.preventDefault();
    if (!valid) return;
    setFunding(true);
    setFundError(null);
    try {
      await api.fund(employer.id, amount);
      setAmount("");
      onFunded(`Added ${formatMoney(amount, employer.funding_currency)} to ${employer.name}. Balances and the funding picture have been refreshed.`);
    } catch (err) {
      setFundError(errMsg(err));
    } finally {
      setFunding(false);
    }
  }

  // The funding equation, in USD, from the run's stored assessment.
  // Identities (backend policy): required_usd = payroll_usd + platform_fee_usd;
  // total needed = required_usd + reserve_floor_usd;
  // shortfall_usd = max(0, needed - available_usd).
  const needed = a ? num(a.required_usd) + num(a.reserve_floor_usd) : 0;
  const headroom = a ? num(a.available_usd) - needed : 0;
  const short = a ? num(a.shortfall_usd) > 0 : false;
  const segments = a
    ? [
        { label: "Payroll incl. payout fees", value: num(a.payroll_usd), bar: "bg-sage-500", text: "text-sage-300" },
        { label: "Platform fee", value: num(a.platform_fee_usd), bar: "bg-sand-400", text: "text-sand-300" },
        { label: "Reserve floor", value: num(a.reserve_floor_usd), bar: "bg-clay-400", text: "text-clay-300" },
      ]
    : [];
  const availablePct = a && needed > 0 ? Math.min(100, Math.max(0, (num(a.available_usd) / needed) * 100)) : 0;

  return (
    <Card className="grid gap-6 p-5 sm:p-6 xl:grid-cols-[minmax(0,1fr)_21rem]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="font-display text-2xl font-semibold text-sand-100">
              <Link href={`/dashboard/employers/${employer.id}`} className="hover:underline hover:underline-offset-4">{employer.name}</Link>
            </h2>
            <p className="mt-0.5 text-sm font-semibold text-sage-500">
              {employer.country} · funds in {employer.funding_currency} · {employer.contractor_count} contractor{employer.contractor_count === 1 ? "" : "s"}
            </p>
          </div>
          <Badge tone={toneForStatus(employer.status)}>{employer.status}</Badge>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          {balances.length === 0 && <span className="rounded-full bg-ink-950/50 px-3 py-1.5 text-sm font-bold text-sage-400">No funds yet</span>}
          {balances.map(([cur, amt]) => (
            <span key={cur} className="rounded-full bg-ink-950/50 px-3 py-1.5 text-sm font-bold text-sand-100">
              <span className="text-sage-400">{cur}</span> {formatMoney(amt, cur)}
            </span>
          ))}
        </div>

        {error && <div className="mt-5"><SectionError message={error} /></div>}

        {!error && !run && (
          <div className="mt-5 rounded-3xl bg-ink-950/40 p-5">
            <p className="font-bold text-sand-100">No assessed run yet</p>
            <p className="mt-1 max-w-lg text-sm text-sage-400">
              The funding equation needs a priced run. Start a run or import a payroll sheet — once the pricing engine
              assesses it, the exact amount this silo must hold appears here. No figure is estimated in the meantime.
            </p>
            <div className="mt-4 flex flex-wrap gap-3">
              <Button size="sm" asChild><Link href="/dashboard"><Play /> Run payroll</Link></Button>
              <Button size="sm" variant="outline" asChild><Link href="/dashboard/imports"><FileUp /> Import payroll</Link></Button>
            </div>
          </div>
        )}

        {!error && run && !a && (
          <div className="mt-5 rounded-3xl bg-ink-950/40 p-5">
            <p className="font-bold text-sand-100">This run has no stored assessment</p>
            <p className="mt-1 max-w-lg text-sm text-sage-400">
              Run {run.period_label ?? `#${run.id.slice(0, 8)}`} is {run.status.replaceAll("_", " ").toLowerCase()} and the API returned no
              assessment for it, so the funding equation cannot be shown. Open the run for its timeline.
            </p>
            <Button size="sm" variant="outline" className="mt-4" asChild>
              <Link href={`/dashboard/runs/${run.id}`}>Open run <ArrowUpRight /></Link>
            </Button>
          </div>
        )}

        {!error && run && a && (
          <div className="mt-5">
            <p className="text-sm font-semibold text-sage-400">
              Based on {run.period_label ?? "the latest open run"} · status {run.status.replaceAll("_", " ")} ·{" "}
              <Link href={`/dashboard/runs/${run.id}`} className="font-bold text-sand-200 underline-offset-4 hover:underline">open run</Link>
              {" "}· assessed {new Date(run.created_at).toLocaleDateString()}
            </p>

            <div className="mt-3 divide-y divide-ink-800 rounded-3xl bg-ink-950/40 px-4 py-2">
              <EquationRow label="Available in this silo (USD value)" detail="The employer's own wallet only — never another employer's money" value={a.available_usd} />
              <EquationRow
                label="Payroll, including payout fees"
                detail={
                  a.payroll_currency === "MIXED"
                    ? `Salaries ${formatMoney(a.total_payroll)} + payout fees ${formatMoney(a.swift_fees)} (USD equivalents, mixed currencies)`
                    : `Salaries ${formatMoney(a.total_payroll, a.payroll_currency)} + payout fees ${formatMoney(a.swift_fees, a.payroll_currency)}`
                }
                value={a.payroll_usd}
              />
              <EquationRow label="Platform fee for this run" value={a.platform_fee_usd} />
              <EquationRow label="Required — leaves the wallet" detail="Payroll incl. fees + platform fee" value={a.required_usd} strong />
              <EquationRow label="Reserve floor — must stay in the silo" value={a.reserve_floor_usd} muted />
              <EquationRow label="Total that must be in the silo" detail="Required + reserve floor" value={needed.toFixed(2)} strong />
            </div>

            {short ? (
              <p role="alert" className="mt-4 flex gap-2.5 rounded-3xl border-2 border-coral-600/60 bg-coral-500/10 p-4 text-[15px] font-bold leading-relaxed text-coral-200">
                <TriangleAlert className="mt-0.5 size-5 shrink-0 text-coral-400" />
                <span>
                  Short by {formatMoney(a.shortfall_usd)} against this run. Add at least that much to {employer.name}&apos;s
                  silo before the pay date — the run stays held until the silo covers the required amount plus the reserve.
                </span>
              </p>
            ) : (
              <p role="status" className="mt-4 flex gap-2.5 rounded-3xl border-2 border-sage-600/60 bg-sage-500/10 p-4 text-[15px] font-bold leading-relaxed text-sage-200">
                <CircleCheck className="mt-0.5 size-5 shrink-0 text-sage-400" />
                <span>
                  Covered. After this run and the reserve floor, the silo keeps {formatMoney(headroom.toFixed(2))} of headroom.
                </span>
              </p>
            )}

            <div className="mt-5">
              <div className="flex items-baseline justify-between gap-3">
                <p className="text-sm font-extrabold text-sand-100">What the total is made of</p>
                <p className="text-xs font-bold text-sage-500">Total needed {formatMoney(needed.toFixed(2))}</p>
              </div>
              <div className="mt-2 flex h-5 w-full overflow-hidden rounded-full bg-ink-950/70" role="img"
                   aria-label={`Composition of the ${formatMoney(needed.toFixed(2))} total: ${segments.map((s) => `${s.label} ${formatMoney(s.value.toFixed(2))}`).join(", ")}`}>
                {segments.map((s) => (
                  <div key={s.label} className={cn("h-full", s.bar)} style={{ width: needed > 0 ? `${(s.value / needed) * 100}%` : "0%" }} />
                ))}
              </div>
              <ul className="mt-2.5 grid gap-1.5 sm:grid-cols-3">
                {segments.map((s) => (
                  <li key={s.label} className="flex items-baseline justify-between gap-2 text-sm">
                    <span className={cn("flex items-center gap-1.5 font-bold", s.text)}>
                      <span aria-hidden className={cn("size-2.5 rounded-full", s.bar)} /> {s.label}
                    </span>
                    <span className="font-display font-semibold tabular-nums text-sand-100">
                      {formatMoney(s.value.toFixed(2))} · {needed > 0 ? `${((s.value / needed) * 100).toFixed(1)}%` : "—"}
                    </span>
                  </li>
                ))}
              </ul>

              <div className="mt-4 flex items-baseline justify-between gap-3">
                <p className="text-sm font-extrabold text-sand-100">Available vs needed</p>
                <p className="text-xs font-bold text-sage-500">
                  {formatMoney(a.available_usd)} of {formatMoney(needed.toFixed(2))} · {needed > 0 ? `${availablePct.toFixed(0)}%` : "—"}
                </p>
              </div>
              <div className="mt-2 h-3 w-full overflow-hidden rounded-full bg-ink-950/70">
                <div className={cn("h-full rounded-full", short ? "bg-coral-500" : "bg-mint-500")} style={{ width: `${availablePct}%` }} />
              </div>
            </div>

            {a.by_currency.length > 0 && (
              <div className="mt-5 overflow-x-auto rounded-3xl border-2 border-ink-700">
                <table className="w-full min-w-[560px] border-collapse text-left">
                  <thead>
                    <tr className="bg-ink-900/70 text-xs font-extrabold uppercase tracking-widest text-sage-500">
                      <th scope="col" className="px-4 py-2.5">Currency</th>
                      <th scope="col" className="px-4 py-2.5 text-right">Payouts</th>
                      <th scope="col" className="px-4 py-2.5 text-right">Salaries</th>
                      <th scope="col" className="px-4 py-2.5 text-right">Payout fees</th>
                      <th scope="col" className="px-4 py-2.5 text-right">Required</th>
                      <th scope="col" className="px-4 py-2.5 text-right">Required (USD)</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y-2 divide-ink-700">
                    {a.by_currency.map((row) => (
                      <tr key={row.currency} className="bg-ink-900/40 text-sm">
                        <td className="px-4 py-2.5 font-extrabold text-sand-100">{row.currency}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums text-sand-200">{row.payouts}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums text-sand-200">{formatMoney(row.payroll, row.currency)}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums text-sand-200">{formatMoney(row.fees, row.currency)}</td>
                        <td className="px-4 py-2.5 text-right font-semibold tabular-nums text-sand-100">{formatMoney(row.required, row.currency)}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums text-sand-200">{formatMoney(row.required_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>

      <aside className="h-fit rounded-3xl bg-ink-950/40 p-5 xl:sticky xl:top-40">
        <h3 className="flex items-center gap-2 font-display text-lg font-semibold text-sand-100">
          <Landmark className="size-5 text-sage-400" /> Deposit instructions
        </h3>
        <ol className="mt-3 grid list-decimal gap-2 pl-5 text-sm leading-relaxed text-sage-300">
          <li>Send funds to {employer.name}&apos;s connected account
            {employer.airwallex_account_id
              ? <> (account …{employer.airwallex_account_id.slice(-6)})</>
              : " — PaySilo provisions it when the silo is created"}.
          </li>
          <li>Funds land in this silo only. They can never pay another employer&apos;s run.</li>
          <li>In this workspace, use Add funds to record a deposit, then refresh — the equation above re-reads the live balance and the run&apos;s stored assessment.</li>
        </ol>

        <form onSubmit={(e) => void submitFund(e)} className="mt-4 grid gap-2">
          <label className="text-sm font-bold text-sage-300" htmlFor={`fund-${employer.id}`}>
            Add funds ({employer.funding_currency})
          </label>
          <div className="flex gap-2">
            <Input
              id={`fund-${employer.id}`}
              type="number" min="0" step="0.01" inputMode="decimal"
              value={amount} onChange={(e) => setAmount(e.target.value)}
              placeholder="0.00"
            />
            <Button type="submit" variant="sage" disabled={!valid || funding}>
              {funding ? <Loader2 className="animate-spin" /> : <Plus />} Add
            </Button>
          </div>
          {fundError && <p role="alert" className="text-sm font-bold text-coral-300">{fundError}</p>}
        </form>

        <div className="mt-4 flex flex-wrap gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link href={`/dashboard/employers/${employer.id}`}>Open employer <ArrowUpRight /></Link>
          </Button>
          <Button variant="ghost" size="sm" asChild>
            <Link href="/dashboard/calendar">Check the pay date</Link>
          </Button>
        </div>
        <p className="mt-3 text-xs font-semibold leading-relaxed text-sage-500">
          A line shown as — means the API did not return that figure for this run. PaySilo never estimates a funding number.
        </p>
      </aside>
    </Card>
  );
}

export default function FundingPage() {
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [fundings, setFundings] = useState<EmployerFunding[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const loadAll = useCallback(async (base: Overview) => {
    const results = await Promise.all(
      base.employers.map(async (employer): Promise<EmployerFunding> => {
        try {
          const runs = await api.runs(employer.id);
          const run = pickFundingRun(runs);
          if (!run) return { employer, run: null, detail: null, error: null };
          const detail = await api.run(run.id);
          return { employer, run, detail, error: null };
        } catch (err) {
          return { employer, run: null, detail: null, error: errMsg(err) };
        }
      }),
    );
    setFundings(results);
  }, []);

  const initialLoad = useCallback(async () => {
    setLoadError(null);
    try {
      const [me, ov] = await Promise.all([api.me(), api.overview()]);
      setSession(me);
      setOverview(ov);
      await loadAll(ov);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login?next=/dashboard/funding");
        return;
      }
      setLoadError(errMsg(err));
    }
    setLoading(false);
  }, [loadAll, router]);

  useEffect(() => { void initialLoad(); }, [initialLoad]);

  async function refreshAll() {
    setRefreshing(true);
    setNotice(null);
    try {
      const ov = await api.overview();
      setOverview(ov);
      await loadAll(ov);
    } catch (err) {
      setLoadError(errMsg(err));
    } finally {
      setRefreshing(false);
    }
  }

  const shortCount = fundings.filter((f) => f.detail?.assessment && num(f.detail.assessment.shortfall_usd) > 0).length;
  const totalShortfall = fundings.reduce((sum, f) => sum + (f.detail?.assessment ? num(f.detail.assessment.shortfall_usd) : 0), 0);
  const assessedCount = fundings.filter((f) => f.detail?.assessment).length;

  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}

      <main className="mx-auto mt-8 grid max-w-6xl gap-8 px-4 sm:px-6">
        <section className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Funding planner</h1>
            <p className="mt-1 max-w-2xl text-sage-300">
              What each silo must hold before its next run: payroll, payout fees and the platform fee that leave the
              wallet, plus the reserve that must stay. Every figure comes from the run&apos;s stored assessment.
            </p>
          </div>
          <Button variant="outline" onClick={() => void refreshAll()} disabled={refreshing || loading}>
            <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
          </Button>
        </section>

        {loading && <SectionLoading label="Reading balances and assessments…" />}
        {loadError && <SectionError message={loadError} onRetry={() => void initialLoad()} />}
        {notice && <PageNotice message={notice} />}

        {!loading && overview && (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
              <div className="rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4">
                <p className="text-sm font-bold text-sage-400">Employers planned</p>
                <p className="mt-1 font-display text-3xl font-semibold tabular-nums text-sand-100">{overview.employers.length}</p>
                <p className="mt-1 text-xs font-semibold text-sage-500">{assessedCount} with an assessed run</p>
              </div>
              <div className="rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4">
                <p className="text-sm font-bold text-sage-400">Short right now</p>
                <p className={cn("mt-1 font-display text-3xl font-semibold tabular-nums", shortCount > 0 ? "text-coral-300" : "text-sand-100")}>{shortCount}</p>
                <p className="mt-1 text-xs font-semibold text-sage-500">Employers whose silo does not cover the next run plus reserve</p>
              </div>
              <div className="col-span-2 rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4 lg:col-span-1">
                <p className="text-sm font-bold text-sage-400">Total shortfall (USD)</p>
                <p className={cn("mt-1 font-display text-3xl font-semibold tabular-nums", totalShortfall > 0 ? "text-coral-300" : "text-sand-100")}>
                  {formatMoney(totalShortfall.toFixed(2))}
                </p>
                <p className="mt-1 text-xs font-semibold text-sage-500">Sum of shortfall_usd across assessed employers</p>
              </div>
            </div>

            {overview.employers.length === 0 ? (
              <SectionEmpty title="No employers yet" body="Add an employer before planning funding." />
            ) : (
              <div className="grid gap-6">
                {fundings.map((f) => (
                  <FundingCard
                    key={f.employer.id}
                    data={f}
                    onFunded={(msg) => { setNotice(msg); void refreshAll(); }}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
