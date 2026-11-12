"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Check, Pause, RefreshCw, Sunrise } from "lucide-react";

import { PageNotice, SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, ApiError, apiWorkflows, type MorningBriefContractor, type MorningBriefItem } from "@/lib/api";
import { DECIDER_ROLES } from "@/lib/status";
import { cn, formatMoney } from "@/lib/utils";

function briefError(e: unknown): string {
  if (e instanceof ApiError) return e.code ? `${e.message} (${e.code})` : e.message;
  return errMsg(e);
}
const num = (v: string | null | undefined) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};
function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

/**
 * Card action state — derived only from API fields, never edited here:
 *  - shortfall == 0                     -> approval-ready ("Approve payroll")
 *  - PARTIAL + a payable will_pay split -> approval-ready (pays will_pay, holds will_hold)
 *  - PARTIALLY_SETTLED + ready residual -> "Approve remainder"
 *  - shortfall > 0 with nothing payable -> honest cannot-approve state
 * The split itself is computed by policy on the server; there is no
 * priority-order or partial-amount editing in this UI, and no partial-pay
 * configuration (settlement_mode is set per employer via API/seed).
 */
function approvalReady(item: MorningBriefItem): boolean {
  if (num(item.shortfall) <= 0) return true;
  if (item.settlement_mode === "PARTIAL" && (item.will_pay?.length ?? 0) > 0) return true;
  return false;
}

function ContractorList({ title, items, variant, fallbackCurrency }: { title: string; items: MorningBriefContractor[]; variant: "pay" | "hold"; fallbackCurrency: string }) {
  const isPay = variant === "pay";
  return (
    <div className={cn("grid content-start gap-2 rounded-2xl border-2 p-4", isPay ? "border-sage-600/50 bg-sage-500/5" : "border-clay-500/40 bg-coral-500/5")}>
      <p className={cn("flex items-center gap-2 text-sm font-extrabold", isPay ? "text-sage-300" : "text-coral-300")}>
        {isPay ? <Check className="size-4" /> : <Pause className="size-4" />} {title}
        <span className="ml-auto text-xs font-bold text-sage-400">{items.length}</span>
      </p>
      {items.length === 0 ? (
        <p className="text-sm font-semibold text-sage-400">No one in this list.</p>
      ) : (
        <ul className="grid gap-1.5">
          {items.map((c, i) => (
            <li key={`${c.contractor_name}-${i}`} className="flex items-center gap-2 text-sm">
              <span className={cn("grid size-6 shrink-0 place-items-center rounded-full", isPay ? "bg-sage-500/20 text-sage-300" : "bg-coral-500/15 text-coral-300")}>
                {isPay ? <Check className="size-3.5" /> : <Pause className="size-3.5" />}
              </span>
              <span className="min-w-0 flex-1 truncate font-bold text-sand-100">{c.contractor_name}</span>
              <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(c.amount, c.currency || fallbackCurrency)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function BriefCard({ item, canDecide, onApproved }: { item: MorningBriefItem; canDecide: boolean; onApproved: (item: MorningBriefItem) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resolved, setResolved] = useState<string | null>(null);

  const shortfall = num(item.shortfall);
  const fullyFunded = shortfall <= 0;
  const isPartial = item.settlement_mode === "PARTIAL";
  const isPartiallySettled = (item.run_status ?? "").toUpperCase() === "PARTIALLY_SETTLED";
  const ready = approvalReady(item);

  async function approve() {
    setBusy(true); setError(null);
    try {
      // Existing run decision flow — identical to the payroll run screen:
      // POST /runs/{run_id}/decision {decision: "approve"} via api.decide.
      await api.decide(item.run_id, "approve");
      setResolved(isPartiallySettled
        ? "Remainder approved — execution continues in the payroll module"
        : "Approved — execution continues in the payroll module");
      await onApproved(item);
    } catch (e) {
      setError(briefError(e));
    }
    setBusy(false);
  }

  return (
    <Card className="grid gap-4 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-display text-xl font-semibold text-sand-100">{item.employer_name}</h3>
          <p className="mt-0.5 text-sm font-semibold text-sage-400">
            {item.workflow_name} · Pay date {fmtDate(item.pay_date)}
          </p>
          {isPartiallySettled && (
            <p role="status" className="mt-1.5 text-sm font-bold text-sand-200">Partially settled — remainder awaiting funds</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {isPartial && (
            <Badge tone="sand" className="border-sand-400 bg-sand-200 text-ink-900">Partial settlement</Badge>
          )}
          {fullyFunded && <Badge tone="sage" className="border-sage-500 bg-sage-500 text-ink-900">Fully funded</Badge>}
          {item.workflow_run_id && (
            <Button variant="ghost" size="sm" asChild>
              <Link href={`/dashboard/workflows/runs/${item.workflow_run_id}`}>View run</Link>
            </Button>
          )}
        </div>
      </div>

      <dl className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4">
          <dt className="text-xs font-extrabold uppercase tracking-wider text-sage-400">Total needed</dt>
          <dd className="mt-1 font-display text-xl font-semibold tabular-nums text-sand-100">{formatMoney(item.total_required, item.currency)}</dd>
        </div>
        <div className="rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4">
          <dt className="text-xs font-extrabold uppercase tracking-wider text-sage-400">Current balance</dt>
          <dd className="mt-1 font-display text-xl font-semibold tabular-nums text-sand-100">{formatMoney(item.balance, item.currency)}</dd>
        </div>
        <div className={cn("rounded-2xl border-2 p-4", fullyFunded ? "border-sage-600/50 bg-sage-500/5" : "border-coral-600/60 bg-coral-500/10")}>
          <dt className="text-xs font-extrabold uppercase tracking-wider text-sage-400">Shortfall</dt>
          <dd className={cn("mt-1 font-display text-xl font-semibold tabular-nums", fullyFunded ? "text-sage-300" : "text-coral-300")}>
            {fullyFunded ? formatMoney("0", item.currency) : formatMoney(item.shortfall, item.currency)}
          </dd>
        </div>
      </dl>

      <div className="grid gap-3 md:grid-cols-2">
        <ContractorList title="Will be paid" items={item.will_pay ?? []} variant="pay" fallbackCurrency={item.currency} />
        <ContractorList title="Will be held" items={item.will_hold ?? []} variant="hold" fallbackCurrency={item.currency} />
      </div>

      {error && <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">{error}</p>}

      {resolved ? (
        <p role="status" className="rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-4 py-3 text-sm font-bold text-sage-300">{resolved}</p>
      ) : !ready ? (
        <p role="status" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
          Cannot approve — shortfall of {item.currency} {formatMoney(item.shortfall, item.currency).replace(/^[^\d-]+/, "")}. Funding the silo will trigger the rescue workflow.
        </p>
      ) : canDecide ? (
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => void approve()} disabled={busy || !item.run_id}>
            <Check /> {busy ? "Approving…" : isPartiallySettled ? "Approve remainder" : "Approve payroll"}
          </Button>
          {isPartial && !fullyFunded && (
            <span className="text-sm font-semibold text-sage-400">Approving pays the Will be paid list; the rest stays held. The split is computed by policy and cannot be edited here.</span>
          )}
        </div>
      ) : (
        <p className="text-sm font-semibold text-sage-400">Waiting for a decider — owners, admins and approvers can approve this payroll.</p>
      )}
    </Card>
  );
}

export default function MorningBriefPage() {
  const { session } = useSession("/dashboard/workflows/brief");
  const canDecide = !!session && DECIDER_ROLES.includes(session.role);
  const [items, setItems] = useState<MorningBriefItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      setItems(await apiWorkflows.brief());
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setError(briefError(e));
    }
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function onApproved(item: MorningBriefItem) {
    setNotice(`Approved payroll for ${item.employer_name} — execution continues in the payroll module.`);
    await load();
  }

  const today = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });

  return (
    <FebShell
      session={session}
      title="Payroll Morning Brief"
      subtitle={`${today}. Every employer whose payroll is waiting at an approval gate, with the exact split the policy computed — what will be paid, what will be held, and what is missing.`}
      backHref="/dashboard/workflows"
      backLabel="Workflows"
      actions={
        <Button variant="outline" size="sm" onClick={() => { setRefreshing(true); void load().finally(() => setRefreshing(false)); }} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading the morning brief…" />}
      {error && <SectionError message={error} onRetry={() => void load()} />}
      {notice && <PageNotice message={notice} />}

      {items && items.length === 0 && !loading && (
        <SectionEmpty
          title="All clear this morning"
          body="No payroll is waiting for approval. Scheduled workflows will prepare the next brief on their cadence."
          lottie={false}
        />
      )}

      {items && items.length > 0 && (
        <section className="grid gap-5" aria-label="Employers awaiting a payroll decision">
          <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
            <Sunrise className="size-5 text-coral-400" /> Awaiting a gate decision
          </h2>
          {items.map((item) => (
            <BriefCard key={item.workflow_run_id || `${item.employer_id}-${item.run_id}`} item={item} canDecide={canDecide} onApproved={onApproved} />
          ))}
        </section>
      )}
    </FebShell>
  );
}
