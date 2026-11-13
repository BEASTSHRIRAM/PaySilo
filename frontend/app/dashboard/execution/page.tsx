"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  Anchor, ArrowUpRight, Check, CircleDot, ClipboardCheck, Copy, Download, FileText, Landmark, Loader2,
  RefreshCw, ShieldCheck, X,
} from "lucide-react";

import { AgentFlow } from "@/components/dashboard/agent-flow";
import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { LifecycleStepper, lifecycleFor } from "@/components/feb/stepper";
import { PaymentTracePanel } from "@/components/feb/payment-trace";
import { FebShell, PickerField } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { pickBool, pickStr, rec } from "@/components/feb/accessors";
import { Badge, toneForStatus } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/select";
import {
  api, apiV3, ApiError, proofDownloads,
  type EvidenceBundle, type RunDetail, type RunSummary,
} from "@/lib/api";
import { Stagger } from "@/lib/gsap-anim";
import { isMoneyMoved, TERMINAL_RUN_STATUSES } from "@/lib/status";
import { cn, formatMoney } from "@/lib/utils";

type CheckState = "done" | "missing" | "pending" | "unknown";
interface CheckItem { label: string; detail: string; state: CheckState }

function CheckRow({ item }: { item: CheckItem }) {
  return (
    <li className="flex items-start gap-3 rounded-2xl bg-ink-900/50 px-3 py-2.5">
      <span
        className={cn(
          "mt-0.5 grid size-6 shrink-0 place-items-center rounded-full border-2",
          item.state === "done" && "border-mint-500 bg-mint-500/15 text-mint-400",
          item.state === "missing" && "border-coral-500 bg-coral-500/15 text-coral-300",
          item.state === "pending" && "border-sand-400/60 bg-sand-300/10 text-sand-300",
          item.state === "unknown" && "border-ink-600 bg-ink-800 text-sage-500",
        )}
        aria-hidden
      >
        {item.state === "done" ? <Check className="size-3.5" /> : item.state === "missing" ? <X className="size-3.5" /> : <CircleDot className="size-3.5" />}
      </span>
      <div className="min-w-0">
        <p className="text-sm font-extrabold text-sand-100">{item.label}</p>
        <p className="break-words text-xs font-semibold text-sage-400">{item.detail}</p>
      </div>
      <span className="ml-auto shrink-0 pt-0.5">
        <Badge tone={item.state === "done" ? "success" : item.state === "missing" ? "danger" : item.state === "pending" ? "warning" : "neutral"}>
          {item.state === "done" ? "Verified" : item.state === "missing" ? "Missing" : item.state === "pending" ? "Pending" : "No data"}
        </Badge>
      </span>
    </li>
  );
}


export default function ExecutionPage() {
  const { session } = useSession("/dashboard/execution");
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [runId, setRunId] = useState("");
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [evidence, setEvidence] = useState<EvidenceBundle | null>(null);
  const [evidenceNote, setEvidenceNote] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [linkNote, setLinkNote] = useState<string | null>(null);
  const detailSeq = useRef(0); // guards against out-of-order responses when switching runs quickly

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

  const loadDetail = useCallback(async (id: string, opts: { quiet?: boolean } = {}) => {
    const seq = ++detailSeq.current;
    const stale = () => seq !== detailSeq.current;
    if (!id) { setDetail(null); setEvidence(null); return; }
    if (!opts.quiet) { setDetailLoading(true); setEvidenceNote(null); }
    try {
      const d = await api.run(id);
      if (stale()) return;
      setDetail(d);
    } catch (e) {
      if (stale()) return;
      setError(errMsg(e));
      setDetailLoading(false);
      return;
    }
    // Evidence feeds the verification checklist; its absence must not fail the page.
    try {
      const ev = await apiV3.runEvidence(id);
      if (stale()) return;
      setEvidence(ev);
    } catch (e) {
      if (stale()) return;
      setEvidence(null);
      setEvidenceNote(
        e instanceof ApiError && e.status === 404
          ? "No evidence bundle for this run yet, so checklist items that depend on it show as “No data” rather than a guess."
          : "The evidence bundle could not be loaded, so checklist items that depend on it show as “No data” rather than a guess.",
      );
    }
    setDetailLoading(false);
  }, []);

  useEffect(() => { setSelected(0); setLinkNote(null); setDetail(null); void loadDetail(runId); }, [runId, loadDetail]);

  // Poll while the run is in flight (not terminal and not waiting on a human).
  const polling = !!detail && !TERMINAL_RUN_STATUSES.has(detail.status) && !detail.pending;
  useEffect(() => {
    if (!polling || !runId) return;
    const t = setInterval(() => { void loadDetail(runId, { quiet: true }); }, 4000);
    return () => clearInterval(t);
  }, [polling, runId, loadDetail]);

  async function copyReceiptLink(payLineId: string) {
    setLinkNote(null);
    try {
      const link = await apiV3.receiptLink(runId, payLineId);
      const url = `${window.location.origin}${link.path}`;
      await navigator.clipboard.writeText(url);
      setLinkNote("Receipt link copied. It expires in 30 days.");
    } catch (e) {
      setLinkNote(`Could not copy the receipt link: ${errMsg(e)}`);
    }
  }

  async function refresh() {
    setRefreshing(true);
    await loadRuns();
    if (runId) await loadDetail(runId);
    setRefreshing(false);
  }

  const line = detail?.pay_lines[selected] ?? null;

  const checklist: CheckItem[] = useMemo(() => {
    if (!detail || !line) return [];
    const settled = isMoneyMoved(detail.status);
    const evRun = rec(evidence?.run);
    const evLine = evidence?.pay_lines.find((p) => {
      const pos = p.position;
      return typeof pos === "number" && pos === line.position;
    }) ?? evidence?.pay_lines[selected] ?? null;
    const evLineRec = rec(evLine);

    // 1 — Funding confirmed
    const fundingFlag = pickBool(evRun, ["funding_confirmed", "funded", "funding_verified"]);
    const assessOk = detail.events.some((e) => e.agent === "assess" && e.level === "success");
    const funding: CheckState =
      fundingFlag === true || settled || assessOk || (detail.assessment?.can_run ?? false)
        ? "done"
        : detail.status === "HELD_SHORTFALL" || fundingFlag === false
          ? "missing"
          : "pending";

    // 2 — TAP approval verified
    const tapRec = rec(evidence?.tap);
    const tapFlag =
      pickBool(tapRec, ["verified", "tap_verified"]) ??
      (evidence ? evidence.approvals.some((a) => a.verified === true) : null) ??
      (detail.approvals.length > 0 ? detail.approvals.some((a) => a.verified) : null);
    const tap: CheckState = tapFlag === true ? "done" : detail.approvals.length > 0 || evidence?.approvals.length ? "missing" : settled ? "missing" : "pending";

    // 3 — Provider reference present (per pay line)
    const providerRef =
      line.airwallex_transfer_id ??
      pickStr(evLineRec, ["provider_ref", "airwallex_transfer_id", "transfer_id", "provider_reference"]);
    const provider: CheckState = providerRef ? "done" : settled ? "missing" : "pending";

    // 4 — Journal posted
    const journalIds = evidence?.journal_transaction_ids ?? [];
    const journal: CheckState = !evidence ? "unknown" : journalIds.length > 0 ? "done" : settled ? "missing" : "pending";

    // 5 — Metal anchor
    const metalRec = rec(evidence?.metal) ?? rec(detail.receipt as unknown);
    const metalHash = pickStr(metalRec, ["tx_hash", "metal_tx_hash", "transaction_hash"]) ?? detail.receipt?.tx_hash ?? null;
    const mode = pickStr(rec(evidence?.manifest), ["mode", "environment"]);
    const metal: CheckState = !evidence && !detail.receipt ? "unknown" : metalHash ? "done" : settled ? "missing" : "pending";

    return [
      { label: "Funding confirmed", state: funding, detail: funding === "done" ? "The employer's silo covered this run before execution." : funding === "missing" ? "The run was held for a shortfall — funding is not confirmed." : "Waiting on the balance and policy check." },
      { label: "Signed approval verified", state: tap, detail: tap === "done" ? "A human approval carries a verified signature." : tap === "missing" ? "An approval exists but no verified signature was reported." : "No approval has been recorded for this run yet." },
      { label: "Provider reference present", state: provider, detail: providerRef ? `Airwallex reference ${providerRef}` : "No provider transfer reference reported for this payment yet." },
      { label: "Journal posted", state: journal, detail: journal === "done" ? `${journalIds.length} journal transaction${journalIds.length === 1 ? "" : "s"} linked to this run.` : journal === "unknown" ? "Needs the evidence bundle, which is not available for this run." : journal === "missing" ? "The run settled but no journal transaction is linked in the evidence bundle." : "The journal entry posts when the run settles." },
      { label: "Metal anchor", state: metal, detail: metalHash ? `Anchored on chain: ${metalHash.slice(0, 18)}…` : mode ? `No anchor hash reported yet (mode: ${mode}).` : "No anchor hash reported for this run yet." },
    ];
  }, [detail, line, evidence, selected]);

  const stats = useMemo(() => {
    const lines = detail?.pay_lines ?? [];
    return {
      total: lines.length,
      settled: lines.filter((l) => ["PAID", "SETTLED", "COMPLETED"].includes(l.status.toUpperCase())).length,
      inflight: lines.filter((l) => ["SENT", "PROCESSING", "PROCESSED", "PENDING", "QUEUED"].includes(l.status.toUpperCase())).length,
      failed: lines.filter((l) => ["FAILED", "REFUSED", "RETURNED", "REFUNDED"].includes(l.status.toUpperCase())).length,
    };
  }, [detail]);

  const runSummary = runs?.find((r) => r.id === runId) ?? null;

  return (
    <FebShell
      session={session}
      title="Execution monitor"
      subtitle="Follow every payment in a run from initiation to settlement, with the verification checks that prove it happened."
      actions={
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading runs…" />}
      {error && <SectionError message={error} onRetry={() => void loadRuns()} />}

      {runs && runs.length === 0 && (
        <SectionEmpty title="No payroll runs yet" body="Start a run from an employer's silo and its payments will appear here with their lifecycle and checks." />
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
            {runSummary && (
              <div className="flex flex-wrap items-center gap-2 pb-1">
                <Badge tone={toneForStatus(runSummary.status)}>{runSummary.status.replaceAll("_", " ")}</Badge>
                <span className="text-sm font-semibold text-sage-400">
                  Started {new Date(runSummary.created_at).toLocaleString()}
                  {runSummary.settled_at ? ` · settled ${new Date(runSummary.settled_at).toLocaleString()}` : ""}
                </span>
                <Button variant="ghost" size="sm" asChild>
                  <Link href={`/dashboard/runs/${runSummary.id}`}>Open run <ArrowUpRight /></Link>
                </Button>
                <Button variant="sand" size="sm" asChild>
                  <a href={proofDownloads.statement(runSummary.id)} download><FileText /> Download statement (PDF)</a>
                </Button>
              </div>
            )}
          </Card>

          {detailLoading && <SectionLoading label="Loading payments…" />}

          {detail && !detailLoading && (
            <>
              <Stagger className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                {[
                  { label: "Payments in run", value: stats.total, icon: Landmark },
                  { label: "Settled", value: stats.settled, icon: ShieldCheck },
                  { label: "In flight", value: stats.inflight, icon: Loader2 },
                  { label: "Failed or returned", value: stats.failed, icon: ClipboardCheck },
                ].map(({ label, value, icon: Icon }) => (
                  <div key={label} className="rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4">
                    <p className="flex items-center gap-2 text-sm font-bold text-sage-400"><Icon className="size-4" /> {label}</p>
                    <p className="mt-1 font-display text-3xl font-semibold tabular-nums text-sand-100">{value}</p>
                  </div>
                ))}
              </Stagger>

              {evidenceNote && (
                <p role="note" className="rounded-2xl border-2 border-sand-400/40 bg-sand-300/5 px-4 py-3 text-sm font-semibold text-sand-200">{evidenceNote}</p>
              )}

              <div className="grid items-start gap-6 lg:grid-cols-[1.5fr_1fr]">
                <Card className="overflow-hidden">
                  <div className="border-b-2 border-ink-700 px-5 py-4">
                    <h2 className="font-display text-xl font-semibold text-sand-100">Payments</h2>
                    <p className="text-sm font-semibold text-sage-400">Select a payment to see its lifecycle and verification checklist.</p>
                  </div>
                  {detail.pay_lines.length === 0 ? (
                    <p className="p-5 font-semibold text-sage-400">This run has no pay lines yet.</p>
                  ) : (
                    <ul className="divide-y-2 divide-ink-700">
                      {detail.pay_lines.map((l, i) => {
                        const lc = lifecycleFor(l.status, detail.status);
                        const active = i === selected;
                        return (
                          <li key={l.position}>
                            <button
                              type="button"
                              onClick={() => setSelected(i)}
                              aria-pressed={active}
                              className={cn("grid w-full gap-3 px-5 py-4 text-left transition-colors", active ? "bg-ink-800/80" : "bg-ink-900/30 hover:bg-ink-800/50")}
                            >
                              <span className="flex flex-wrap items-center justify-between gap-2">
                                <span className="font-bold text-sand-100">{l.beneficiary_name}</span>
                                <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(l.amount, l.currency)}</span>
                              </span>
                              <span className="flex flex-wrap items-center gap-2 text-xs font-semibold text-sage-400">
                                <span>{l.method}</span>
                                {l.payout_fee ? <span>· fee {formatMoney(l.payout_fee, l.currency)}</span> : null}
                                <Badge tone={l.status === "PAID" ? "success" : l.status === "FAILED" ? "danger" : l.status === "HELD" ? "warning" : "neutral"}>{l.status}</Badge>
                              </span>
                              <LifecycleStepper {...lc} compact />
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </Card>

                <div className="grid gap-6">
                  {line && (
                    <Card className="p-5">
                      <h2 className="font-display text-xl font-semibold text-sand-100">{line.beneficiary_name}</h2>
                      <p className="mt-0.5 text-sm font-semibold text-sage-400">
                        {formatMoney(line.amount, line.currency)} · {line.method}
                        {line.airwallex_transfer_id ? ` · ${line.airwallex_transfer_id}` : ""}
                      </p>
                      <div className="mt-3 flex flex-wrap items-center gap-3">
                        <Button variant="outline" size="sm" onClick={() => void copyReceiptLink(line.id)}>
                          <Copy /> Copy receipt link
                        </Button>
                        <Button variant={line.status === "PAID" ? "success" : "sand"} size="sm" asChild>
                          <a href={proofDownloads.line(detail.id, line.id)} download>
                            {line.status === "PAID" ? <Download /> : <FileText />}
                            {line.status === "PAID" ? "Download proof" : "Download status notice"}
                          </a>
                        </Button>
                        {linkNote && <span role="status" className="text-xs font-semibold text-sage-400">{linkNote}</span>}
                      </div>
                      <PaymentTracePanel key={line.id} runId={detail.id} lineId={line.id} />
                      <div className="mt-5">
                        <LifecycleStepper {...lifecycleFor(line.status, detail.status)} />
                      </div>
                      <h3 className="mb-3 mt-6 flex items-center gap-2 text-sm font-extrabold text-sage-300">
                        <ClipboardCheck className="size-4" /> Verification checklist
                      </h3>
                      <ul className="grid gap-2">
                        {checklist.map((item) => <CheckRow key={item.label} item={item} />)}
                      </ul>
                      <p className="mt-3 flex items-start gap-2 text-xs font-semibold text-sage-500">
                        <Anchor className="mt-0.5 size-3.5 shrink-0" />
                        Checklist sources: run assessment, signed approvals, provider references, and the run's evidence bundle. Anything not reported shows as pending or no data — never as verified.
                      </p>
                    </Card>
                  )}

                  <Card className="p-5">
                    <h2 className="mb-1 font-display text-xl font-semibold text-sand-100">Attempt history</h2>
                    <p className="mb-4 text-sm font-semibold text-sage-400">Every agent attempt and decision recorded for this run, in order.</p>
                    {detail.events.length === 0 ? (
                      <p className="font-semibold text-sage-400">No agent events recorded yet.</p>
                    ) : (
                      <AgentFlow
                        events={detail.events}
                        status={detail.status}
                        pendingKind={detail.pending?.kind ?? null}
                        approvalHref={`/dashboard/runs/${detail.id}#approval-controls`}
                      />
                    )}
                  </Card>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </FebShell>
  );
}
