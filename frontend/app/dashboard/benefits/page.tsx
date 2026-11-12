"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Check, CreditCard, ExternalLink, Gift, Lock, Pencil, RefreshCw, ShieldCheck,
  ShoppingBag, TriangleAlert, X,
} from "lucide-react";

import { PageNotice, SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell, PickerField } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { num } from "@/components/feb/accessors";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/select";
import {
  api, apiBenefits, ApiError,
  type BenefitBudget, type BenefitPurchase, type BenefitRequest, type BenefitRequestStatus,
  type Contractor, type Employer, type VicStatus,
} from "@/lib/api";
import { AiriConnectControls } from "@/components/spend/airi-connect";
import { Stagger } from "@/lib/gsap-anim";
import { cn, formatMoney } from "@/lib/utils";

// Role gates, mirroring the Command Center's DECIDERS pattern.
const DECIDERS = ["owner", "admin", "approver"];
const EDITORS = ["owner", "admin"];

const CURRENCIES = ["USD", "EUR", "GBP"];

/**
 * Surface a backend error for display. Structured refusals arrive as
 * HTTP 409 {detail: {code, message}}; parseError keeps the server
 * message verbatim on ApiError.message and the code on ApiError.code,
 * so the message shown here is exactly what the server said.
 */
function benefitError(e: unknown): string {
  if (e instanceof ApiError) {
    return e.code ? `${e.message} (${e.code})` : e.message;
  }
  return errMsg(e);
}

const REQUEST_TONES: Record<BenefitRequestStatus, BadgeProps["tone"]> = {
  REQUESTED: "sand",
  APPROVED: "sage",
  DENIED: "coral",
  PURCHASED: "sage",
  FAILED: "coral",
};

const REQUEST_BADGE_CLASS: Partial<Record<BenefitRequestStatus, string>> = {
  // Denied reads as a coral outline; Purchased as a filled sage pill.
  DENIED: "bg-transparent",
  PURCHASED: "border-sage-500 bg-sage-500 text-ink-900",
};

function RequestStatusPill({ status }: { status: BenefitRequestStatus }) {
  return (
    <Badge tone={REQUEST_TONES[status]} className={REQUEST_BADGE_CLASS[status]}>
      {status}
    </Badge>
  );
}

function ModeBadge({ mode }: { mode: "SIMULATED" | "VIC" }) {
  return mode === "SIMULATED" ? (
    <Badge tone="sage" className="bg-transparent">Simulated</Badge>
  ) : (
    <Badge tone="sage" className="border-sage-500 bg-sage-500 text-ink-900">VIC</Badge>
  );
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function shortHash(hash: string | null): string | null {
  if (!hash) return null;
  return hash.length > 14 ? `${hash.slice(0, 12)}…` : hash;
}

// ------------------------------------------------------------------ VIC status banner

function VicStatusBanner({ status, canEdit, onChanged }: { status: VicStatus; canEdit: boolean; onChanged: () => void }) {
  if (status.mode === "SIMULATED") {
    return (
      <div className="flex gap-3 rounded-3xl border-2 border-sand-400/50 bg-sand-300/10 px-5 py-4" role="status">
        <CreditCard className="mt-0.5 size-6 shrink-0 text-sand-300" />
        <div className="grid gap-1">
          <p className="font-extrabold text-sand-100">
            Purchases run in <span className="uppercase">Simulated</span> mode
          </p>
          <p className="text-sm text-sage-300">
            The live Airi connection is switched off. Benefit purchases are simulated against the published
            Airwallex test card (US$200.00 per transaction) — no real card is charged.
            Set <span className="font-mono font-bold text-sand-100">VIC_MODE=live</span> and sign in with{" "}
            <span className="font-mono font-bold text-sand-100">airi-cli auth login</span> to go live.
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
            Airi is connected and signed in
            {status.tx_limit_usd ? <> · test card limit {formatMoney(status.tx_limit_usd, "USD")} per transaction</> : null}.
            Each purchase creates an Airi mandate that you authorize in Airi, then you complete it here. PaySilo
            never sees or stores card numbers.
          </p>
          <AiriConnectControls status={status} canEdit={canEdit} onChanged={onChanged} />
        </div>
        <Badge tone="sage" className="ml-auto h-fit shrink-0 border-sage-500 bg-sage-500 text-ink-900">Live</Badge>
      </div>
    );
  }
  return (
    <div className="flex gap-3 rounded-3xl border-2 border-coral-600/60 bg-coral-500/10 px-5 py-4" role="alert">
      <TriangleAlert className="mt-0.5 size-6 shrink-0 text-coral-300" />
      <div className="grid gap-1">
        <p className="font-extrabold text-coral-300">
          {status.cli_available ? "Live mode is on, but Airi is not signed in" : "Live mode is on, but the Airi CLI was not found"}
        </p>
        <p className="text-sm text-coral-300/90">
          {status.cli_available
            ? "Connect the server's Airi account to enable purchases."
            : "Install it with npm i -g @airwallex/airi-cli@beta (and check AIRI_CLI_COMMAND)."}{" "}
          Purchases are refused until then. They are never simulated silently while live mode is on.
        </p>
        <AiriConnectControls status={status} canEdit={canEdit} onChanged={onChanged} />
      </div>
      <Badge tone="danger" className="ml-auto h-fit shrink-0">Unavailable</Badge>
    </div>
  );
}

// ------------------------------------------------------------------ budget card

function BudgetCard({
  budget, status, canEdit, onSaved,
}: {
  budget: BenefitBudget;
  status: VicStatus | null;
  canEdit: boolean;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [cap, setCap] = useState(budget.monthly_cap);
  const [perTx, setPerTx] = useState(budget.per_tx_limit ?? "");
  const [enabled, setEnabled] = useState(budget.enabled);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const capN = num(budget.monthly_cap);
  const spentN = num(budget.spent_this_period) ?? 0;
  const pct = capN && capN > 0 ? Math.min(100, Math.max(0, (spentN / capN) * 100)) : spentN > 0 ? 100 : 0;

  // Effective per-transaction limit: the tighter of the budget's own
  // limit and the credential's ceiling (tx_limit_usd, when present).
  const budgetTx = num(budget.per_tx_limit);
  const credTx = status ? num(status.tx_limit_usd) : null;
  const effTx = budgetTx === null ? credTx : credTx === null ? budgetTx : Math.min(budgetTx, credTx);
  const effTxIsCred = effTx !== null && credTx !== null && credTx === effTx && (budgetTx === null || credTx <= budgetTx);

  function openEdit() {
    setCap(budget.monthly_cap);
    setPerTx(budget.per_tx_limit ?? "");
    setEnabled(budget.enabled);
    setSaveError(null);
    setEditing(true);
  }

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      await apiBenefits.updateBudget(budget.employer_id, {
        currency: budget.currency,
        monthly_cap: cap.trim(),
        per_tx_limit: perTx.trim() === "" ? null : perTx.trim(),
        enabled,
      });
      setEditing(false);
      onSaved();
    } catch (e) {
      setSaveError(benefitError(e));
    }
    setSaving(false);
  }

  return (
    <Card className={cn("grid gap-4 p-5", budget.locked && "border-coral-700/70")}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="font-display text-lg font-semibold text-sand-100">{budget.employer_name}</h3>
          <p className="text-xs font-semibold text-sage-500">
            Period {budget.period} · {budget.currency} budget
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {budget.locked && (
            <Badge tone="danger"><Lock className="size-3.5" /> Locked</Badge>
          )}
          <Badge tone={budget.enabled ? "sage" : "neutral"}>{budget.enabled ? "Enabled" : "Disabled"}</Badge>
        </div>
      </div>

      {budget.locked && (
        <p className="rounded-2xl border-2 border-coral-700/50 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
          {budget.lock_reason || "Payroll is held for this employer — wages are paid first, so benefit purchases are locked."}
        </p>
      )}

      <div className="grid gap-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
          <span className="font-bold text-sage-300">
            Spent <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(budget.spent_this_period, budget.currency)}</span>
            {" "}of <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(budget.monthly_cap, budget.currency)}</span>
          </span>
          <span className="font-extrabold tabular-nums text-sand-100">
            {formatMoney(budget.remaining, budget.currency)} left
          </span>
        </div>
        <div className="h-3 overflow-hidden rounded-full bg-ink-700" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label={`Budget used for ${budget.employer_name}`}>
          <div
            className={cn("h-full rounded-full", pct >= 100 ? "bg-coral-500" : pct >= 80 ? "bg-sand-400" : "bg-sage-500")}
            style={{ width: `${pct}%` }}
          />
        </div>
        <p className="text-xs font-semibold text-sage-400">
          {effTx !== null
            ? <>Effective per-purchase limit: <span className="font-display font-bold tabular-nums text-sand-100">{formatMoney(effTx.toFixed(2), effTxIsCred ? "USD" : budget.currency)}</span>{effTxIsCred ? " (test card limit)" : ""}</>
            : "No per-purchase limit set."}
          {budget.per_tx_limit ? <> Budget limit {formatMoney(budget.per_tx_limit, budget.currency)}.</> : null}
        </p>
      </div>

      {canEdit && !editing && (
        <div>
          <Button variant="outline" size="sm" onClick={openEdit}>
            <Pencil /> Edit budget
          </Button>
        </div>
      )}

      {canEdit && editing && (
        <form
          className="grid gap-3 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4"
          onSubmit={(e) => { e.preventDefault(); void save(); }}
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1.5">
              <span className="text-sm font-extrabold text-sage-400">Monthly cap ({budget.currency})</span>
              <Input inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} required aria-label="Monthly cap" />
            </label>
            <label className="grid gap-1.5">
              <span className="text-sm font-extrabold text-sage-400">Per-purchase limit (blank = none)</span>
              <Input inputMode="decimal" value={perTx} onChange={(e) => setPerTx(e.target.value)} aria-label="Per-purchase limit" />
            </label>
          </div>
          <label className="flex items-center gap-2.5 text-sm font-bold text-sand-100">
            <input
              type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)}
              className="size-5 shrink-0 accent-coral-500"
            />
            Budget enabled — requests can be approved and purchased against it
          </label>
          {saveError && <p role="alert" className="text-sm font-bold text-coral-300">{saveError}</p>}
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={saving}>{saving ? "Saving…" : "Save budget"}</Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(false)} disabled={saving}>Cancel</Button>
          </div>
        </form>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ page

export default function BenefitsPage() {
  const { session } = useSession("/dashboard/benefits");
  const canDecide = !!session && DECIDERS.includes(session.role);
  const canEdit = !!session && EDITORS.includes(session.role);

  const [status, setStatus] = useState<VicStatus | null>(null);
  const [budgets, setBudgets] = useState<BenefitBudget[] | null>(null);
  const [purchases, setPurchases] = useState<BenefitPurchase[] | null>(null);
  const [employers, setEmployers] = useState<Employer[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [requests, setRequests] = useState<BenefitRequest[] | null>(null);
  const [reqLoading, setReqLoading] = useState(false);
  const [reqError, setReqError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState("");

  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [denyFor, setDenyFor] = useState<string | null>(null);
  const [denyReason, setDenyReason] = useState("");

  const [confirmFor, setConfirmFor] = useState<BenefitRequest | null>(null);
  const [purchaseError, setPurchaseError] = useState<string | null>(null);
  const [purchasing, setPurchasing] = useState(false);
  const [confirmedIds, setConfirmedIds] = useState<Record<string, boolean>>({});

  // New-request form
  const [formEmployerId, setFormEmployerId] = useState("");
  const [contractors, setContractors] = useState<Contractor[] | null>(null);
  const [formContractorId, setFormContractorId] = useState("");
  const [formWorkerName, setFormWorkerName] = useState("");
  const [formMerchant, setFormMerchant] = useState("");
  const [formItem, setFormItem] = useState("");
  const [formAmount, setFormAmount] = useState("");
  const [formCurrency, setFormCurrency] = useState("USD");
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const loadCore = useCallback(async () => {
    const [st, b, p, ov] = await Promise.allSettled([
      apiBenefits.vicStatus(),
      apiBenefits.budgets(),
      apiBenefits.purchases(),
      api.overview(),
    ]);
    if (st.status === "fulfilled") setStatus(st.value);
    if (b.status === "fulfilled") setBudgets(b.value);
    if (p.status === "fulfilled") setPurchases(p.value);
    if (ov.status === "fulfilled") {
      setEmployers(ov.value.employers);
      setFormEmployerId((cur) => cur || (ov.value.employers[0]?.id ?? ""));
    }
    const failed = [st, b, p, ov].find((r) => r.status === "rejected") as PromiseRejectedResult | undefined;
    if (failed) {
      if (!(failed.reason instanceof ApiError && failed.reason.status === 401)) setError(benefitError(failed.reason));
    } else {
      setError(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => { void loadCore(); }, [loadCore]);

  const loadRequests = useCallback(async () => {
    setReqLoading(true);
    setReqError(null);
    try {
      setRequests(await apiBenefits.requests(statusFilter ? { status: statusFilter } : undefined));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setRequests(null);
      setReqError(benefitError(e));
    }
    setReqLoading(false);
  }, [statusFilter]);

  useEffect(() => { void loadRequests(); }, [loadRequests]);

  // Contractors for the new-request form follow the chosen employer.
  useEffect(() => {
    if (!formEmployerId) { setContractors(null); return; }
    let live = true;
    setContractors(null);
    setFormContractorId("");
    api.employer(formEmployerId)
      .then((detail) => { if (live) setContractors(detail.contractors.filter((c) => c.is_active)); })
      .catch(() => { if (live) setContractors([]); });
    return () => { live = false; };
  }, [formEmployerId]);

  async function refresh() {
    setRefreshing(true);
    await Promise.all([loadCore(), loadRequests()]);
    setRefreshing(false);
  }

  const requestById = new Map((requests ?? []).map((r) => [r.id, r]));
  const employerNameById = new Map((employers ?? []).map((e) => [e.id, e.name]));

  async function decide(req: BenefitRequest, decision: "approve" | "deny") {
    setBusyId(req.id);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await apiBenefits.decide(req.id, {
        decision,
        ...(decision === "deny" && denyReason.trim() ? { reason: denyReason.trim() } : {}),
      });
      setRequests((cur) => (cur ?? []).map((r) => (r.id === updated.id ? updated : r)));
      setNotice(decision === "approve"
        ? `Approved ${updated.item} for ${updated.contractor_name} — ${formatMoney(updated.amount, updated.currency)} at ${updated.merchant}.`
        : `Denied ${updated.item} for ${updated.contractor_name}.`);
      setDenyFor(null);
      setDenyReason("");
      void loadCore();
    } catch (e) {
      // 409s (e.g. budget or payroll state changed) show the server message verbatim.
      setActionError(benefitError(e));
    }
    setBusyId(null);
  }

  async function confirmPurchase() {
    if (!confirmFor) return;
    setPurchasing(true);
    setPurchaseError(null);
    try {
      const purchase = await apiBenefits.purchase(confirmFor.id);
      // The endpoint returns 200 even for a FAILED attempt — inspect .status.
      if (purchase.status === "SUCCEEDED") {
        setNotice(
          `Purchase completed for ${confirmFor.contractor_name}: ${confirmFor.item} at ${confirmFor.merchant} — ` +
          `${formatMoney(purchase.amount, purchase.currency)} in ${purchase.mode} mode.` +
          (purchase.journal_txn_id !== null ? ` Posted as Journal #${purchase.journal_txn_id}.` : ""),
        );
      } else if (purchase.status === "PENDING") {
        setNotice(
          `Airi mandate created for ${confirmFor.item}. Open the authorization link on the request, approve it in Airi, then press Complete purchase.`,
        );
      } else {
        setActionError(
          `Purchase attempt ${purchase.attempt_no} failed: ${purchase.failure_detail || purchase.failure_code || "The provider refused the purchase."}`,
        );
      }
      setConfirmFor(null);
      await Promise.all([loadCore(), loadRequests()]);
    } catch (e) {
      // Policy refusals (409: PAYROLL_HELD, TX_LIMIT_EXCEEDED, …) — server message verbatim, panel stays open.
      setPurchaseError(benefitError(e));
    }
    setPurchasing(false);
  }

  async function completeLive(req: BenefitRequest, purchase: BenefitPurchase) {
    setBusyId(req.id);
    setActionError(null);
    setNotice(null);
    try {
      const done = await apiBenefits.completePurchase(purchase.id, confirmedIds[purchase.id] === true);
      if (done.status === "SUCCEEDED") {
        setNotice(`Purchase completed: ${req.item} at ${req.merchant}.`);
      } else if (done.status === "PENDING") {
        setNotice("Airi is still waiting for you to authorize this purchase. Open the authorization link, approve it, then try again.");
      } else {
        setActionError(done.failure_detail || done.failure_code || "The purchase did not complete.");
      }
      await Promise.all([loadCore(), loadRequests()]);
    } catch (e) {
      setActionError(benefitError(e));
    }
    setBusyId(null);
  }

  async function cancelLive(req: BenefitRequest, purchase: BenefitPurchase) {
    setBusyId(req.id);
    setActionError(null);
    setNotice(null);
    try {
      await apiBenefits.cancelPurchase(purchase.id);
      setNotice("The Airi mandate was cancelled. You can start a new purchase.");
      await Promise.all([loadCore(), loadRequests()]);
    } catch (e) {
      setActionError(benefitError(e));
    }
    setBusyId(null);
  }

  async function createRequest(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    setNotice(null);
    const contractor = (contractors ?? []).find((c) => c.id === formContractorId) ?? null;
    if (!formEmployerId) { setFormError("Choose an employer first."); return; }
    if (!contractor && !formWorkerName.trim()) { setFormError("Pick a contractor or type the worker's name."); return; }
    setCreating(true);
    try {
      await apiBenefits.createRequest({
        employer_id: formEmployerId,
        contractor_id: contractor ? contractor.id : null,
        contractor_name: contractor ? contractor.full_name : formWorkerName.trim(),
        merchant: formMerchant.trim(),
        item: formItem.trim(),
        amount: formAmount.trim(),
        currency: formCurrency,
      });
      setFormMerchant("");
      setFormItem("");
      setFormAmount("");
      setFormWorkerName("");
      setFormContractorId("");
      setNotice("Benefit request created — it is waiting for a decision.");
      await loadRequests();
    } catch (err) {
      setFormError(benefitError(err));
    }
    setCreating(false);
  }

  const purchaseBlocked = status?.mode === "VIC_UNAVAILABLE";

  return (
    <FebShell
      session={session}
      title="Benefits"
      subtitle="Employer-funded benefits for your team, bought through Visa VIC (Airi). Each employer has a separate benefits budget with its own monthly cap — benefit money never touches wage funds, and a held payroll locks purchases until wages are paid."
      actions={
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading benefits…" />}
      {error && <SectionError message={error} onRetry={() => void loadCore()} />}
      {notice && <PageNotice message={notice} />}
      {actionError && (
        <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
          {actionError}
        </p>
      )}

      {status && <VicStatusBanner status={status} canEdit={canEdit} onChanged={() => void loadCore()} />}

      {/* ------------------------------------------------ budgets */}
      <section className="grid gap-4">
        <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
          <Gift className="size-5 text-coral-400" /> Budgets by employer
        </h2>
        {budgets && budgets.length === 0 && (
          <SectionEmpty
            title="No benefit budgets yet"
            body="A budget appears here for each employer. Once one exists, requests can be approved and purchased against its monthly cap."
          />
        )}
        {budgets && budgets.length > 0 && (
          <Stagger className="grid gap-5 lg:grid-cols-2">
            {budgets.map((b) => (
              <BudgetCard key={b.id} budget={b} status={status} canEdit={canEdit} onSaved={() => void loadCore()} />
            ))}
          </Stagger>
        )}
        {budgets && !canEdit && (
          <p className="text-sm font-semibold text-sage-400">Budget editing is available to owners and admins.</p>
        )}
      </section>

      {/* ------------------------------------------------ new request */}
      <section className="grid gap-4">
        <h2 className="font-display text-xl font-semibold text-sand-100">Request a benefit</h2>
        <Card className="p-5">
          <form className="grid gap-4" onSubmit={(e) => void createRequest(e)}>
            <div className="grid gap-4 md:grid-cols-2">
              <PickerField label="Employer">
                <NativeSelect value={formEmployerId} onChange={(e) => setFormEmployerId(e.target.value)} aria-label="Employer for the request">
                  {(employers ?? []).map((emp) => (
                    <option key={emp.id} value={emp.id}>{emp.name} — {emp.country}</option>
                  ))}
                </NativeSelect>
              </PickerField>
              <PickerField label="Worker (contractor)">
                <NativeSelect value={formContractorId} onChange={(e) => setFormContractorId(e.target.value)} aria-label="Contractor for the request">
                  <option value="">Not listed — type the name below</option>
                  {(contractors ?? []).map((c) => (
                    <option key={c.id} value={c.id}>{c.full_name}</option>
                  ))}
                </NativeSelect>
              </PickerField>
            </div>
            {!formContractorId && (
              <label className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">Worker name</span>
                <Input value={formWorkerName} onChange={(e) => setFormWorkerName(e.target.value)} placeholder="e.g. Priya Nair" aria-label="Worker name" />
              </label>
            )}
            <div className="grid gap-4 md:grid-cols-2">
              <label className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">Merchant</span>
                <Input value={formMerchant} onChange={(e) => setFormMerchant(e.target.value)} placeholder="e.g. Amazon" required aria-label="Merchant" />
              </label>
              <label className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">Item</span>
                <Input value={formItem} onChange={(e) => setFormItem(e.target.value)} placeholder="e.g. Noise-cancelling headphones" required aria-label="Item" />
              </label>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <label className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">Amount</span>
                <Input inputMode="decimal" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} placeholder="149.00" required aria-label="Amount" />
              </label>
              <PickerField label="Currency">
                <NativeSelect value={formCurrency} onChange={(e) => setFormCurrency(e.target.value)} aria-label="Currency">
                  {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
                </NativeSelect>
              </PickerField>
            </div>
            {formError && <p role="alert" className="text-sm font-bold text-coral-300">{formError}</p>}
            <div>
              <Button type="submit" size="sm" disabled={creating || !formEmployerId}>
                <ShoppingBag /> {creating ? "Creating…" : "Create request"}
              </Button>
            </div>
          </form>
        </Card>
      </section>

      {/* ------------------------------------------------ requests */}
      <section className="grid gap-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h2 className="font-display text-xl font-semibold text-sand-100">Requests</h2>
          <div className="w-56">
            <PickerField label="Status">
              <NativeSelect value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} aria-label="Filter requests by status">
                <option value="">All statuses</option>
                <option value="REQUESTED">Requested</option>
                <option value="APPROVED">Approved</option>
                <option value="DENIED">Denied</option>
                <option value="PURCHASED">Purchased</option>
                <option value="FAILED">Failed</option>
              </NativeSelect>
            </PickerField>
          </div>
        </div>

        {reqLoading && <SectionLoading label="Loading requests…" />}
        {reqError && <SectionError message={reqError} onRetry={() => void loadRequests()} />}
        {requests && requests.length === 0 && !reqLoading && (
          <SectionEmpty
            title="No benefit requests"
            body={statusFilter
              ? "No requests match this status filter yet."
              : "No one has requested a benefit yet. Create the first request with the form above."}
          />
        )}

        {requests && requests.length > 0 && (
          <Stagger className="grid gap-4">
            {requests.map((req) => (
              <Card key={req.id} className="grid gap-3 p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-display text-lg font-semibold text-sand-100">
                      {req.item} <span className="text-sage-400">at</span> {req.merchant}
                    </p>
                    <p className="mt-0.5 text-sm font-semibold text-sage-400">
                      For {req.contractor_name} · {req.employer_name} · requested by {req.requested_by_email} · {fmtDate(req.created_at)}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="font-display text-lg font-semibold tabular-nums text-sand-100">
                      {formatMoney(req.amount, req.currency)}
                    </span>
                    <RequestStatusPill status={req.status} />
                  </div>
                </div>

                {req.status === "DENIED" && req.deny_reason && (
                  <p className="text-sm font-semibold text-coral-300">
                    Denied{req.decided_by_email ? ` by ${req.decided_by_email}` : ""}: {req.deny_reason}
                  </p>
                )}
                {req.status === "FAILED" && req.latest_purchase?.failure_detail && (
                  <p role="alert" className="rounded-2xl border-2 border-coral-700/50 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
                    Last purchase attempt failed: {req.latest_purchase.failure_detail}
                    {req.latest_purchase.failure_code ? ` (${req.latest_purchase.failure_code})` : ""}
                  </p>
                )}
                {req.status === "PURCHASED" && req.latest_purchase && (
                  <p className="flex flex-wrap items-center gap-2 text-sm font-semibold text-sage-300">
                    Purchased in <ModeBadge mode={req.latest_purchase.mode} /> mode
                    {req.latest_purchase.journal_txn_id !== null && <> · Journal #{req.latest_purchase.journal_txn_id}</>}
                  </p>
                )}

                {canDecide && req.status === "REQUESTED" && denyFor !== req.id && (
                  <div className="flex flex-wrap gap-2">
                    <Button variant="sage" size="sm" onClick={() => void decide(req, "approve")} disabled={busyId === req.id}>
                      <Check /> {busyId === req.id ? "Working…" : "Approve"}
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => { setDenyFor(req.id); setDenyReason(""); }} disabled={busyId === req.id}>
                      <X /> Deny
                    </Button>
                  </div>
                )}
                {canDecide && req.status === "REQUESTED" && denyFor === req.id && (
                  <div className="grid gap-2 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4 sm:grid-cols-[1fr_auto_auto] sm:items-end">
                    <label className="grid gap-1.5">
                      <span className="text-sm font-extrabold text-sage-400">Reason for denying (optional)</span>
                      <Input value={denyReason} onChange={(e) => setDenyReason(e.target.value)} placeholder="e.g. Over the per-person guideline" aria-label="Reason for denying" />
                    </label>
                    <Button variant="primary" size="sm" onClick={() => void decide(req, "deny")} disabled={busyId === req.id}>
                      {busyId === req.id ? "Working…" : "Confirm deny"}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => setDenyFor(null)} disabled={busyId === req.id}>Cancel</Button>
                  </div>
                )}

                {req.status === "APPROVED" && req.latest_purchase?.status === "PENDING" && (
                  <div className="grid gap-3 rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone="warning">Waiting for your authorization in Airi</Badge>
                      <ModeBadge mode={req.latest_purchase.mode} />
                    </div>
                    {req.latest_purchase.approval_url && (
                      <a
                        href={req.latest_purchase.approval_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex w-fit items-center gap-2 font-extrabold text-sand-100 underline underline-offset-4"
                      >
                        <ExternalLink className="size-4" /> Authorize this purchase in Airi
                      </a>
                    )}
                    {canDecide && (
                      <>
                        <label className="flex items-start gap-2 text-sm font-semibold text-sage-300">
                          <input
                            type="checkbox"
                            className="mt-0.5 size-4"
                            checked={confirmedIds[req.latest_purchase.id] === true}
                            onChange={(e) => {
                              const id = req.latest_purchase!.id;
                              setConfirmedIds((m) => ({ ...m, [id]: e.target.checked }));
                            }}
                          />
                          <span>I completed this purchase with the merchant. PaySilo does not check out for you; if unticked, the mandate is reported as cancelled.</span>
                        </label>
                        <div className="flex flex-wrap gap-2">
                          <Button size="sm" onClick={() => void completeLive(req, req.latest_purchase!)} disabled={busyId === req.id}>
                            <Check /> {busyId === req.id ? "Working…" : "Complete purchase"}
                          </Button>
                          <Button variant="outline" size="sm" onClick={() => void cancelLive(req, req.latest_purchase!)} disabled={busyId === req.id}>
                            <X /> Cancel mandate
                          </Button>
                        </div>
                      </>
                    )}
                  </div>
                )}

                {canDecide && req.status === "APPROVED" && req.latest_purchase?.status !== "PENDING" && (
                  <div className="flex flex-wrap items-center gap-3">
                    <Button
                      size="sm"
                      onClick={() => { setConfirmFor(req); setPurchaseError(null); }}
                      disabled={purchaseBlocked || busyId === req.id}
                    >
                      <ShoppingBag /> Purchase
                    </Button>
                    {purchaseBlocked && (
                      <span className="text-sm font-bold text-coral-300">
                        Purchases are refused while Airi is unavailable.
                      </span>
                    )}
                    {req.approval_expires_at && (
                      <span className="text-sm font-semibold text-sage-400">
                        Approval expires {fmtDate(req.approval_expires_at)}
                      </span>
                    )}
                  </div>
                )}
                {!canDecide && req.status === "REQUESTED" && (
                  <p className="text-sm font-semibold text-sage-400">Waiting for a decision — owners, admins and approvers can decide.</p>
                )}
              </Card>
            ))}
          </Stagger>
        )}
      </section>

      {/* ------------------------------------------------ purchase confirmation */}
      {confirmFor && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/70 p-4" role="dialog" aria-modal="true" aria-label="Confirm benefit purchase">
          <Card className="grid w-full max-w-lg gap-4 p-6">
            <div className="flex items-start justify-between gap-3">
              <h3 className="font-display text-xl font-semibold text-sand-100">Confirm this exact purchase</h3>
              {status && <ModeBadge mode={status.mode === "VIC" ? "VIC" : "SIMULATED"} />}
            </div>
            <p className="text-sm text-sage-300">
              The purchase is locked to the approved snapshot below. If the merchant, item or amount no longer
              matches, the server refuses it — nothing is substituted.
            </p>
            <dl className="grid gap-2 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4 text-sm">
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Worker</dt><dd className="font-bold text-sand-100">{confirmFor.contractor_name}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Employer</dt><dd className="font-bold text-sand-100">{confirmFor.employer_name}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Merchant</dt><dd className="font-bold text-sand-100">{confirmFor.merchant}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Item</dt><dd className="font-bold text-sand-100">{confirmFor.item}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Amount</dt><dd className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(confirmFor.amount, confirmFor.currency)}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Approval expires</dt><dd className="font-bold text-sand-100">{fmtDate(confirmFor.approval_expires_at)}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Approval hash</dt><dd className="break-all font-mono font-bold text-sand-100">{shortHash(confirmFor.approval_hash) ?? "—"}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Approved by</dt><dd className="font-bold text-sand-100">{confirmFor.decided_by_email ?? "—"}</dd></div>
            </dl>
            {status?.mode === "SIMULATED" && (
              <p className="rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-4 py-3 text-sm font-bold text-sand-200">
                This purchase will run in SIMULATED mode against the published Airwallex test card. No real card is charged.
              </p>
            )}
            {status?.mode === "VIC" && (
              <p className="rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-4 py-3 text-sm font-bold text-sage-300">
                This starts a live Airi mandate. You then authorize it in Airi and complete the purchase here.
              </p>
            )}
            {purchaseError && (
              <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
                {purchaseError}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => void confirmPurchase()} disabled={purchasing}>
                <ShoppingBag /> {purchasing ? "Purchasing…" : "Confirm purchase"}
              </Button>
              <Button variant="ghost" onClick={() => setConfirmFor(null)} disabled={purchasing}>Cancel</Button>
            </div>
          </Card>
        </div>
      )}

      {/* ------------------------------------------------ purchase history */}
      <section className="grid gap-4">
        <h2 className="font-display text-xl font-semibold text-sand-100">Purchase history</h2>
        {purchases && purchases.length === 0 && (
          <SectionEmpty title="No purchases yet" body="Completed and failed purchase attempts will be listed here, with the mode and credential used for each one." />
        )}
        {purchases && purchases.length > 0 && (
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[860px] text-left text-sm">
                <thead>
                  <tr className="border-b-2 border-ink-700 text-sage-400">
                    <th scope="col" className="px-5 py-3 font-extrabold">When</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Employer</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Item</th>
                    <th scope="col" className="px-3 py-3 text-right font-extrabold">Amount</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Mode</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Payment method</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Provider ref</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Status</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Journal</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-ink-700/70">
                  {purchases.map((p) => {
                    const req = requestById.get(p.request_id);
                    return (
                      <tr key={p.id} className="bg-ink-900/30 align-top">
                        <td className="px-5 py-3 font-semibold text-sage-300">{fmtDate(p.created_at)}</td>
                        <td className="px-3 py-3 font-bold text-sand-100">{req?.employer_name ?? employerNameById.get(p.employer_id) ?? "—"}</td>
                        <td className="px-3 py-3 font-semibold text-sand-100">
                          {req ? <>{req.item} <span className="text-sage-400">at {req.merchant}</span></> : <span className="font-mono text-xs">Request {p.request_id.slice(0, 8)}…</span>}
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
                          <Badge tone={p.status === "SUCCEEDED" ? "success" : p.status === "PENDING" ? "warning" : "danger"}>{p.mandate_status && p.status !== "SUCCEEDED" ? p.mandate_status.replaceAll("_", " ").toLowerCase() : p.status}</Badge>
                        </td>
                        <td className="px-3 py-3 font-semibold text-sage-300">
                          {p.journal_txn_id !== null ? `Journal #${p.journal_txn_id}` : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </section>
    </FebShell>
  );
}
