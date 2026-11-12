"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Check, Lock, PackageCheck, Pencil, Plus, RefreshCw, ShoppingBag, Trash, X,
} from "lucide-react";

import { PageNotice, SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell, PickerField } from "@/components/feb/shell";
import { useSession } from "@/components/feb/use-session";
import { num } from "@/components/feb/accessors";
import {
  ModeBadge, SourceBadge, SpendPurchaseHistory, VicStatusBanner, fmtDate, shortHash, spendError,
} from "@/components/spend/shared";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/select";
import {
  api, apiOnboarding, ApiError, normalizePurchaseAll,
  type Contractor, type Employer, type OnboardingBudget, type OnboardingItem, type OnboardingKit,
  type OnboardingKitStatus, type SpendPurchase, type VicStatus,
} from "@/lib/api";
import { Stagger } from "@/lib/gsap-anim";
import { cn, formatMoney } from "@/lib/utils";

// Role gates, mirroring the Command Center's DECIDERS pattern.
const DECIDERS = ["owner", "admin", "approver"];
const EDITORS = ["owner", "admin"];

const CURRENCIES = ["USD", "EUR", "GBP"];

// ------------------------------------------------------------------ kit builder catalogue

interface StandardItemDef {
  key: string;
  kind: string;
  label: string;
  merchant: string;
  unit: string;
  perSeat: boolean;
  hint: string;
}

const STANDARD_ITEMS: StandardItemDef[] = [
  { key: "background", kind: "BACKGROUND_CHECK", label: "Background verification", merchant: "Verification provider", unit: "40.00", perSeat: false, hint: "One-off check per new hire" },
  { key: "github", kind: "SOFTWARE_SEAT", label: "GitHub seat", merchant: "GitHub", unit: "12.00", perSeat: true, hint: "Per month, per seat" },
  { key: "notion", kind: "SOFTWARE_SEAT", label: "Notion seat", merchant: "Notion", unit: "8.00", perSeat: true, hint: "Per month, per seat" },
  { key: "giftcard", kind: "GIFT_CARD", label: "Welcome gift card", merchant: "Gift card provider", unit: "50.00", perSeat: false, hint: "One-off welcome credit" },
  { key: "headset", kind: "EQUIPMENT", label: "Headset", merchant: "Equipment retailer", unit: "75.00", perSeat: false, hint: "One-off equipment" },
];

interface BuilderStd extends StandardItemDef {
  checked: boolean;
  qty: number;
}

interface BuilderCustom {
  id: number;
  label: string;
  merchant: string;
  amount: string;
}

let customSeq = 0;

// ------------------------------------------------------------------ status pills

const KIT_TONES: Record<OnboardingKitStatus, BadgeProps["tone"]> = {
  DRAFT: "sand",
  APPROVED: "sage",
  DENIED: "coral",
  PARTIALLY_PURCHASED: "warning",
  COMPLETED: "sage",
};

const KIT_BADGE_CLASS: Partial<Record<OnboardingKitStatus, string>> = {
  DENIED: "bg-transparent",
  COMPLETED: "border-sage-500 bg-sage-500 text-ink-900",
};

function KitStatusPill({ status }: { status: OnboardingKitStatus }) {
  return (
    <Badge tone={KIT_TONES[status]} className={KIT_BADGE_CLASS[status]}>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}

function ItemStatusPill({ status }: { status: OnboardingItem["status"] }) {
  if (status === "PURCHASED") {
    return <Badge tone="sage" className="border-sage-500 bg-sage-500 text-ink-900">Purchased</Badge>;
  }
  if (status === "FAILED") return <Badge tone="coral">Failed</Badge>;
  return <Badge tone="sand">Pending</Badge>;
}

function isPurchasable(kit: OnboardingKit, item: OnboardingItem): boolean {
  return (kit.status === "APPROVED" || kit.status === "PARTIALLY_PURCHASED")
    && (item.status === "PENDING" || item.status === "FAILED");
}

// ------------------------------------------------------------------ budget card

function BudgetCard({
  budget, status, canEdit, onSaved,
}: {
  budget: OnboardingBudget;
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
  const remaining = budget.remaining ?? (capN !== null ? (capN - spentN).toFixed(2) : null);

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
      await apiOnboarding.updateBudget(budget.employer_id, {
        monthly_cap: cap.trim(),
        per_tx_limit: perTx.trim() === "" ? null : perTx.trim(),
        enabled,
      });
      setEditing(false);
      onSaved();
    } catch (e) {
      setSaveError(spendError(e));
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
          {budget.lock_reason || "Payroll is held for this employer — wages are paid first, so onboarding purchases are locked."}
        </p>
      )}

      <div className="grid gap-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
          <span className="font-bold text-sage-300">
            Spent <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(budget.spent_this_period, budget.currency)}</span>
            {" "}of <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(budget.monthly_cap, budget.currency)}</span>
          </span>
          <span className="font-extrabold tabular-nums text-sand-100">
            {formatMoney(remaining, budget.currency)} left
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
            ? <>Effective per-purchase limit: <span className="font-display font-bold tabular-nums text-sand-100">{formatMoney(effTx.toFixed(2), effTxIsCred ? "USD" : budget.currency)}</span>{effTxIsCred ? " (set by the VIC credential)" : ""}</>
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
            Budget enabled — kits can be approved and purchased against it
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

type ConfirmTarget =
  | { kind: "item"; kit: OnboardingKit; item: OnboardingItem }
  | { kind: "all"; kit: OnboardingKit };

export default function OnboardingPage() {
  const { session } = useSession("/dashboard/onboarding");
  const canDecide = !!session && DECIDERS.includes(session.role);
  const canEdit = !!session && EDITORS.includes(session.role);

  const [status, setStatus] = useState<VicStatus | null>(null);
  const [budgets, setBudgets] = useState<OnboardingBudget[] | null>(null);
  const [purchases, setPurchases] = useState<SpendPurchase[] | null>(null);
  const [employers, setEmployers] = useState<Employer[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [kits, setKits] = useState<OnboardingKit[] | null>(null);
  const [kitsLoading, setKitsLoading] = useState(false);
  const [kitsError, setKitsError] = useState<string | null>(null);

  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [denyFor, setDenyFor] = useState<string | null>(null);
  const [denyReason, setDenyReason] = useState("");

  const [confirmFor, setConfirmFor] = useState<ConfirmTarget | null>(null);
  const [purchaseError, setPurchaseError] = useState<string | null>(null);
  const [purchasing, setPurchasing] = useState(false);

  // Kit builder form
  const [formEmployerId, setFormEmployerId] = useState("");
  const [contractors, setContractors] = useState<Contractor[] | null>(null);
  const [formContractorId, setFormContractorId] = useState("");
  const [formCurrency, setFormCurrency] = useState("USD");
  const [stdItems, setStdItems] = useState<BuilderStd[]>(() =>
    STANDARD_ITEMS.map((d) => ({ ...d, checked: true, qty: 1 })),
  );
  const [customItems, setCustomItems] = useState<BuilderCustom[]>([]);
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const loadCore = useCallback(async () => {
    const [st, b, p, ov] = await Promise.allSettled([
      apiOnboarding.status(),
      apiOnboarding.budgets(),
      apiOnboarding.purchases(),
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
      if (!(failed.reason instanceof ApiError && failed.reason.status === 401)) setError(spendError(failed.reason));
    } else {
      setError(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => { void loadCore(); }, [loadCore]);

  const loadKits = useCallback(async () => {
    setKitsLoading(true);
    setKitsError(null);
    try {
      setKits(await apiOnboarding.kits());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setKits(null);
      setKitsError(spendError(e));
    }
    setKitsLoading(false);
  }, []);

  useEffect(() => { void loadKits(); }, [loadKits]);

  // Contractors for the kit builder follow the chosen employer.
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
    await Promise.all([loadCore(), loadKits()]);
    setRefreshing(false);
  }

  const itemByRef = new Map<string, { kit: OnboardingKit; item: OnboardingItem }>();
  for (const kit of kits ?? []) {
    for (const item of kit.items) {
      if (item.id) itemByRef.set(item.id, { kit, item });
      itemByRef.set(`${kit.id}:${item.id}`, { kit, item });
    }
  }

  async function decide(kit: OnboardingKit, decision: "approve" | "deny") {
    setBusyId(kit.id);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await apiOnboarding.decide(kit.id, {
        decision,
        ...(decision === "deny" && denyReason.trim() ? { reason: denyReason.trim() } : {}),
      });
      setKits((cur) => (cur ?? []).map((k) => (k.id === updated.id ? updated : k)));
      setNotice(decision === "approve"
        ? `Approved the onboarding kit for ${updated.contractor_name} — ${formatMoney(updated.total, updated.currency)}.`
        : `Denied the onboarding kit for ${updated.contractor_name}.`);
      setDenyFor(null);
      setDenyReason("");
      void loadCore();
    } catch (e) {
      // 409s (e.g. budget or payroll state changed) show the server message verbatim.
      setActionError(spendError(e));
    }
    setBusyId(null);
  }

  async function confirmPurchase() {
    if (!confirmFor) return;
    setPurchasing(true);
    setPurchaseError(null);
    try {
      if (confirmFor.kind === "item") {
        const { kit, item } = confirmFor;
        const purchase = await apiOnboarding.purchaseItem(kit.id, item.id);
        // The endpoint returns 200 even for a FAILED attempt — inspect .status.
        if (purchase.status === "SUCCEEDED") {
          setNotice(
            `Purchased ${item.label} for ${kit.contractor_name} — ${formatMoney(purchase.amount, purchase.currency)} in ${purchase.mode} mode.` +
            (purchase.journal_txn_id !== null ? ` Posted as Journal #${purchase.journal_txn_id}.` : ""),
          );
        } else {
          setActionError(
            `Purchase attempt ${purchase.attempt_no} for ${item.label} failed: ${purchase.failure_detail || purchase.failure_code || "The provider refused the purchase."}`,
          );
        }
      } else {
        const { kit } = confirmFor;
        const summary = normalizePurchaseAll(await apiOnboarding.purchaseAll(kit.id));
        if (summary.purchased.length > 0) {
          setNotice(`Purchased ${summary.purchased.length} item${summary.purchased.length === 1 ? "" : "s"} on the kit for ${kit.contractor_name}.`);
        }
        if (summary.failed.length > 0) {
          const first = summary.failed[0];
          setActionError(
            `${summary.failed.length} item${summary.failed.length === 1 ? "" : "s"} could not be purchased. First failure: ${first.failure_detail || first.failure_code || "The provider refused the purchase."}`,
          );
        }
        if (summary.purchased.length === 0 && summary.failed.length === 0) {
          setNotice(`Nothing was left to purchase on the kit for ${kit.contractor_name}.`);
        }
      }
      setConfirmFor(null);
      await Promise.all([loadCore(), loadKits()]);
    } catch (e) {
      // Policy refusals (409: PAYROLL_HELD, TX_LIMIT_EXCEEDED, …) — server message verbatim, panel stays open.
      setPurchaseError(spendError(e));
    }
    setPurchasing(false);
  }

  // ---------------------------------------------------------- builder helpers
  function setStd(key: string, patch: Partial<BuilderStd>) {
    setStdItems((cur) => cur.map((it) => (it.key === key ? { ...it, ...patch } : it)));
  }

  function addCustom() {
    customSeq += 1;
    setCustomItems((cur) => [...cur, { id: customSeq, label: "", merchant: "", amount: "" }]);
  }

  function setCustom(id: number, patch: Partial<BuilderCustom>) {
    setCustomItems((cur) => cur.map((it) => (it.id === id ? { ...it, ...patch } : it)));
  }

  function removeCustom(id: number) {
    setCustomItems((cur) => cur.filter((it) => it.id !== id));
  }

  const builderTotal = (() => {
    let total = 0;
    for (const it of stdItems) {
      if (!it.checked) continue;
      const unit = num(it.unit);
      if (unit !== null) total += unit * Math.max(1, it.qty || 1);
    }
    for (const it of customItems) {
      const a = num(it.amount);
      if (a !== null) total += a;
    }
    return total;
  })();

  async function createKit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    setNotice(null);
    if (!formEmployerId) { setFormError("Choose an employer first."); return; }
    if (!formContractorId) { setFormError("Pick the contractor this kit is for."); return; }

    const items: { kind: string; label: string; merchant: string; amount: string; currency: string }[] = [];
    for (const it of stdItems) {
      if (!it.checked) continue;
      const unit = num(it.unit);
      if (unit === null || unit <= 0) { setFormError(`Enter a valid amount for ${it.label}.`); return; }
      const qty = it.perSeat ? Math.max(1, Math.floor(it.qty) || 1) : 1;
      items.push({
        kind: it.kind,
        label: qty > 1 ? `${it.label} × ${qty}` : it.label,
        merchant: it.merchant.trim(),
        amount: (unit * qty).toFixed(2),
        currency: formCurrency,
      });
    }
    for (const it of customItems) {
      if (!it.label.trim() && !it.merchant.trim() && !it.amount.trim()) continue;
      const a = num(it.amount);
      if (!it.label.trim() || !it.merchant.trim() || a === null || a <= 0) {
        setFormError("Each custom item needs a label, a merchant and a valid amount — or remove the row.");
        return;
      }
      items.push({ kind: "CUSTOM", label: it.label.trim(), merchant: it.merchant.trim(), amount: a.toFixed(2), currency: formCurrency });
    }
    if (items.length === 0) { setFormError("Add at least one item to the kit."); return; }

    setCreating(true);
    try {
      await apiOnboarding.createKit({
        employer_id: formEmployerId,
        contractor_id: formContractorId,
        items,
      });
      setCustomItems([]);
      setNotice("Onboarding kit created as a draft — it is waiting for a decision.");
      await loadKits();
    } catch (err) {
      setFormError(spendError(err));
    }
    setCreating(false);
  }

  const purchaseBlocked = status?.mode === "VIC_UNAVAILABLE";

  const confirmItems: OnboardingItem[] = confirmFor
    ? confirmFor.kind === "item"
      ? [confirmFor.item]
      : confirmFor.kit.items.filter((it) => isPurchasable(confirmFor.kit, it))
    : [];
  const confirmTotal = confirmItems.reduce((sum, it) => sum + (num(it.amount) ?? 0), 0);

  return (
    <FebShell
      session={session}
      title="Onboarding"
      subtitle="Onboarding kits for new hires, bought through Visa VIC (Airi). Each employer has a separate onboarding budget with its own monthly cap — kit money never touches wage funds, and a held payroll locks purchases until wages are paid."
      actions={
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading onboarding…" />}
      {error && <SectionError message={error} onRetry={() => void loadCore()} />}
      {notice && <PageNotice message={notice} />}
      {actionError && (
        <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
          {actionError}
        </p>
      )}

      {status && <VicStatusBanner status={status} subject="Onboarding purchases" />}

      {/* ------------------------------------------------ budgets */}
      <section className="grid gap-4">
        <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
          <PackageCheck className="size-5 text-coral-400" /> Budgets by employer
        </h2>
        {budgets && budgets.length === 0 && (
          <SectionEmpty
            title="No onboarding budgets yet"
            body="A budget appears here for each employer. Once one exists, kits can be approved and purchased against its monthly cap."
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

      {/* ------------------------------------------------ kit builder */}
      <section className="grid gap-4">
        <h2 className="font-display text-xl font-semibold text-sand-100">Build an onboarding kit</h2>
        <Card className="p-5">
          <form className="grid gap-4" onSubmit={(e) => void createKit(e)}>
            <div className="grid gap-4 md:grid-cols-3">
              <PickerField label="Employer">
                <NativeSelect value={formEmployerId} onChange={(e) => setFormEmployerId(e.target.value)} aria-label="Employer for the kit">
                  {(employers ?? []).map((emp) => (
                    <option key={emp.id} value={emp.id}>{emp.name} — {emp.country}</option>
                  ))}
                </NativeSelect>
              </PickerField>
              <PickerField label="Contractor (new hire)">
                <NativeSelect value={formContractorId} onChange={(e) => setFormContractorId(e.target.value)} aria-label="Contractor for the kit">
                  <option value="">Choose a contractor</option>
                  {(contractors ?? []).map((c) => (
                    <option key={c.id} value={c.id}>{c.full_name}</option>
                  ))}
                </NativeSelect>
              </PickerField>
              <PickerField label="Currency">
                <NativeSelect value={formCurrency} onChange={(e) => setFormCurrency(e.target.value)} aria-label="Currency">
                  {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
                </NativeSelect>
              </PickerField>
            </div>

            <fieldset className="grid gap-2">
              <legend className="text-sm font-extrabold text-sage-400">Standard items</legend>
              <div className="grid gap-2">
                {stdItems.map((it) => (
                  <div key={it.key} className="grid gap-3 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-3 sm:grid-cols-[1fr_auto_auto] sm:items-center">
                    <label className="flex items-start gap-2.5">
                      <input
                        type="checkbox" checked={it.checked} onChange={(e) => setStd(it.key, { checked: e.target.checked })}
                        className="mt-0.5 size-5 shrink-0 accent-coral-500" aria-label={`Include ${it.label}`}
                      />
                      <span>
                        <span className="block text-sm font-bold text-sand-100">{it.label}</span>
                        <span className="block text-xs font-semibold text-sage-400">{it.merchant} · {it.hint}</span>
                      </span>
                    </label>
                    <label className="grid gap-1">
                      <span className="text-xs font-extrabold text-sage-400">Amount ({formCurrency})</span>
                      <Input
                        inputMode="decimal" value={it.unit} disabled={!it.checked}
                        onChange={(e) => setStd(it.key, { unit: e.target.value })}
                        className="w-32" aria-label={`Amount for ${it.label}`}
                      />
                    </label>
                    {it.perSeat ? (
                      <label className="grid gap-1">
                        <span className="text-xs font-extrabold text-sage-400">Seats</span>
                        <Input
                          type="number" min={1} step={1} value={it.qty} disabled={!it.checked}
                          onChange={(e) => setStd(it.key, { qty: Math.max(1, Math.floor(Number(e.target.value)) || 1) })}
                          className="w-24" aria-label={`Seats for ${it.label}`}
                        />
                      </label>
                    ) : (
                      <span className="hidden sm:block w-24" />
                    )}
                  </div>
                ))}
              </div>
            </fieldset>

            <fieldset className="grid gap-2">
              <legend className="text-sm font-extrabold text-sage-400">Custom items</legend>
              {customItems.length === 0 && (
                <p className="text-sm font-semibold text-sage-400">No custom items. Add one for anything outside the standard kit.</p>
              )}
              {customItems.map((it) => (
                <div key={it.id} className="grid gap-3 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-3 sm:grid-cols-[1fr_1fr_auto_auto] sm:items-end">
                  <label className="grid gap-1.5">
                    <span className="text-xs font-extrabold text-sage-400">Label</span>
                    <Input value={it.label} onChange={(e) => setCustom(it.id, { label: e.target.value })} placeholder="e.g. Monitor arm" aria-label="Custom item label" />
                  </label>
                  <label className="grid gap-1.5">
                    <span className="text-xs font-extrabold text-sage-400">Merchant</span>
                    <Input value={it.merchant} onChange={(e) => setCustom(it.id, { merchant: e.target.value })} placeholder="e.g. Amazon" aria-label="Custom item merchant" />
                  </label>
                  <label className="grid gap-1.5">
                    <span className="text-xs font-extrabold text-sage-400">Amount ({formCurrency})</span>
                    <Input inputMode="decimal" value={it.amount} onChange={(e) => setCustom(it.id, { amount: e.target.value })} placeholder="0.00" className="w-32" aria-label="Custom item amount" />
                  </label>
                  <Button type="button" variant="ghost" size="sm" onClick={() => removeCustom(it.id)} aria-label="Remove custom item">
                    <Trash /> Remove
                  </Button>
                </div>
              ))}
              <div>
                <Button type="button" variant="outline" size="sm" onClick={addCustom}>
                  <Plus /> Add custom item
                </Button>
              </div>
            </fieldset>

            {formError && <p role="alert" className="text-sm font-bold text-coral-300">{formError}</p>}
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" size="sm" disabled={creating || !formEmployerId}>
                <PackageCheck /> {creating ? "Creating…" : "Create draft kit"}
              </Button>
              <span className="text-sm font-bold text-sage-300">
                Kit total: <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(builderTotal.toFixed(2), formCurrency)}</span>
              </span>
            </div>
          </form>
        </Card>
      </section>

      {/* ------------------------------------------------ kits */}
      <section className="grid gap-4">
        <h2 className="font-display text-xl font-semibold text-sand-100">Kits</h2>

        {kitsLoading && <SectionLoading label="Loading kits…" />}
        {kitsError && <SectionError message={kitsError} onRetry={() => void loadKits()} />}
        {kits && kits.length === 0 && !kitsLoading && (
          <SectionEmpty
            title="No onboarding kits yet"
            body="No kits have been built yet. Build the first one with the kit builder above — it lands here as a draft waiting for a decision."
          />
        )}

        {kits && kits.length > 0 && (
          <Stagger className="grid gap-4">
            {kits.map((kit) => {
              const purchasable = kit.items.filter((it) => isPurchasable(kit, it));
              return (
                <Card key={kit.id} className="grid gap-3 p-5">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-display text-lg font-semibold text-sand-100">
                        Kit for {kit.contractor_name}
                      </p>
                      <p className="mt-0.5 text-sm font-semibold text-sage-400">
                        {kit.employer_name}
                        {kit.requested_by_email ? <> · requested by {kit.requested_by_email}</> : null}
                        {kit.created_at ? <> · {fmtDate(kit.created_at)}</> : null}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <SourceBadge source={kit.source} />
                      <span className="font-display text-lg font-semibold tabular-nums text-sand-100">
                        {formatMoney(kit.total, kit.currency)}
                      </span>
                      <KitStatusPill status={kit.status} />
                    </div>
                  </div>

                  {kit.source === "WORKFLOW" && kit.draft_reason && (
                    <p className="text-sm font-semibold text-sage-400">{kit.draft_reason}</p>
                  )}
                  {kit.status === "DENIED" && kit.deny_reason && (
                    <p className="text-sm font-semibold text-coral-300">
                      Denied{kit.decided_by_email ? ` by ${kit.decided_by_email}` : ""}: {kit.deny_reason}
                    </p>
                  )}

                  <div className="overflow-x-auto rounded-2xl border-2 border-ink-700">
                    <table className="w-full min-w-[640px] text-left text-sm">
                      <thead>
                        <tr className="border-b-2 border-ink-700 text-sage-400">
                          <th scope="col" className="px-4 py-2.5 font-extrabold">Item</th>
                          <th scope="col" className="px-3 py-2.5 font-extrabold">Merchant</th>
                          <th scope="col" className="px-3 py-2.5 text-right font-extrabold">Amount</th>
                          <th scope="col" className="px-3 py-2.5 font-extrabold">Status</th>
                          <th scope="col" className="px-3 py-2.5 font-extrabold"><span className="sr-only">Action</span></th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-ink-700/70">
                        {kit.items.map((item) => (
                          <tr key={item.id} className="bg-ink-900/30 align-top">
                            <td className="px-4 py-3 font-semibold text-sand-100">
                              {item.label}
                              {item.status === "FAILED" && (
                                <span className="block text-xs font-bold text-coral-300">
                                  Last attempt failed{item.last_failure_code ? <> — reason code <span className="font-mono">{item.last_failure_code}</span></> : ""}
                                  {item.failure_detail ? <>: {item.failure_detail}</> : ""}
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-3 font-semibold text-sage-300">{item.merchant}</td>
                            <td className="px-3 py-3 text-right font-display font-semibold tabular-nums text-sand-100">{formatMoney(item.amount, item.currency)}</td>
                            <td className="px-3 py-3">
                              <span className="flex flex-wrap items-center gap-2">
                                <ItemStatusPill status={item.status} />
                                {item.latest_purchase && <ModeBadge mode={item.latest_purchase.mode} />}
                              </span>
                            </td>
                            <td className="px-3 py-3 text-right">
                              {canDecide && isPurchasable(kit, item) && (
                                <Button
                                  size="sm" variant={item.status === "FAILED" ? "outline" : "primary"}
                                  onClick={() => { setConfirmFor({ kind: "item", kit, item }); setPurchaseError(null); }}
                                  disabled={purchaseBlocked || busyId === kit.id}
                                >
                                  <ShoppingBag /> {item.status === "FAILED" ? "Retry purchase" : "Purchase"}
                                </Button>
                              )}
                              {item.status === "PURCHASED" && (
                                <span className="inline-flex items-center gap-1.5 text-xs font-extrabold uppercase tracking-wide text-sage-400">
                                  <Lock className="size-3.5" /> Done
                                </span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  {canDecide && kit.status === "DRAFT" && denyFor !== kit.id && (
                    <div className="flex flex-wrap gap-2">
                      <Button variant="sage" size="sm" onClick={() => void decide(kit, "approve")} disabled={busyId === kit.id}>
                        <Check /> {busyId === kit.id ? "Working…" : "Approve kit"}
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => { setDenyFor(kit.id); setDenyReason(""); }} disabled={busyId === kit.id}>
                        <X /> Deny
                      </Button>
                    </div>
                  )}
                  {canDecide && kit.status === "DRAFT" && denyFor === kit.id && (
                    <div className="grid gap-2 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4 sm:grid-cols-[1fr_auto_auto] sm:items-end">
                      <label className="grid gap-1.5">
                        <span className="text-sm font-extrabold text-sage-400">Reason for denying (optional)</span>
                        <Input value={denyReason} onChange={(e) => setDenyReason(e.target.value)} placeholder="e.g. Start date moved to next month" aria-label="Reason for denying" />
                      </label>
                      <Button variant="primary" size="sm" onClick={() => void decide(kit, "deny")} disabled={busyId === kit.id}>
                        {busyId === kit.id ? "Working…" : "Confirm deny"}
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setDenyFor(null)} disabled={busyId === kit.id}>Cancel</Button>
                    </div>
                  )}

                  {canDecide && purchasable.length > 0 && (
                    <div className="flex flex-wrap items-center gap-3">
                      <Button
                        size="sm"
                        onClick={() => { setConfirmFor({ kind: "all", kit }); setPurchaseError(null); }}
                        disabled={purchaseBlocked || busyId === kit.id}
                      >
                        <ShoppingBag /> Purchase all ({purchasable.length})
                      </Button>
                      {purchaseBlocked && (
                        <span className="text-sm font-bold text-coral-300">
                          Purchases are refused while the VIC credential is unavailable.
                        </span>
                      )}
                      {kit.approval_expires_at && (
                        <span className="text-sm font-semibold text-sage-400">
                          Approval expires {fmtDate(kit.approval_expires_at)}
                        </span>
                      )}
                    </div>
                  )}
                  {!canDecide && kit.status === "DRAFT" && (
                    <p className="text-sm font-semibold text-sage-400">Waiting for a decision — owners, admins and approvers can decide.</p>
                  )}
                </Card>
              );
            })}
          </Stagger>
        )}
      </section>

      {/* ------------------------------------------------ purchase confirmation */}
      {confirmFor && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/70 p-4" role="dialog" aria-modal="true" aria-label="Confirm onboarding purchase">
          <Card className="grid max-h-[90dvh] w-full max-w-lg gap-4 overflow-y-auto p-6">
            <div className="flex items-start justify-between gap-3">
              <h3 className="font-display text-xl font-semibold text-sand-100">Confirm this exact purchase</h3>
              {status && <ModeBadge mode={status.mode === "VIC" ? "VIC" : "SIMULATED"} />}
            </div>
            <p className="text-sm text-sage-300">
              The purchase is locked to the approved snapshot below — the items and total exactly as approved.
              If anything no longer matches, the server refuses it — nothing is substituted.
            </p>
            <dl className="grid gap-2 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4 text-sm">
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Contractor</dt><dd className="font-bold text-sand-100">{confirmFor.kit.contractor_name}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Employer</dt><dd className="font-bold text-sand-100">{confirmFor.kit.employer_name}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Approval expires</dt><dd className="font-bold text-sand-100">{fmtDate(confirmFor.kit.approval_expires_at)}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Approval hash</dt><dd className="break-all font-mono font-bold text-sand-100">{shortHash(confirmFor.kit.approval_hash) ?? "—"}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Approved by</dt><dd className="font-bold text-sand-100">{confirmFor.kit.decided_by_email ?? "—"}</dd></div>
            </dl>
            <div className="grid gap-1.5 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4 text-sm">
              <p className="font-extrabold text-sage-400">{confirmFor.kind === "all" ? "Items to purchase" : "Item to purchase"}</p>
              {confirmItems.map((it) => (
                <div key={it.id} className="flex justify-between gap-4">
                  <span className="font-semibold text-sand-100">{it.label} <span className="text-sage-400">at {it.merchant}</span></span>
                  <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(it.amount, it.currency)}</span>
                </div>
              ))}
              <div className="mt-1 flex justify-between gap-4 border-t border-ink-600 pt-2">
                <span className="font-extrabold text-sage-400">Total</span>
                <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(confirmTotal.toFixed(2), confirmFor.kit.currency)}</span>
              </div>
            </div>
            {status?.mode === "SIMULATED" && (
              <p className="rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-4 py-3 text-sm font-bold text-sand-200">
                This purchase will run in SIMULATED mode against the published Airwallex test card. No real card is charged.
              </p>
            )}
            {status?.mode === "VIC" && (
              <p className="rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-4 py-3 text-sm font-bold text-sage-300">
                This purchase is live via Visa VIC and will charge the configured credential.
              </p>
            )}
            {purchaseError && (
              <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
                {purchaseError}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => void confirmPurchase()} disabled={purchasing}>
                <ShoppingBag /> {purchasing ? "Purchasing…" : confirmFor.kind === "all" ? "Confirm purchase of all items" : "Confirm purchase"}
              </Button>
              <Button variant="ghost" onClick={() => setConfirmFor(null)} disabled={purchasing}>Cancel</Button>
            </div>
          </Card>
        </div>
      )}

      {/* ------------------------------------------------ purchase history */}
      <section className="grid gap-4">
        <h2 className="font-display text-xl font-semibold text-sand-100">Purchase history</h2>
        {purchases && (
          <SpendPurchaseHistory
            purchases={purchases}
            emptyBody="Completed and failed purchase attempts will be listed here, with the mode and credential used for each one."
            labelFor={(p) => {
              const hit = (p.item_id && itemByRef.get(p.item_id)) || (p.item_id && p.kit_id ? itemByRef.get(`${p.kit_id}:${p.item_id}`) : undefined);
              if (hit) {
                return <>{hit.item.label} <span className="text-sage-400">at {hit.item.merchant} · for {hit.kit.contractor_name}</span></>;
              }
              const parent = p.kit_id ?? p.request_id;
              return <span className="font-mono text-xs">{parent ? `Kit ${parent.slice(0, 8)}…` : "Onboarding purchase"}</span>;
            }}
          />
        )}
      </section>
    </FebShell>
  );
}
