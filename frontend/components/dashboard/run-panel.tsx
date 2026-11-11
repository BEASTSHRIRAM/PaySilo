"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Check, CornerDownRight, Loader2, PauseCircle, RotateCw, Wallet } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, ApiError, type RunDetail } from "@/lib/api";
import { notifyDataChanged } from "@/lib/events";
import { explainRunError } from "@/lib/run-errors";
import { runStatusInfo } from "@/lib/status";
import { cn, formatMoney } from "@/lib/utils";

const AGENT_NAMES: Record<string, string> = {
  intake: "Intake", price: "Pricing", assess: "Balance & Policy", approval: "Approval Gate", escalate: "Escalation",
  execute: "FX & Payout", fee: "Fee", reconcile: "Reconciliation", attest: "Attestation",
};

const LEVEL_DOT: Record<string, string> = {
  info: "bg-sage-500", success: "bg-mint-500", warning: "bg-sand-400", error: "bg-coral-500",
};

export function RunPanel({ run, canDecide, canPayHeld = false, onChange }: {
  run: RunDetail; canDecide: boolean; canPayHeld?: boolean; onChange: (r: RunDetail) => void;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState<"approve" | "hold" | "retry" | "payheld" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const a = run.assessment;
  const info = runStatusInfo(run);
  const heldLines = run.pay_lines.filter((l) => l.status === "HELD");
  // "Held until funded" is only true for a real shortfall; a refused or rejected
  // run simply paid nothing, and the error card explains why.
  const heldForFunding = run.status === "HELD_SHORTFALL" || run.status === "PARTIALLY_PAID";
  const runError = explainRunError(run.error);

  async function decide(decision: "approve" | "hold") {
    setBusy(decision);
    setError(null);
    try {
      onChange(await api.decide(run.id, decision));
      notifyDataChanged({ runId: run.id });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not record the decision.");
    } finally {
      setBusy(null);
    }
  }

  async function payHeld() {
    setBusy("payheld");
    setError(null);
    try {
      const next = await api.payHeld(run.id);
      notifyDataChanged({ runId: next.id });
      router.push(`/dashboard/runs/${next.id}`); // the new run is waiting for approval
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the continuation run.");
      setBusy(null);
    }
  }

  async function retry() {
    setBusy("retry");
    setError(null);
    try {
      onChange(await api.retryRun(run.id));
      notifyDataChanged({ runId: run.id });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not retry the run.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-sm font-bold text-sage-400">Payroll run</p>
          <h3 className="font-display text-2xl font-semibold text-sand-100">{run.employer_name}</h3>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {run.parent_run_id && <Badge tone="sage">Continuation</Badge>}
          <Badge tone={info.tone}>{info.label}</Badge>
        </div>
      </div>
      {run.parent_run_id && (
        <p className="-mt-2 flex items-center gap-2 text-sm font-bold text-sage-300">
          <CornerDownRight className="size-4 shrink-0" aria-hidden />
          <span>
            Continuation of run{" "}
            <Link href={`/dashboard/runs/${run.parent_run_id}`} className="text-sand-100 underline underline-offset-4 hover:text-sage-300">
              #{run.parent_run_id.slice(0, 8)}
            </Link>
            . Pays only the lines that run held.
          </span>
        </p>
      )}
      {info.detail && (
        <p role="status" className="rounded-3xl border-2 border-sand-400/50 bg-sand-300/10 p-4 text-sm font-bold text-sand-200">{info.detail}</p>
      )}

      {run.explanation && <p className="rounded-3xl bg-ink-950/40 p-4 text-[15px] leading-relaxed text-sand-200">{run.explanation}</p>}
      {runError && (
        <div role="alert" className="rounded-3xl border-2 border-coral-600/50 bg-coral-500/10 p-4 text-sm">
          <p className="font-extrabold text-coral-300">{runError.title}</p>
          <p className="mt-1 font-semibold leading-relaxed text-sand-200">{runError.message}</p>
          {runError.hint && <p className="mt-2 font-semibold text-sage-300">{runError.hint}</p>}
          {runError.technical !== runError.message && (
            <details className="mt-3 text-xs text-sage-400">
              <summary className="cursor-pointer font-bold text-sage-300">Technical details</summary>
              <p className="mt-1 break-words font-mono">{runError.technical}</p>
            </details>
          )}
          {run.status === "FAILED" && canDecide && (
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <Button size="sm" disabled={!!busy} onClick={() => void retry()}>
                {busy === "retry" ? <Loader2 className="animate-spin" /> : <RotateCw />} Retry failed payouts
              </Button>
              <span className="text-xs font-semibold text-sage-400">Payouts already sent are skipped — nobody is paid twice.</span>
            </div>
          )}
          {run.status === "FAILED" && error && <p className="mt-2 text-sm font-bold text-coral-300">{error}</p>}
        </div>
      )}

      {a && (
        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          {[
            ["Payroll + fees", a.payroll_currency === "MIXED" ? formatMoney(a.payroll_usd) : formatMoney(a.total_required, a.payroll_currency)],
            ["Leaves wallet", formatMoney(a.required_usd)],
            ["Reserve kept", formatMoney(a.reserve_floor_usd)],
            [a.can_run ? "Available" : "Short by", a.can_run ? formatMoney(a.available_usd) : formatMoney(a.shortfall_usd)],
          ].map(([k, v]) => (
            <div key={k} className="rounded-2xl border-2 border-ink-700 bg-ink-900/60 p-3">
              <dt className="font-bold text-sage-500">{k}</dt>
              <dd className="mt-0.5 font-display text-lg font-semibold tabular-nums text-sand-100">{v}</dd>
            </div>
          ))}
        </dl>
      )}

      {heldForFunding && heldLines.length > 0 && (
        <div className="grid gap-3 rounded-3xl border-2 border-sand-400/50 bg-sand-300/10 p-4 text-sm font-bold text-sand-200">
          <p>
            {heldLines.length} of {run.pay_lines.length} line{run.pay_lines.length === 1 ? "" : "s"} held, not paid:{" "}
            {heldLines.map((l) => `${l.beneficiary_name} (${formatMoney(l.amount, l.currency)})`).join(", ")}.
            Held lines move only after the employer is funded and a person approves again.
          </p>
          {run.continuation_run_id && (
            <p className="font-semibold text-sage-300">
              {run.can_pay_held ? "Last attempt in run" : "Held lines continued in run"}{" "}
              <Link href={`/dashboard/runs/${run.continuation_run_id}`} className="font-extrabold text-sand-100 underline underline-offset-4 hover:text-sage-300">
                #{run.continuation_run_id.slice(0, 8)}
              </Link>
              .
            </p>
          )}
          {run.can_pay_held && canPayHeld && (
            <div className="grid gap-2">
              <div className="flex flex-wrap items-center gap-3">
                <Button disabled={!!busy} onClick={() => void payHeld()}>
                  {busy === "payheld" ? <Loader2 className="animate-spin" /> : <Wallet />} Pay held lines
                </Button>
                <Button variant="outline" asChild>
                  <Link href={`/dashboard/employers/${run.employer_id}`}>Add funds first</Link>
                </Button>
              </div>
              <p className="font-semibold leading-relaxed text-sage-300">
                Pays only the held contractors from this employer&apos;s own wallet. Anyone already paid is not paid again.
                {run.run_fee_collected
                  ? " The run fee was already collected, so it is not charged again."
                  : " The run fee has not been collected yet; it is charged when this payroll runs."}
              </p>
            </div>
          )}
          {error && !run.pending && <p role="alert" className="font-bold text-coral-300">{error}</p>}
        </div>
      )}

      {run.pay_lines.length > 0 && (
        <ul className="divide-y-2 divide-ink-700 overflow-hidden rounded-3xl border-2 border-ink-700">
          {run.pay_lines.map((l) => (
            <li key={l.position} className={cn("flex items-center justify-between gap-3 px-4 py-3", l.status === "HELD" ? "bg-sand-300/10" : "bg-ink-900/40")}>
              <div>
                <p className="font-bold text-sand-100">{l.beneficiary_name}</p>
                <p className="text-xs font-semibold text-sage-500">
                  {l.method}{l.payout_fee ? ` · fee ${formatMoney(l.payout_fee, l.currency)}` : ""}
                  {l.airwallex_transfer_id ? ` · ${l.airwallex_transfer_id}` : ""}
                </p>
              </div>
              <div className="text-right">
                <p className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(l.amount, l.currency)}</p>
                <Badge tone={l.status === "PAID" ? "success" : l.status === "HELD" ? "warning" : l.status === "FAILED" ? "danger" : "neutral"}>{l.status}</Badge>
              </div>
            </li>
          ))}
        </ul>
      )}

      {run.pending && (
        <div className={cn("rounded-3xl border-2 p-4", run.pending.kind === "approval" ? "border-sage-600/60 bg-sage-500/10" : "border-sand-400/50 bg-sand-300/10")}>
          <p className="font-bold text-sand-100">{run.pending.message}</p>
          {run.pending.run_fee_waived && (
            <p className="mt-2 text-sm font-semibold text-sage-300">
              Continuation: the run fee was already collected on the original payroll, so none is charged here.
            </p>
          )}
          {!run.resumable && <p className="mt-2 text-sm font-semibold text-coral-300">The server restarted while this run was paused. Start a new run.</p>}
          {error && <p className="mt-2 text-sm font-bold text-coral-300">{error}</p>}
          {canDecide && run.resumable && (
            <div className="mt-4 flex flex-wrap gap-3">
              {run.pending.kind === "approval" && (
                <Button variant="success" disabled={!!busy} onClick={() => void decide("approve")}>
                  {busy === "approve" ? <Loader2 className="animate-spin" /> : <Check />} Approve & sign
                </Button>
              )}
              <Button variant="outline" disabled={!!busy} onClick={() => void decide("hold")}>
                {busy === "hold" ? <Loader2 className="animate-spin" /> : <PauseCircle />} Hold
              </Button>
            </div>
          )}
        </div>
      )}

      {run.approvals.length > 0 && (
        <div className="rounded-3xl bg-ink-950/40 p-4 text-sm">
          {run.approvals.map((ap, i) => (
            <p key={i} className="font-semibold text-sage-300">
              <span className="font-extrabold text-sand-100">{ap.decided_by_email}</span> chose <span className="font-extrabold">{ap.decision}</span>
              {ap.tap_key_id && <> · Visa TAP {ap.verified ? "verified" : "not verified"} <span className="break-all text-sage-500">({ap.tap_key_id})</span></>}
            </p>
          ))}
          {run.receipt && (
            <p className="mt-2 break-all font-semibold text-sage-400">
              Receipt anchored ({run.receipt.chain} #{run.receipt.chain_index}): <span className="text-sand-200">{run.receipt.receipt_hash.slice(0, 24)}…</span>
              {run.receipt.explorer_url && run.receipt.tx_hash && (
                <> · <a className="text-sand-200 underline" href={run.receipt.explorer_url} target="_blank" rel="noreferrer">View transaction {run.receipt.tx_hash.slice(0, 10)}…</a></>
              )}
            </p>
          )}
        </div>
      )}

      <div>
        <p className="mb-3 text-sm font-extrabold uppercase tracking-widest text-sage-500">Agent timeline</p>
        <ol className="grid gap-2">
          {run.events.map((e) => (
            <li key={e.seq} className="flex gap-3 rounded-2xl bg-ink-900/50 px-3 py-2.5">
              <span className={cn("mt-1.5 size-2.5 shrink-0 rounded-full", LEVEL_DOT[e.level] ?? "bg-sage-500")} />
              <p className="text-sm leading-relaxed text-sand-200">
                <span className="font-extrabold text-sand-100">{AGENT_NAMES[e.agent] ?? e.agent}</span> — {e.message}
              </p>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
