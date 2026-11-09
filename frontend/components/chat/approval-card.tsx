"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Check, Loader2, PauseCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, ApiError, type ChatApproval, type RunDetail } from "@/lib/api";
import { notifyDataChanged, useDataChanged } from "@/lib/events";
import { runStatusInfo } from "@/lib/status";
import { explainRunError } from "@/lib/run-errors";

export type ApprovalCardState = ChatApproval;

const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

/**
 * Inline approval. The chat never approves anything itself: the card shows
 * the run's real, current state, and only offers Approve & sign / Hold to a
 * person whose role may decide, while the run is still paused and resumable.
 */
export function ApprovalCard({ card, canDecide }: { card: ApprovalCardState; canDecide: boolean }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"approve" | "hold" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const hasRun = Boolean(card.run_id);

  const load = useCallback(async () => {
    if (!card.run_id) return;
    try {
      setRun(await api.run(card.run_id));
      setLoadError(null);
    } catch (err) {
      setLoadError(errMsg(err));
    }
  }, [card.run_id]);

  useEffect(() => {
    void load();
  }, [load]);
  useDataChanged(() => void load());

  async function decide(decision: "approve" | "hold") {
    setBusy(decision);
    setError(null);
    try {
      // The human clicks; the decision goes through the same endpoint as the run panel.
      const result = await api.decide(card.run_id, decision);
      setRun(result);
      notifyDataChanged({ runId: card.run_id });
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(null);
    }
  }

  const title = `Approval needed${card.employer_name ? ` · ${card.employer_name}` : ""}`;

  if (!hasRun) {
    return (
      <div className="grid gap-1 rounded-2xl border-2 border-ink-600 bg-ink-950/40 px-3 py-3">
        <p className="text-sm font-bold text-sand-100">{title}</p>
        <p className="text-sm text-sage-300">This approval has no run attached, so it can't be actioned here. Open the run from the dashboard.</p>
      </div>
    );
  }

  const pending = run?.pending ?? null;
  const actionable = !!run && !!pending && run.resumable;
  const info = run ? runStatusInfo(run) : null;

  return (
    <div className="grid gap-2 rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-3 py-3">
      <p className="text-sm font-bold text-sand-100">{title}</p>
      {card.summary && <p className="text-sm leading-snug text-sand-200">{card.summary}</p>}

      {!run && !loadError && (
        <p className="flex items-center gap-2 text-sm font-semibold text-sage-300" role="status">
          <Loader2 className="size-4 animate-spin" aria-hidden /> Checking the run…
        </p>
      )}
      {loadError && (
        <p className="text-sm font-bold text-coral-300" role="alert">
          {loadError}{" "}
          <button type="button" onClick={() => void load()} className="underline">Retry</button>
        </p>
      )}

      {run && actionable && canDecide && pending && (
        <div className="flex flex-wrap gap-2">
          {pending.kind === "approval" && (
            <Button type="button" variant="success" size="sm" disabled={!!busy} onClick={() => void decide("approve")}>
              {busy === "approve" ? <Loader2 className="animate-spin" /> : <Check />} Approve &amp; sign
            </Button>
          )}
          <Button type="button" variant="outline" size="sm" disabled={!!busy} onClick={() => void decide("hold")}>
            {busy === "hold" ? <Loader2 className="animate-spin" /> : <PauseCircle />} Hold
          </Button>
        </div>
      )}
      {run && actionable && !canDecide && (
        <p className="text-sm font-bold text-sand-200" role="status">Ask an approver. Your role can't approve or hold payroll.</p>
      )}
      {run && !actionable && info && (
        <div className="grid gap-1" role="status">
          <p className="flex flex-wrap items-center gap-2 text-sm font-bold text-sage-200">
            {pending && !run.resumable ? "This run can't be resumed. Start a new run." : "Current status:"}
            {!pending || run.resumable ? <Badge tone={info.tone}>{info.label}</Badge> : null}
          </p>
          {info.detail && <p className="text-sm leading-snug text-sand-300">{info.detail}</p>}
          {run.error && (
            <p className="text-sm font-semibold text-coral-300" title={run.error}>
              {explainRunError(run.error)?.title}: {explainRunError(run.error)?.message}
            </p>
          )}
        </div>
      )}
      {run && (
        <Link href={`/dashboard/runs/${run.id}`} className="w-fit text-sm font-extrabold text-sage-300 underline underline-offset-4 hover:text-sand-100">
          Open run
        </Link>
      )}
      {error && (
        <p className="text-sm font-bold text-coral-300" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
