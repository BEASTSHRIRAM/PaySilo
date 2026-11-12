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
import { api, ApiError, apiWorkflows, type MorningBriefLine, type MorningBriefItem } from "@/lib/api";
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

const MODE_LABEL: Record<string, string> = { ALL_OR_NOTHING: "All or nothing", PARTIAL: "Partial" };
const RUN_STATUS_LABEL: Record<string, string> = {
  AWAITING_APPROVAL: "Waiting for approval",
  AWAITING_ESCALATION: "Waiting for a decision",
  PARTIALLY_PAID: "Partly paid, lines held",
  HELD: "Held",
};
const humanStatus = (s: string) => RUN_STATUS_LABEL[s] ?? s.replaceAll("_", " ").toLowerCase();

function ContractorList({ title, items, variant, fallbackCurrency }: { title: string; items: MorningBriefLine[]; variant: "pay" | "hold"; fallbackCurrency: string }) {
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
              <span className="min-w-0 flex-1 font-bold text-sand-100">
                <span className="block truncate">{c.contractor_name}</span>
                {!isPay && (
                  <span className="block text-xs font-semibold text-sage-300">{c.reason ?? "Held until the wallet can cover it."}</span>
                )}
              </span>
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
  const currency = item.currency ?? "USD";
  const modeLabel = item.settlement_mode ? (MODE_LABEL[item.settlement_mode] ?? item.settlement_mode) : null;

  async function approve() {
    setBusy(true); setError(null);
    try {
      // Existing run decision flow, identical to the payroll run screen:
      // POST /runs/{run_id}/decision {decision: "approve"} via api.decide.
      await api.decide(item.run_id, "approve");
      setResolved("Approved. Execution continues in the payroll module.");
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
            {humanStatus(item.run_status)}{item.workflow_name ? <> · {item.workflow_name}</> : null}{item.pay_date ? <> · Pay date {fmtDate(item.pay_date)}</> : null}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {modeLabel && (
            <Badge tone="sand" className="border-sand-400 bg-sand-200 text-ink-900">{modeLabel}</Badge>
          )}
          {fullyFunded && <Badge tone="sage" className="border-sage-500 bg-sage-500 text-ink-900">Fully funded</Badge>}
          <Button variant="ghost" size="sm" asChild>
            <Link href={`/dashboard/runs/${item.run_id}`}>Open payroll run</Link>
          </Button>
          {item.workflow_run_id && (
            <Button variant="ghost" size="sm" asChild>
              <Link href={`/dashboard/workflows/runs/${item.workflow_run_id}`}>View workflow run</Link>
            </Button>
          )}
        </div>
      </div>

      <dl className="grid gap-3 sm:grid-cols-3">
        <div className="rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4">
          <dt className="text-xs font-extrabold uppercase tracking-wider text-sage-400">Total needed</dt>
          <dd className="mt-1 font-display text-xl font-semibold tabular-nums text-sand-100">{formatMoney(item.total_required ?? "0", currency)}</dd>
        </div>
        <div className="rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4">
          <dt className="text-xs font-extrabold uppercase tracking-wider text-sage-400">Current balance</dt>
          <dd className="mt-1 font-display text-xl font-semibold tabular-nums text-sand-100">{formatMoney(item.balance ?? "0", currency)}</dd>
        </div>
        <div className={cn("rounded-2xl border-2 p-4", fullyFunded ? "border-sage-600/50 bg-sage-500/5" : "border-coral-600/60 bg-coral-500/10")}>
          <dt className="text-xs font-extrabold uppercase tracking-wider text-sage-400">Shortfall</dt>
          <dd className={cn("mt-1 font-display text-xl font-semibold tabular-nums", fullyFunded ? "text-sage-300" : "text-coral-300")}>
            {fullyFunded ? formatMoney("0", currency) : formatMoney(item.shortfall ?? "0", currency)}
          </dd>
        </div>
      </dl>

      <div className="grid gap-3 md:grid-cols-2">
        <ContractorList title="Will be paid" items={item.will_pay ?? []} variant="pay" fallbackCurrency={currency} />
        <ContractorList title="Will be held" items={item.will_hold ?? []} variant="hold" fallbackCurrency={currency} />
      </div>

      {error && <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">{error}</p>}

      {resolved ? (
        <p role="status" className="rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-4 py-3 text-sm font-bold text-sage-300">{resolved}</p>
      ) : item.can_approve ? (
        canDecide ? (
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={() => void approve()} disabled={busy}>
              <Check /> {busy ? "Approving…" : "Approve payroll"}
            </Button>
            {item.will_hold.length > 0 && (
              <span className="text-sm font-semibold text-sage-400">Approving pays the Will be paid list. The rest stays held until the wallet is topped up.</span>
            )}
          </div>
        ) : (
          <p className="text-sm font-semibold text-sage-400">Waiting for a decider. Owners, admins and approvers can approve this payroll.</p>
        )
      ) : (
        <p role="status" className="rounded-2xl border-2 border-ink-600 bg-ink-900/50 px-4 py-3 text-sm font-bold text-sage-300">
          {fullyFunded
            ? "Nothing to approve right now."
            : item.settlement_mode === "ALL_OR_NOTHING"
              ? `Nobody is paid until this employer's wallet is topped up by ${formatMoney(item.shortfall ?? "0", currency)}. The held run resumes once it is funded.`
              : `Held lines are waiting for the wallet to be topped up by ${formatMoney(item.shortfall ?? "0", currency)}.`}
        </p>
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
      subtitle={`${today}. Every employer with a payroll decision waiting or lines on hold: who will be paid, who is held and why, and what is missing. A person approves here, never a workflow.`}
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
          body="No payroll is waiting for approval and nothing is on hold."
          lottie={false}
        />
      )}

      {items && items.length > 0 && (
        <section className="grid gap-5" aria-label="Employers awaiting a payroll decision">
          <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
            <Sunrise className="size-5 text-coral-400" /> Needs a person today
          </h2>
          {items.map((item) => (
            <BriefCard key={`${item.employer_id}-${item.run_id}`} item={item} canDecide={canDecide} onApproved={onApproved} />
          ))}
        </section>
      )}
    </FebShell>
  );
}
