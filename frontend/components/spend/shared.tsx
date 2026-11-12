"use client";

import { useState, type ReactNode } from "react";
import { Check, CreditCard, ExternalLink, ShieldCheck, TriangleAlert, X } from "lucide-react";

import { SectionEmpty } from "@/components/dashboard/states";
import { errMsg } from "@/components/feb/use-session";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ApiError, type SpendPurchase, type SpendSource, type VicStatus } from "@/lib/api";
import { formatMoney } from "@/lib/utils";

/**
 * Shared building blocks for the VIC spend categories (Onboarding,
 * Travel). These mirror the Benefits page's local helpers exactly —
 * same three-state VIC honesty, same verbatim 409 surfacing — so every
 * spend surface reads the same way.
 */

/**
 * Surface a backend error for display. Structured refusals arrive as
 * HTTP 409 {detail: {code, message}}; parseError keeps the server
 * message verbatim on ApiError.message and the code on ApiError.code,
 * so the message shown here is exactly what the server said.
 */
export function spendError(e: unknown): string {
  if (e instanceof ApiError) {
    return e.code ? `${e.message} (${e.code})` : e.message;
  }
  return errMsg(e);
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Dates that arrive as plain YYYY-MM-DD (trip dates) render as a day, not a timestamp. */
export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(`${iso}T00:00:00`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

/** Sum of decimal-string amounts (rows share one currency; the first row's currency is used). */
export function sumAmounts(rows: { amount: string; currency: string }[]): { total: string; currency: string } {
  const total = rows.reduce((sum, r) => sum + (Number(r.amount) || 0), 0);
  return { total: total.toFixed(2), currency: rows[0]?.currency ?? "USD" };
}

export function shortHash(hash: string | null | undefined): string | null {
  if (!hash) return null;
  return hash.length > 14 ? `${hash.slice(0, 12)}…` : hash;
}

export function ModeBadge({ mode }: { mode: "SIMULATED" | "VIC" }) {
  return mode === "SIMULATED" ? (
    <Badge tone="sage" className="bg-transparent">Simulated</Badge>
  ) : (
    <Badge tone="sage" className="border-sage-500 bg-sage-500 text-ink-900">VIC</Badge>
  );
}

/** "Drafted by workflow" / "Proposed in chat" provenance, from a payload's `source`. MANUAL renders nothing. */
export function SourceBadge({ source }: { source: SpendSource }) {
  if (source === "WORKFLOW") return <Badge tone="sand">Drafted by workflow</Badge>;
  if (source === "CHAT") return <Badge tone="sage" className="bg-transparent">Proposed in chat</Badge>;
  return null;
}

// ------------------------------------------------------------------ VIC status banner

/**
 * The three-state VIC banner, mirroring Benefits: SIMULATED is stated
 * plainly, VIC shows the masked credential ref, and VIC_UNAVAILABLE
 * never falls back to silent simulation. `subject` names the purchases
 * this page makes (e.g. "Onboarding purchases").
 */
export function VicStatusBanner({ status, subject }: { status: VicStatus; subject: string }) {
  if (status.mode === "SIMULATED") {
    return (
      <div className="flex gap-3 rounded-3xl border-2 border-sand-400/50 bg-sand-300/10 px-5 py-4" role="status">
        <CreditCard className="mt-0.5 size-6 shrink-0 text-sand-300" />
        <div className="grid gap-1">
          <p className="font-extrabold text-sand-100">
            Purchases run in <span className="uppercase">Simulated</span> mode
          </p>
          <p className="text-sm text-sage-300">
            No live Visa VIC credential is configured. {subject} are simulated against the published
            Airwallex test card (US$200.00 per transaction) — no real card is charged.
            Set <span className="font-mono font-bold text-sand-100">VIC_CREDENTIAL</span> from Airi to go live.
          </p>
        </div>
        <Badge tone="warning" className="ml-auto h-fit shrink-0">Simulated</Badge>
      </div>
    );
  }
  if (status.mode === "VIC") {
    return (
      <div className="flex gap-3 rounded-3xl border-2 border-sage-600/60 bg-sage-500/10 px-5 py-4" role="status">
        <ShieldCheck className="mt-0.5 size-6 shrink-0 text-sage-300" />
        <div className="grid gap-1">
          <p className="font-extrabold text-sand-100">Live via Visa VIC (Airi)</p>
          <p className="text-sm text-sage-300">
            Purchases are executed with credential{" "}
            <span className="break-all font-mono font-bold text-sand-100">{status.credential_ref}</span>
            {status.tx_limit_usd ? <> · per-transaction limit {formatMoney(status.tx_limit_usd, "USD")}</> : null}.
            Real funds move when a purchase is confirmed.
          </p>
        </div>
        <Badge tone="sage" className="ml-auto h-fit shrink-0 border-sage-500 bg-sage-500 text-ink-900">Live</Badge>
      </div>
    );
  }
  return (
    <div className="flex gap-3 rounded-3xl border-2 border-coral-600/60 bg-coral-500/10 px-5 py-4" role="alert">
      <TriangleAlert className="mt-0.5 size-6 shrink-0 text-coral-300" />
      <div className="grid gap-1">
        <p className="font-extrabold text-coral-300">VIC credential configured, but the Airi CLI is unavailable</p>
        <p className="text-sm text-coral-300/90">
          A credential is set (<span className="break-all font-mono font-bold">{status.credential_ref}</span>) but the
          Airi CLI cannot be reached, so purchases are refused. They are never simulated silently while a live
          credential is configured.
        </p>
      </div>
      <Badge tone="danger" className="ml-auto h-fit shrink-0">Unavailable</Badge>
    </div>
  );
}

// ------------------------------------------------------------------ purchase history

/**
 * Purchase-history table in the Benefits layout. `labelFor` resolves
 * the human item label for a row (kit item, trip component, …); rows
 * whose parent cannot be resolved show their parent id prefix instead
 * of an invented label.
 */
export function SpendPurchaseHistory({
  purchases,
  labelFor,
  emptyBody,
}: {
  purchases: SpendPurchase[];
  labelFor: (p: SpendPurchase) => ReactNode;
  emptyBody: string;
}) {
  if (purchases.length === 0) {
    return <SectionEmpty title="No purchases yet" body={emptyBody} />;
  }
  return (
    <Card className="overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[860px] text-left text-sm">
          <thead>
            <tr className="border-b-2 border-ink-700 text-sage-400">
              <th scope="col" className="px-5 py-3 font-extrabold">When</th>
              <th scope="col" className="px-3 py-3 font-extrabold">Item</th>
              <th scope="col" className="px-3 py-3 text-right font-extrabold">Amount</th>
              <th scope="col" className="px-3 py-3 font-extrabold">Mode</th>
              <th scope="col" className="px-3 py-3 font-extrabold">Credential</th>
              <th scope="col" className="px-3 py-3 font-extrabold">Provider ref</th>
              <th scope="col" className="px-3 py-3 font-extrabold">Status</th>
              <th scope="col" className="px-3 py-3 font-extrabold">Journal</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-700/70">
            {purchases.map((p) => (
              <tr key={p.id} className="bg-ink-900/30 align-top">
                <td className="px-5 py-3 font-semibold text-sage-300">{fmtDate(p.created_at)}</td>
                <td className="px-3 py-3 font-semibold text-sand-100">
                  {labelFor(p)}
                  {p.attempt_no > 1 && <span className="block text-xs font-bold text-sage-400">Attempt {p.attempt_no}</span>}
                  {p.status === "FAILED" && p.failure_detail && (
                    <span className="block text-xs font-bold text-coral-300">{p.failure_detail}</span>
                  )}
                </td>
                <td className="px-3 py-3 text-right font-display font-semibold tabular-nums text-sand-100">{formatMoney(p.amount, p.currency)}</td>
                <td className="px-3 py-3"><ModeBadge mode={p.mode} /></td>
                <td className="max-w-40 break-all px-3 py-3 font-mono text-xs font-semibold text-sage-300">{p.credential_ref}</td>
                <td className="max-w-40 break-all px-3 py-3 font-mono text-xs font-semibold text-sage-300">{p.provider_ref ?? "—"}</td>
                <td className="px-3 py-3">
                  <PurchaseStatusChip purchase={p} />
                </td>
                <td className="px-3 py-3 font-semibold text-sage-300">
                  {p.journal_txn_id !== null ? `Journal #${p.journal_txn_id}` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ two-phase purchase (Airi mandate)

/** True when this purchase is an open Airi mandate waiting for a person. */
export function isAwaitingAuthorization(p: SpendPurchase | null | undefined): p is SpendPurchase {
  return !!p && p.status === "PENDING";
}

/** Status chip for a purchase attempt, same wording as the Benefits history table. */
export function PurchaseStatusChip({ purchase }: { purchase: SpendPurchase }) {
  const label = purchase.mandate_status && purchase.status !== "SUCCEEDED"
    ? purchase.mandate_status.replaceAll("_", " ").toLowerCase()
    : purchase.status;
  return (
    <Badge tone={purchase.status === "SUCCEEDED" ? "success" : purchase.status === "PENDING" ? "warning" : "danger"}>{label}</Badge>
  );
}

/**
 * Phase B of a live purchase, shared by Onboarding and Travel and styled like
 * the Benefits request panel: the Airi authorization link, an honest
 * "I completed this" tick, then Complete or Cancel. Only people (never a
 * workflow or the chat) can press these.
 */
export function PendingMandatePanel({
  purchase, canAct, busy, onComplete, onCancel,
}: {
  purchase: SpendPurchase;
  canAct: boolean;
  busy: boolean;
  onComplete: (confirmed: boolean) => void;
  onCancel: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  return (
    <div className="grid gap-3 rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="warning">Awaiting authorization in Airi</Badge>
        <ModeBadge mode={purchase.mode} />
        {purchase.mandate_status && (
          <span className="text-xs font-bold text-sage-400">{purchase.mandate_status.replaceAll("_", " ").toLowerCase()}</span>
        )}
      </div>
      {purchase.approval_url && (
        <a
          href={purchase.approval_url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex w-fit items-center gap-2 font-extrabold text-sand-100 underline underline-offset-4"
        >
          <ExternalLink className="size-4" /> Authorize this purchase in Airi
        </a>
      )}
      {canAct ? (
        <>
          <label className="flex items-start gap-2 text-sm font-semibold text-sage-300">
            <input type="checkbox" className="mt-0.5 size-4" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            <span>I completed this purchase with the merchant. PaySilo does not check out for you; if unticked, the mandate is reported as cancelled.</span>
          </label>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => onComplete(confirmed)} disabled={busy}>
              <Check /> {busy ? "Working…" : "Complete purchase"}
            </Button>
            <Button variant="outline" size="sm" onClick={onCancel} disabled={busy}>
              <X /> Cancel mandate
            </Button>
          </div>
        </>
      ) : (
        <p className="text-sm font-semibold text-sage-400">An owner, admin or approver can complete or cancel this purchase.</p>
      )}
    </div>
  );
}
