"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  Anchor, ArrowUpRight, BookOpen, Download, ExternalLink, FileSearch,
  Info, RefreshCw, ShieldAlert, ShieldCheck,
} from "lucide-react";

import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell, PickerField } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { asStr, pickBool, pickStr, rec } from "@/components/feb/accessors";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/select";
import {
  api, apiV3, v3Downloads,
  type EvidenceBundle, type RunSummary,
} from "@/lib/api";
import { ApiError } from "@/lib/api";
import { Stagger } from "@/lib/gsap-anim";
import { formatMoney } from "@/lib/utils";

function modeOf(bundle: EvidenceBundle): string | null {
  const m = rec(bundle.manifest);
  return pickStr(m, ["mode", "environment", "provider_mode"]) ?? asStr(bundle.manifest);
}

function isSimulator(mode: string | null): boolean {
  return !!mode && /simulat|mock|fake|sandbox/i.test(mode);
}

/** Renders the scalar entries of a backend-owned record as a definition list. */
function RecordGrid({ record, emptyText }: { record: Record<string, unknown> | null; emptyText: string }) {
  if (!record) return <p className="font-semibold text-sage-400">{emptyText}</p>;
  const entries = Object.entries(record).filter(
    ([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean",
  );
  if (entries.length === 0) return <p className="font-semibold text-sage-400">{emptyText}</p>;
  return (
    <dl className="grid gap-2 text-sm sm:grid-cols-2">
      {entries.map(([k, v]) => (
        <div key={k}>
          <dt className="font-bold text-sage-500">{k.replaceAll("_", " ")}</dt>
          <dd className="break-all font-semibold text-sand-200">{String(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function SectionTitle({ children }: { children: string }) {
  return <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">{children}</h2>;
}

export default function EvidencePage() {
  const { session } = useSession("/dashboard/evidence");
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [runId, setRunId] = useState("");
  const [bundle, setBundle] = useState<EvidenceBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [bundleLoading, setBundleLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadRuns = useCallback(async () => {
    setError(null);
    try {
      const list = await api.runs();
      setRuns(list);
      setRunId((cur) => cur || (list[0]?.id ?? ""));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setError(errMsg(e));
    }
    setLoading(false);
  }, []);

  useEffect(() => { void loadRuns(); }, [loadRuns]);

  const loadBundle = useCallback(async (id: string) => {
    if (!id) { setBundle(null); return; }
    setBundleLoading(true);
    setError(null);
    try {
      setBundle(await apiV3.runEvidence(id));
    } catch (e) {
      setBundle(null);
      setError(
        e instanceof ApiError && e.status === 404
          ? "No evidence bundle exists for this run yet. Bundles are produced when a run settles."
          : errMsg(e),
      );
    }
    setBundleLoading(false);
  }, []);

  useEffect(() => { void loadBundle(runId); }, [runId, loadBundle]);

  async function refresh() {
    setRefreshing(true);
    await loadRuns();
    if (runId) await loadBundle(runId);
    setRefreshing(false);
  }

  const mode = bundle ? modeOf(bundle) : null;
  const simulator = isSimulator(mode);
  const runRec = rec(bundle?.run);
  const empRec = rec(bundle?.employer);
  const metalRec = rec(bundle?.metal);
  const tapRec = rec(bundle?.tap);
  const metalHash = pickStr(metalRec, ["tx_hash", "metal_tx_hash", "transaction_hash", "hash"]);
  const metalExplorer = pickStr(metalRec, ["explorer_url", "metal_explorer_url", "explorer"]);
  const metalChain = pickStr(metalRec, ["chain", "network"]);

  return (
    <FebShell
      session={session}
      title="Evidence explorer"
      subtitle="The complete proof bundle for a run: what was approved, what the provider confirmed, what was posted to the books, and what anchors it on chain — including its stated limitations."
      actions={
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading runs…" />}
      {error && !bundle && <SectionError message={error} onRetry={() => void (runId ? loadBundle(runId) : loadRuns())} />}

      {runs && runs.length === 0 && (
        <SectionEmpty title="No payroll runs yet" body="Evidence bundles are produced per run. Start a run from an employer's silo first." />
      )}

      {runs && runs.length > 0 && (
        <>
          <Card className="flex flex-wrap items-end gap-4 p-5">
            <PickerField label="Payroll run">
              <NativeSelect value={runId} onChange={(e) => setRunId(e.target.value)} aria-label="Choose a payroll run">
                {runs.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.employer_name} — {r.period_label ?? "Payroll run"} ({r.status.replaceAll("_", " ")})
                  </option>
                ))}
              </NativeSelect>
            </PickerField>
            {runId && (
              <Button variant="sage" size="sm" asChild>
                <a href={v3Downloads.evidenceBundle(runId)} download>
                  <Download /> Download bundle (JSON)
                </a>
              </Button>
            )}
          </Card>

          {bundleLoading && <SectionLoading label="Loading evidence bundle…" />}

          {bundle && !bundleLoading && (
            <Stagger className="grid gap-8">
              <Card className="p-5 sm:p-6">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="flex items-center gap-2 font-display text-2xl font-semibold text-sand-100">
                      <FileSearch className="size-6 text-sage-400" /> Bundle manifest
                    </h2>
                    <p className="mt-1 text-sm font-semibold text-sage-400">
                      {pickStr(empRec, ["name", "employer_name"]) ?? runs.find((r) => r.id === runId)?.employer_name ?? "Employer"}
                      {" · "}
                      {pickStr(runRec, ["period_label", "status", "id"]) ?? runId}
                    </p>
                  </div>
                  {mode ? (
                    simulator ? (
                      <Badge tone="warning">Mode: Simulator</Badge>
                    ) : (
                      <Badge tone="sage">Mode: {mode}</Badge>
                    )
                  ) : (
                    <Badge tone="neutral">Mode not reported</Badge>
                  )}
                </div>
                {simulator && (
                  <p className="mt-3 rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-4 py-3 text-sm font-bold text-sand-200">
                    This bundle was produced in simulator mode. Provider references and chain anchors below are simulated records, not live Airwallex or on-chain activity.
                  </p>
                )}
                <div className="mt-4">
                  <RecordGrid record={rec(bundle.manifest)} emptyText="The bundle did not include manifest details beyond its mode." />
                </div>
              </Card>

              <section>
                <SectionTitle>Approvals and TAP</SectionTitle>
                {bundle.approvals.length === 0 && !tapRec ? (
                  <Card className="p-5"><p className="font-semibold text-sage-400">No approvals are recorded in this bundle.</p></Card>
                ) : (
                  <div className="grid gap-3">
                    {bundle.approvals.map((a, i) => {
                      const verified = a.verified === true;
                      return (
                        <Card key={i} className="p-5">
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <p className="font-bold text-sand-100">
                              {asStr(a.decided_by_email) ?? asStr(a.decided_by) ?? "Approver"}{" "}
                              <span className="font-semibold text-sage-300">chose</span> {asStr(a.decision) ?? "a decision"}
                            </p>
                            <Badge tone={verified ? "success" : "warning"}>
                              {verified ? <><ShieldCheck className="size-3.5" /> TAP verified</> : <><ShieldAlert className="size-3.5" /> TAP not verified</>}
                            </Badge>
                          </div>
                          <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                            <div>
                              <dt className="font-bold text-sage-500">Kind</dt>
                              <dd className="font-semibold text-sand-200">{asStr(a.kind) ?? "—"}</dd>
                            </div>
                            <div>
                              <dt className="font-bold text-sage-500">Signed by (TAP key)</dt>
                              <dd className="break-all font-semibold text-sand-200">{asStr(a.tap_key_id) ?? "Unsigned — no TAP key recorded"}</dd>
                            </div>
                          </dl>
                        </Card>
                      );
                    })}
                    {tapRec && (
                      <Card className="p-5">
                        <h3 className="mb-3 font-display text-lg font-semibold text-sand-100">TAP verification record</h3>
                        <RecordGrid record={tapRec} emptyText="No TAP record fields were reported." />
                        {pickBool(tapRec, ["verified", "tap_verified"]) === false && (
                          <p className="mt-3 text-sm font-bold text-coral-300">The TAP record in this bundle reports itself as not verified.</p>
                        )}
                      </Card>
                    )}
                  </div>
                )}
              </section>

              <section>
                <SectionTitle>Payments and provider references</SectionTitle>
                {bundle.pay_lines.length === 0 ? (
                  <Card className="p-5"><p className="font-semibold text-sage-400">No pay lines are recorded in this bundle.</p></Card>
                ) : (
                  <Card className="overflow-x-auto">
                    <table className="w-full min-w-[560px] text-left text-sm">
                      <thead>
                        <tr className="border-b-2 border-ink-700 text-sage-400">
                          <th scope="col" className="px-5 py-3 font-extrabold">Beneficiary</th>
                          <th scope="col" className="px-3 py-3 font-extrabold">Amount</th>
                          <th scope="col" className="px-3 py-3 font-extrabold">Status</th>
                          <th scope="col" className="px-3 py-3 font-extrabold">Provider reference</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y-2 divide-ink-700">
                        {bundle.pay_lines.map((p, i) => {
                          const amount = asStr(p.amount) ?? (typeof p.amount === "number" ? String(p.amount) : null);
                          const currency = asStr(p.currency) ?? "USD";
                          const ref = pickStr(rec(p), ["provider_ref", "airwallex_transfer_id", "transfer_id", "provider_reference"]);
                          return (
                            <tr key={i} className="bg-ink-900/30">
                              <td className="px-5 py-3 font-bold text-sand-100">{asStr(p.beneficiary_name) ?? asStr(p.name) ?? "Beneficiary"}</td>
                              <td className="px-3 py-3 font-display font-semibold tabular-nums text-sand-100">{amount ? formatMoney(amount, currency) : "—"}</td>
                              <td className="px-3 py-3 font-semibold text-sage-300">{asStr(p.status) ?? "—"}</td>
                              <td className="max-w-52 truncate px-3 py-3 font-semibold text-sage-300">{ref ?? "Not reported"}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </Card>
                )}
              </section>

              <div className="grid items-start gap-8 lg:grid-cols-2">
                <section>
                  <SectionTitle>Journal links</SectionTitle>
                  <Card className="p-5">
                    {bundle.journal_transaction_ids.length === 0 ? (
                      <p className="font-semibold text-sage-400">No journal transactions are linked to this run in the bundle.</p>
                    ) : (
                      <>
                        <ul className="grid gap-2">
                          {bundle.journal_transaction_ids.map((id) => (
                            <li key={id} className="flex items-center justify-between gap-3 rounded-2xl bg-ink-950/40 px-4 py-2.5">
                              <span className="break-all text-sm font-semibold text-sand-200">{id}</span>
                              <BookOpen className="size-4 shrink-0 text-sage-400" />
                            </li>
                          ))}
                        </ul>
                        <Button variant="ghost" size="sm" asChild className="mt-3">
                          <Link href="/dashboard/journals">Open the journals <ArrowUpRight /></Link>
                        </Button>
                      </>
                    )}
                  </Card>
                </section>

                <section>
                  <SectionTitle>Metal anchor</SectionTitle>
                  <Card className="p-5">
                    {metalRec ? (
                      <>
                        <dl className="grid gap-2 text-sm">
                          <div>
                            <dt className="font-bold text-sage-500">Chain</dt>
                            <dd className="font-semibold text-sand-200">{metalChain ?? "Not reported"}</dd>
                          </div>
                          <div>
                            <dt className="font-bold text-sage-500">Transaction hash</dt>
                            <dd className="break-all font-semibold text-sand-200">{metalHash ?? "Not reported"}</dd>
                          </div>
                        </dl>
                        {metalExplorer && metalHash ? (
                          <a
                            href={metalExplorer} target="_blank" rel="noreferrer"
                            className="mt-3 inline-flex items-center gap-2 font-bold text-sand-100 underline underline-offset-4 hover:text-sand-200"
                          >
                            <Anchor className="size-4" /> View on the block explorer <ExternalLink className="size-4" />
                          </a>
                        ) : (
                          <p className="mt-3 text-xs font-semibold text-sage-500">
                            The bundle does not include a block-explorer URL for this anchor, so no explorer link is shown rather than guessing one.
                          </p>
                        )}
                        <div className="mt-4">
                          <RecordGrid record={metalRec} emptyText="" />
                        </div>
                      </>
                    ) : (
                      <p className="font-semibold text-sage-400">No Metal anchor is recorded in this bundle yet. Anchors are written when a run settles.</p>
                    )}
                  </Card>
                </section>
              </div>

              <section>
                <SectionTitle>Agent events and exceptions</SectionTitle>
                <div className="grid gap-4 lg:grid-cols-2">
                  <Card className="p-5">
                    <h3 className="mb-3 font-display text-lg font-semibold text-sand-100">Agent events ({bundle.agent_events.length})</h3>
                    {bundle.agent_events.length === 0 ? (
                      <p className="font-semibold text-sage-400">No agent events in this bundle.</p>
                    ) : (
                      <ul className="grid max-h-80 gap-2 overflow-y-auto pr-1">
                        {bundle.agent_events.map((e, i) => (
                          <li key={i} className="rounded-2xl bg-ink-900/50 px-3 py-2.5 text-sm text-sand-200">
                            <span className="font-extrabold text-sand-100">{asStr(e.agent) ?? "agent"}</span>
                            {" — "}{asStr(e.message) ?? asStr(e.detail) ?? "Event recorded"}
                          </li>
                        ))}
                      </ul>
                    )}
                  </Card>
                  <Card className="p-5">
                    <h3 className="mb-3 font-display text-lg font-semibold text-sand-100">Exceptions ({bundle.exceptions.length})</h3>
                    {bundle.exceptions.length === 0 ? (
                      <p className="font-semibold text-sage-400">No exceptions are attached to this run.</p>
                    ) : (
                      <ul className="grid gap-2">
                        {bundle.exceptions.map((x, i) => (
                          <li key={i} className="rounded-2xl border-2 border-coral-700/50 bg-coral-500/10 px-3 py-2.5 text-sm font-semibold text-coral-200">
                            {asStr(x.title) ?? asStr(x.type) ?? "Exception"} — {asStr(x.detail) ?? asStr(x.message) ?? "See the exceptions inbox for detail."}
                          </li>
                        ))}
                      </ul>
                    )}
                    <Button variant="ghost" size="sm" asChild className="mt-3">
                      <Link href="/dashboard/exceptions">Open the exceptions inbox <ArrowUpRight /></Link>
                    </Button>
                  </Card>
                </div>
              </section>

              <section>
                <SectionTitle>Limitations</SectionTitle>
                <Card className="border-sand-400/40 p-5">
                  <p className="mb-3 flex items-start gap-2 text-sm font-semibold text-sage-300">
                    <Info className="mt-0.5 size-4 shrink-0" />
                    Stated by the bundle itself, shown verbatim. Read these before treating this bundle as proof of live settlement.
                  </p>
                  {bundle.limitations.length === 0 ? (
                    <p className="font-semibold text-sage-400">The bundle states no limitations.</p>
                  ) : (
                    <ul className="list-disc space-y-1.5 pl-5 text-[15px] leading-relaxed text-sand-200">
                      {bundle.limitations.map((l, i) => <li key={i}>{l}</li>)}
                    </ul>
                  )}
                </Card>
              </section>
            </Stagger>
          )}
        </>
      )}
    </FebShell>
  );
}
