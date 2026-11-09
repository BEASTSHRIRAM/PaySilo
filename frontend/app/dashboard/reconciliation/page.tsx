"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Check, RefreshCw, Scale, Wallet, X } from "lucide-react";

import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { CurrencyDonut, DONUT_PALETTE, MatchedRing } from "@/components/feb/charts";
import { LifecycleStepper, lifecycleFor } from "@/components/feb/stepper";
import { FebShell, PickerField } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { num } from "@/components/feb/accessors";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/select";
import {
  api, apiV3, ApiError,
  type Employer, type ReconCurrency, type Reconciliation,
} from "@/lib/api";
import { Stagger } from "@/lib/gsap-anim";
import { cn, formatMoney } from "@/lib/utils";

const GOOD_STATUS = ["MATCHED", "BALANCED", "RECONCILED", "OK", "SETTLED"];

/**
 * Accountant's identity check, sign-convention tolerant: a silo reconciles
 * when opening + deposits ± refunds + FX buys − FX sells − payout debits
 * − provider fees − platform charges lands on the reported closing cash
 * (refunds are tried both as a signed movement and as a deduction, since
 * either convention is legitimate — the check only fires when neither works).
 */
function identityHolds(c: ReconCurrency): boolean | null {
  const o = num(c.opening_cash), d = num(c.deposits), r = num(c.refunds);
  const b = num(c.fx_buys), s = num(c.fx_sells), p = num(c.payout_debits);
  const f = num(c.provider_fees), pc = num(c.platform_charges), cl = num(c.closing_cash);
  if ([o, d, r, b, s, p, f, pc, cl].some((v) => v === null)) return null;
  const base = (o as number) + (d as number) + (b as number) - (s as number) - (p as number) - (f as number) - (pc as number);
  return Math.abs(base + (r as number) - (cl as number)) <= 0.01 || Math.abs(base - (r as number) - (cl as number)) <= 0.01;
}

function SiloCard({ c }: { c: ReconCurrency }) {
  const good = GOOD_STATUS.includes(c.status.toUpperCase());
  const identity = identityHolds(c);
  const rows: [string, string][] = [
    ["Opening cash", c.opening_cash],
    ["Deposits", c.deposits],
    ["Refunds", c.refunds],
    ["FX buys", c.fx_buys],
    ["FX sells", c.fx_sells],
    ["Payout debits", c.payout_debits],
    ["Provider fees", c.provider_fees],
    ["Platform charges", c.platform_charges],
  ];
  return (
    <Card className={cn("p-5", !good && "border-coral-700/70")}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-display text-xl font-semibold text-sand-100">{c.currency} silo</h3>
        <Badge tone={good ? "success" : "danger"}>{c.status.replaceAll("_", " ")}</Badge>
      </div>
      <dl className="mt-4 grid gap-1.5">
        {rows.map(([label, v]) => (
          <div key={label} className="flex items-baseline justify-between gap-3">
            <dt className="text-sm font-bold text-sage-400">{label}</dt>
            <dd className="font-display font-semibold tabular-nums text-sand-200">{formatMoney(v, c.currency)}</dd>
          </div>
        ))}
        <div className="mt-1.5 flex items-baseline justify-between gap-3 border-t-2 border-ink-600 pt-2.5">
          <dt className="font-extrabold text-sand-100">Closing cash (ledger)</dt>
          <dd className="font-display text-lg font-semibold tabular-nums text-sand-100">{formatMoney(c.closing_cash, c.currency)}</dd>
        </div>
        {c.wallet_available ? (
          <>
            <div className="flex items-baseline justify-between gap-3">
              <dt className="text-sm font-bold text-sage-400">Provider wallet</dt>
              <dd className="font-display font-semibold tabular-nums text-sand-200">{formatMoney(c.wallet_balance ?? "0", c.currency)}</dd>
            </div>
            <div className="flex items-baseline justify-between gap-3">
              <dt className="text-sm font-bold text-sage-400">Variance (ledger − wallet)</dt>
              <dd className={cn("font-display font-semibold tabular-nums", Number(c.variance ?? 0) !== 0 ? "text-coral-300" : "text-sand-200")}>
                {formatMoney(c.variance ?? "0", c.currency)}
              </dd>
            </div>
          </>
        ) : (
          <p className="text-xs font-semibold text-sage-500">Provider wallet not readable: ledger self-check only.</p>
        )}
      </dl>
      {!good && (
        <p className="mt-3 rounded-2xl border-2 border-coral-600/50 bg-coral-500/10 px-3 py-2 text-sm font-bold text-coral-300">
          This silo is not fully reconciled. The variance stays visible until the underlying settlements match.
        </p>
      )}
      {identity === false && (
        <p className="mt-3 rounded-2xl border-2 border-coral-600/50 bg-coral-500/10 px-3 py-2 text-sm font-bold text-coral-300">
          The components above do not sum to the reported closing cash under either refund sign convention. Treat this as a variance and investigate before relying on the closing figure.
        </p>
      )}
    </Card>
  );
}

export default function ReconciliationPage() {
  const { session } = useSession("/dashboard/reconciliation");
  const [employers, setEmployers] = useState<Employer[] | null>(null);
  const [employerId, setEmployerId] = useState("");
  const [data, setData] = useState<Reconciliation | null>(null);
  const [loading, setLoading] = useState(true);
  const [dataLoading, setDataLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadEmployers = useCallback(async () => {
    setError(null);
    try {
      const ov = await api.overview();
      setEmployers(ov.employers);
      setEmployerId((cur) => cur || (ov.employers[0]?.id ?? ""));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setError(errMsg(e));
    }
    setLoading(false);
  }, []);

  useEffect(() => { void loadEmployers(); }, [loadEmployers]);

  const loadData = useCallback(async (id: string) => {
    if (!id) { setData(null); return; }
    setDataLoading(true);
    setError(null);
    try {
      setData(await apiV3.reconciliation(id));
    } catch (e) {
      setData(null);
      setError(errMsg(e));
    }
    setDataLoading(false);
  }, []);

  useEffect(() => { void loadData(employerId); }, [employerId, loadData]);

  async function refresh() {
    setRefreshing(true);
    await loadEmployers();
    if (employerId) await loadData(employerId);
    setRefreshing(false);
  }

  const matchedCount = data?.settlements.filter((s) => s.matched).length ?? 0;
  const unmatchedCount = (data?.settlements.length ?? 0) - matchedCount;

  const donutSlices = useMemo(() => {
    return (data?.currencies ?? [])
      .map((c, i) => ({ label: c.currency, value: Math.abs(num(c.closing_cash) ?? 0), raw: c.closing_cash, color: DONUT_PALETTE[i % DONUT_PALETTE.length] }))
      .filter((s) => s.value > 0);
  }, [data]);

  // Settlement timeline for the most recent run present in this report.
  const latestBatch = useMemo(() => {
    const settlements = data?.settlements ?? [];
    if (settlements.length === 0) return null;
    const lastRunId = settlements[settlements.length - 1].run_id;
    const batch = settlements.filter((s) => s.run_id === lastRunId);
    const probe = batch.find((s) => ["FAILED", "REFUSED", "RETURNED", "REFUNDED"].includes(s.status.toUpperCase())) ?? batch[0];
    return { runId: lastRunId, count: batch.length, lc: lifecycleFor(probe.status) };
  }, [data]);

  const employer = employers?.find((e) => e.id === employerId) ?? null;

  return (
    <FebShell
      session={session}
      title="Reconciliation center"
      subtitle="Per-currency settlement reconciliation for one employer's silo. Variance compares the PaySilo ledger with the employer's provider wallet when it can be read; otherwise this is a ledger self-check only."
      actions={
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading employers…" />}
      {error && <SectionError message={error} onRetry={() => void (employerId ? loadData(employerId) : loadEmployers())} />}

      {employers && employers.length === 0 && (
        <SectionEmpty title="No employers yet" body="Add an employer and run payroll, and its reconciliation will appear here." />
      )}

      {employers && employers.length > 0 && (
        <>
          <Card className="flex flex-wrap items-end gap-4 p-5">
            <PickerField label="Employer">
              <NativeSelect value={employerId} onChange={(e) => setEmployerId(e.target.value)} aria-label="Choose an employer">
                {employers.map((e) => (
                  <option key={e.id} value={e.id}>{e.name} — {e.country}</option>
                ))}
              </NativeSelect>
            </PickerField>
            {employer && (
              <p className="pb-2 text-sm font-semibold text-sage-400">
                Funding currency {employer.funding_currency} · {employer.contractor_count} contractor{employer.contractor_count === 1 ? "" : "s"}
              </p>
            )}
          </Card>

          {dataLoading && <SectionLoading label="Loading reconciliation…" />}

          {data && !dataLoading && (
            <>
              {data.currencies.length === 0 && data.settlements.length === 0 ? (
                <SectionEmpty title="Nothing to reconcile yet" body="This employer has no settlements or currency movements yet. Reconciliation appears after the first settled run." />
              ) : (
                <div className="grid items-start gap-6 xl:grid-cols-[1fr_320px]">
                  <div className="grid gap-6">
                    <div className="grid gap-6 md:grid-cols-2">
                      <Card className="flex flex-col items-center gap-4 p-6">
                        <h2 className="flex items-center gap-2 self-start font-display text-lg font-semibold text-sand-100">
                          <Scale className="size-5 text-sage-400" /> Reconciliation status
                        </h2>
                        <MatchedRing percent={data.matched_percent ?? 0} />
                        {data.matched_percent === null && (
                          <p className="text-sm font-semibold text-sage-400">No settlements to match yet.</p>
                        )}
                        <div className="grid w-full gap-1.5 text-sm">
                          <p className="flex items-center justify-between font-semibold text-sage-300">
                            <span className="inline-flex items-center gap-2"><Check className="size-4 text-mint-400" /> Matched settlements</span>
                            <span className="tabular-nums text-sand-100">{matchedCount}</span>
                          </p>
                          <p className="flex items-center justify-between font-semibold text-sage-300">
                            <span className="inline-flex items-center gap-2"><X className="size-4 text-coral-400" /> Unmatched settlements</span>
                            <span className={cn("tabular-nums", unmatchedCount > 0 ? "font-extrabold text-coral-300" : "text-sand-100")}>{unmatchedCount}</span>
                          </p>
                        </div>
                        {unmatchedCount > 0 && (
                          <p className="w-full rounded-2xl border-2 border-coral-600/50 bg-coral-500/10 px-3 py-2 text-sm font-bold text-coral-300">
                            {unmatchedCount} settlement{unmatchedCount === 1 ? " has" : "s have"} no matching provider record yet.
                          </p>
                        )}
                      </Card>

                      <Card className="flex flex-col items-center gap-4 p-6">
                        <h2 className="self-start font-display text-lg font-semibold text-sand-100">Currency breakdown</h2>
                        {donutSlices.length > 0 ? (
                          <>
                            <CurrencyDonut
                              slices={donutSlices}
                              centerLabel="Currencies"
                              centerValue={`${data.currencies.length} silo${data.currencies.length === 1 ? "" : "s"}`}
                            />
                            <ul className="grid w-full gap-1.5 text-sm">
                              {donutSlices.map((s) => {
                                const total = donutSlices.reduce((a, x) => a + x.value, 0);
                                return (
                                  <li key={s.label} className="flex items-center justify-between gap-2 font-semibold text-sage-300">
                                    <span className="inline-flex items-center gap-2">
                                      <span className="size-2.5 rounded-full" style={{ background: s.color }} aria-hidden /> {s.label}
                                    </span>
                                    <span className="tabular-nums text-sand-100">
                                      {formatMoney(s.raw, s.label)} · {total > 0 ? ((s.value / total) * 100).toFixed(1) : "0.0"}%
                                    </span>
                                  </li>
                                );
                              })}
                            </ul>
                            <p className="w-full text-xs font-semibold text-sage-500">
                              Shares are by absolute closing cash per currency — currencies are never converted or summed together.
                            </p>
                          </>
                        ) : (
                          <p className="font-semibold text-sage-400">No closing balances to chart yet.</p>
                        )}
                      </Card>
                    </div>

                    {latestBatch && (
                      <Card className="p-6">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <h2 className="font-display text-lg font-semibold text-sand-100">Settlement timeline</h2>
                          <Button variant="ghost" size="sm" asChild>
                            <Link href={`/dashboard/runs/${latestBatch.runId}`}>Run {latestBatch.runId.slice(0, 8)}… <ArrowUpRight /></Link>
                          </Button>
                        </div>
                        <p className="mb-5 text-sm font-semibold text-sage-400">
                          Most recent run in this report · {latestBatch.count} settlement{latestBatch.count === 1 ? "" : "s"}
                        </p>
                        <div className="max-w-md">
                          <LifecycleStepper {...latestBatch.lc} />
                        </div>
                      </Card>
                    )}

                    <section>
                      <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">Currency silos</h2>
                      {data.currencies.length === 0 ? (
                        <Card className="p-5"><p className="font-semibold text-sage-400">No currency movements recorded for this employer yet.</p></Card>
                      ) : (
                        <Stagger className="grid gap-4 md:grid-cols-2">
                          {data.currencies.map((c) => <SiloCard key={c.currency} c={c} />)}
                        </Stagger>
                      )}
                    </section>

                    <section>
                      <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">Settlements</h2>
                      {data.settlements.length === 0 ? (
                        <Card className="p-5"><p className="font-semibold text-sage-400">No settlements in this report yet.</p></Card>
                      ) : (
                        <Card className="overflow-x-auto">
                          <table className="w-full min-w-[640px] text-left text-sm">
                            <thead>
                              <tr className="border-b-2 border-ink-700 text-sage-400">
                                <th scope="col" className="px-5 py-3 font-extrabold">Beneficiary</th>
                                <th scope="col" className="px-3 py-3 font-extrabold">Amount</th>
                                <th scope="col" className="px-3 py-3 font-extrabold">Status</th>
                                <th scope="col" className="px-3 py-3 font-extrabold">Provider reference</th>
                                <th scope="col" className="px-3 py-3 font-extrabold">Match</th>
                                <th scope="col" className="px-3 py-3 font-extrabold"><span className="sr-only">Run</span></th>
                              </tr>
                            </thead>
                            <tbody className="divide-y-2 divide-ink-700">
                              {data.settlements.map((s) => (
                                <tr key={`${s.run_id}-${s.pay_line_id}`} className="bg-ink-900/30">
                                  <td className="px-5 py-3 font-bold text-sand-100">{s.beneficiary_name}</td>
                                  <td className="px-3 py-3 font-display font-semibold tabular-nums text-sand-100">{formatMoney(s.amount, s.currency)}</td>
                                  <td className="px-3 py-3"><Badge tone={["PAID", "SETTLED"].includes(s.status.toUpperCase()) ? "success" : s.status.toUpperCase() === "FAILED" ? "danger" : "neutral"}>{s.status.replaceAll("_", " ")}</Badge></td>
                                  <td className="max-w-44 truncate px-3 py-3 font-semibold text-sage-300">{s.provider_ref ?? "Not reported"}</td>
                                  <td className="px-3 py-3">
                                    {s.matched ? (
                                      <Badge tone="success"><Check className="size-3.5" /> Matched</Badge>
                                    ) : (
                                      <Badge tone="danger"><X className="size-3.5" /> Unmatched</Badge>
                                    )}
                                  </td>
                                  <td className="px-3 py-3">
                                    <Link href={`/dashboard/runs/${s.run_id}`} className="inline-flex items-center gap-1 font-bold text-sage-300 hover:text-sand-100">
                                      Run <ArrowUpRight className="size-4" />
                                    </Link>
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </Card>
                      )}
                    </section>
                  </div>

                  <aside className="grid gap-6">
                    <Card className="p-5">
                      <h2 className="mb-4 flex items-center gap-2 font-display text-lg font-semibold text-sand-100">
                        <Wallet className="size-5 text-sage-400" /> Accounts balance
                      </h2>
                      {data.currencies.length === 0 ? (
                        <p className="text-sm font-semibold text-sage-400">No accounts yet.</p>
                      ) : (
                        <ul className="grid gap-2">
                          {data.currencies.map((c) => {
                            const good = GOOD_STATUS.includes(c.status.toUpperCase());
                            return (
                              <li key={c.currency} className="flex items-center justify-between gap-3 rounded-2xl bg-ink-950/40 px-4 py-3">
                                <span>
                                  <span className="block font-extrabold text-sand-100">{c.currency}</span>
                                  <span className={cn("block text-xs font-bold", good ? "text-sage-400" : "text-coral-300")}>{c.status.replaceAll("_", " ")}</span>
                                </span>
                                <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(c.closing_cash, c.currency)}</span>
                              </li>
                            );
                          })}
                        </ul>
                      )}
                      <p className="mt-3 text-xs font-semibold text-sage-500">Closing cash per currency silo, exactly as reported. Balances in different currencies are never added together.</p>
                    </Card>
                  </aside>
                </div>
              )}
            </>
          )}
        </>
      )}
    </FebShell>
  );
}
