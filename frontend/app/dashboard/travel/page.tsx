"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Check, Lock, MapPin, Pencil, Plane, Plus, RefreshCw, ShoppingBag, Trash, X,
} from "lucide-react";

import { PageNotice, SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell, PickerField } from "@/components/feb/shell";
import { useSession } from "@/components/feb/use-session";
import { num } from "@/components/feb/accessors";
import {
  ModeBadge, SourceBadge, SpendPurchaseHistory, VicStatusBanner, fmtDate, fmtDay, shortHash, spendError,
} from "@/components/spend/shared";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/select";
import {
  api, apiTravel, ApiError, normalizePurchaseAll,
  type Contractor, type Employer, type SpendPurchase, type TravelBudget, type TravelComponent,
  type TravelComponentKind, type TravelTrip, type TravelTripStatus, type VicStatus,
} from "@/lib/api";
import { Stagger } from "@/lib/gsap-anim";
import { cn, formatMoney } from "@/lib/utils";

// Role gates, mirroring the Command Center's DECIDERS pattern.
const DECIDERS = ["owner", "admin", "approver"];
const EDITORS = ["owner", "admin"];

const CURRENCIES = ["USD", "EUR", "GBP"];

const KIND_LABELS: Record<TravelComponentKind, string> = {
  TRANSPORT: "Transport",
  LODGING: "Lodging",
  PER_DIEM: "Per diem",
  OTHER: "Other",
};

const KINDS = Object.keys(KIND_LABELS) as TravelComponentKind[];

/** Rendered verbatim under a trip's component table when a component is blocked by the credential limit. */
const TX_LIMIT_EXPLANATION =
  "This component is approved policy-wise but cannot execute on the current test credential (USD 200 per transaction). It is never split automatically.";

// ------------------------------------------------------------------ status pills + cap badges

const TRIP_TONES: Record<TravelTripStatus, BadgeProps["tone"]> = {
  REQUESTED: "sand",
  APPROVED: "sage",
  DENIED: "coral",
  PARTIALLY_PURCHASED: "warning",
  COMPLETED: "sage",
};

const TRIP_BADGE_CLASS: Partial<Record<TravelTripStatus, string>> = {
  DENIED: "bg-transparent",
  COMPLETED: "border-sage-500 bg-sage-500 text-ink-900",
};

function TripStatusPill({ status }: { status: TravelTripStatus }) {
  return (
    <Badge tone={TRIP_TONES[status]} className={TRIP_BADGE_CLASS[status]}>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}

function ComponentStatusPill({ status }: { status: TravelComponent["status"] }) {
  if (status === "PURCHASED") {
    return <Badge tone="sage" className="border-sage-500 bg-sage-500 text-ink-900">Purchased</Badge>;
  }
  if (status === "FAILED") return <Badge tone="coral">Failed</Badge>;
  return <Badge tone="sand">Pending</Badge>;
}

/**
 * The three distinct component cap states: within cap renders nothing
 * (neutral); above cap is amber until an override approval lands, then
 * sage; above the credential limit is coral and outranks the cap state.
 */
function CapBadge({ trip, comp }: { trip: TravelTrip; comp: TravelComponent }) {
  if (comp.tx_limit_blocked) {
    return <Badge tone="danger">Exceeds $200 credential limit</Badge>;
  }
  if (comp.over_cap) {
    const overridden = trip.caps_overridden
      && (trip.status === "APPROVED" || trip.status === "PARTIALLY_PURCHASED" || trip.status === "COMPLETED");
    return overridden
      ? <Badge tone="sage" className="bg-transparent">Above cap — override approved</Badge>
      : <Badge tone="warning">Above cap — override approval required</Badge>;
  }
  return null;
}

function isPurchasable(trip: TravelTrip, comp: TravelComponent): boolean {
  return (trip.status === "APPROVED" || trip.status === "PARTIALLY_PURCHASED")
    && (comp.status === "PENDING" || comp.status === "FAILED");
}

function tripCurrency(trip: TravelTrip): string {
  return trip.currency ?? trip.components[0]?.currency ?? "USD";
}

// ------------------------------------------------------------------ budget card

type CapKey = "cap_transport" | "cap_lodging" | "cap_per_diem" | "cap_other";

const CAP_FIELDS: { key: CapKey; label: string }[] = [
  { key: "cap_transport", label: "Transport cap" },
  { key: "cap_lodging", label: "Lodging cap" },
  { key: "cap_per_diem", label: "Per diem cap" },
  { key: "cap_other", label: "Other cap" },
];

function BudgetCard({
  budget, status, canEdit, onSaved,
}: {
  budget: TravelBudget;
  status: VicStatus | null;
  canEdit: boolean;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [cap, setCap] = useState(budget.monthly_cap);
  const [perTx, setPerTx] = useState(budget.per_tx_limit ?? "");
  const [enabled, setEnabled] = useState(budget.enabled);
  const [caps, setCaps] = useState<Record<CapKey, string>>({
    cap_transport: budget.cap_transport ?? "",
    cap_lodging: budget.cap_lodging ?? "",
    cap_per_diem: budget.cap_per_diem ?? "",
    cap_other: budget.cap_other ?? "",
  });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const capN = num(budget.monthly_cap);
  const spentN = num(budget.spent_this_period) ?? 0;
  const pct = capN && capN > 0 ? Math.min(100, Math.max(0, (spentN / capN) * 100)) : spentN > 0 ? 100 : 0;
  const remaining = budget.remaining ?? (capN !== null ? (capN - spentN).toFixed(2) : null);

  const budgetTx = num(budget.per_tx_limit);
  const credTx = status ? num(status.tx_limit_usd) : null;
  const effTx = budgetTx === null ? credTx : credTx === null ? budgetTx : Math.min(budgetTx, credTx);
  const effTxIsCred = effTx !== null && credTx !== null && credTx === effTx && (budgetTx === null || credTx <= budgetTx);

  function openEdit() {
    setCap(budget.monthly_cap);
    setPerTx(budget.per_tx_limit ?? "");
    setEnabled(budget.enabled);
    setCaps({
      cap_transport: budget.cap_transport ?? "",
      cap_lodging: budget.cap_lodging ?? "",
      cap_per_diem: budget.cap_per_diem ?? "",
      cap_other: budget.cap_other ?? "",
    });
    setSaveError(null);
    setEditing(true);
  }

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      await apiTravel.updateBudget(budget.employer_id, {
        monthly_cap: cap.trim(),
        per_tx_limit: perTx.trim() === "" ? null : perTx.trim(),
        enabled,
        cap_transport: caps.cap_transport.trim() === "" ? null : caps.cap_transport.trim(),
        cap_lodging: caps.cap_lodging.trim() === "" ? null : caps.cap_lodging.trim(),
        cap_per_diem: caps.cap_per_diem.trim() === "" ? null : caps.cap_per_diem.trim(),
        cap_other: caps.cap_other.trim() === "" ? null : caps.cap_other.trim(),
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
          {budget.lock_reason || "Payroll is held for this employer — wages are paid first, so travel purchases are locked."}
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

      <dl className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
        {CAP_FIELDS.map(({ key, label }) => (
          <div key={key} className="rounded-2xl border-2 border-ink-700 bg-ink-900/50 px-3 py-2.5">
            <dt className="text-xs font-extrabold uppercase tracking-wide text-sage-400">{label}</dt>
            <dd className="mt-0.5 font-display font-semibold tabular-nums text-sand-100">
              {budget[key] ? formatMoney(budget[key], budget.currency) : "No cap"}
            </dd>
          </div>
        ))}
      </dl>

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
            {CAP_FIELDS.map(({ key, label }) => (
              <label key={key} className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">{label} (blank = no cap)</span>
                <Input
                  inputMode="decimal" value={caps[key]}
                  onChange={(e) => setCaps((cur) => ({ ...cur, [key]: e.target.value }))}
                  aria-label={label}
                />
              </label>
            ))}
          </div>
          <label className="flex items-center gap-2.5 text-sm font-bold text-sand-100">
            <input
              type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)}
              className="size-5 shrink-0 accent-coral-500"
            />
            Budget enabled — trips can be approved and purchased against it
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
  | { kind: "component"; trip: TravelTrip; comp: TravelComponent }
  | { kind: "all"; trip: TravelTrip };

interface BuilderRow {
  id: number;
  kind: TravelComponentKind;
  label: string;
  merchant: string;
  amount: string;
}

let rowSeq = 0;

export default function TravelPage() {
  const { session } = useSession("/dashboard/travel");
  const canDecide = !!session && DECIDERS.includes(session.role);
  const canEdit = !!session && EDITORS.includes(session.role);

  const [status, setStatus] = useState<VicStatus | null>(null);
  const [budgets, setBudgets] = useState<TravelBudget[] | null>(null);
  const [purchases, setPurchases] = useState<SpendPurchase[] | null>(null);
  const [employers, setEmployers] = useState<Employer[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [trips, setTrips] = useState<TravelTrip[] | null>(null);
  const [tripsLoading, setTripsLoading] = useState(false);
  const [tripsError, setTripsError] = useState<string | null>(null);

  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [denyFor, setDenyFor] = useState<string | null>(null);
  const [denyReason, setDenyReason] = useState("");

  // Approve-with-override confirmation
  const [approveFor, setApproveFor] = useState<TravelTrip | null>(null);
  const [overrideChecked, setOverrideChecked] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);

  const [confirmFor, setConfirmFor] = useState<ConfirmTarget | null>(null);
  const [purchaseError, setPurchaseError] = useState<string | null>(null);
  const [purchasing, setPurchasing] = useState(false);

  // Trip creator form
  const [formEmployerId, setFormEmployerId] = useState("");
  const [contractors, setContractors] = useState<Contractor[] | null>(null);
  const [formContractorId, setFormContractorId] = useState("");
  const [formDestination, setFormDestination] = useState("");
  const [formStart, setFormStart] = useState("");
  const [formEnd, setFormEnd] = useState("");
  const [formPurpose, setFormPurpose] = useState("");
  const [formCurrency, setFormCurrency] = useState("USD");
  const [rows, setRows] = useState<BuilderRow[]>(() => [
    { id: ++rowSeq, kind: "TRANSPORT", label: "", merchant: "", amount: "" },
    { id: ++rowSeq, kind: "LODGING", label: "", merchant: "", amount: "" },
  ]);
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const loadCore = useCallback(async () => {
    const [st, b, p, ov] = await Promise.allSettled([
      apiTravel.status(),
      apiTravel.budgets(),
      apiTravel.purchases(),
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

  const loadTrips = useCallback(async () => {
    setTripsLoading(true);
    setTripsError(null);
    try {
      setTrips(await apiTravel.trips());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setTrips(null);
      setTripsError(spendError(e));
    }
    setTripsLoading(false);
  }, []);

  useEffect(() => { void loadTrips(); }, [loadTrips]);

  // Contractors for the trip creator follow the chosen employer.
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
    await Promise.all([loadCore(), loadTrips()]);
    setRefreshing(false);
  }

  const compByRef = new Map<string, { trip: TravelTrip; comp: TravelComponent }>();
  for (const trip of trips ?? []) {
    for (const comp of trip.components) {
      if (comp.id) compByRef.set(comp.id, { trip, comp });
      compByRef.set(`${trip.id}:${comp.id}`, { trip, comp });
    }
  }

  // ---------------------------------------------------------- decisions
  function openApprove(trip: TravelTrip) {
    setApproveFor(trip);
    setOverrideChecked(false);
    setApproveError(null);
  }

  async function confirmApprove() {
    if (!approveFor) return;
    const overCap = approveFor.components.filter((c) => c.over_cap);
    setApproving(true);
    setApproveError(null);
    try {
      const updated = await apiTravel.decide(approveFor.id, {
        decision: "approve",
        override_caps: overCap.length > 0 ? overrideChecked : false,
      });
      setTrips((cur) => (cur ?? []).map((t) => (t.id === updated.id ? updated : t)));
      setNotice(
        `Approved the trip to ${updated.destination} for ${updated.contractor_name} — ${formatMoney(updated.total, tripCurrency(updated))}` +
        (updated.caps_overridden ? ", with a cap override." : "."),
      );
      setApproveFor(null);
      void loadCore();
    } catch (e) {
      // 409s show the server message verbatim; the panel stays open.
      setApproveError(spendError(e));
    }
    setApproving(false);
  }

  async function deny(trip: TravelTrip) {
    setBusyId(trip.id);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await apiTravel.decide(trip.id, {
        decision: "deny",
        override_caps: false,
        ...(denyReason.trim() ? { reason: denyReason.trim() } : {}),
      });
      setTrips((cur) => (cur ?? []).map((t) => (t.id === updated.id ? updated : t)));
      setNotice(`Denied the trip to ${updated.destination} for ${updated.contractor_name}.`);
      setDenyFor(null);
      setDenyReason("");
      void loadCore();
    } catch (e) {
      setActionError(spendError(e));
    }
    setBusyId(null);
  }

  // ---------------------------------------------------------- purchases
  async function confirmPurchase() {
    if (!confirmFor) return;
    setPurchasing(true);
    setPurchaseError(null);
    try {
      if (confirmFor.kind === "component") {
        const { trip, comp } = confirmFor;
        const purchase = await apiTravel.purchaseComponent(trip.id, comp.id);
        // The endpoint returns 200 even for a FAILED attempt — inspect .status.
        if (purchase.status === "SUCCEEDED") {
          setNotice(
            `Purchased ${comp.label} for ${trip.contractor_name} — ${formatMoney(purchase.amount, purchase.currency)} in ${purchase.mode} mode.` +
            (purchase.journal_txn_id !== null ? ` Posted as Journal #${purchase.journal_txn_id}.` : ""),
          );
        } else {
          setActionError(
            `Purchase attempt ${purchase.attempt_no} for ${comp.label} failed: ${purchase.failure_detail || purchase.failure_code || "The provider refused the purchase."}`,
          );
        }
      } else {
        const { trip } = confirmFor;
        const summary = normalizePurchaseAll(await apiTravel.purchaseAll(trip.id));
        if (summary.purchased.length > 0) {
          setNotice(`Purchased ${summary.purchased.length} component${summary.purchased.length === 1 ? "" : "s"} on the trip to ${trip.destination} for ${trip.contractor_name}.`);
        }
        if (summary.failed.length > 0) {
          const first = summary.failed[0];
          setActionError(
            `${summary.failed.length} component${summary.failed.length === 1 ? "" : "s"} could not be purchased. First failure: ${first.failure_detail || first.failure_code || "The provider refused the purchase."}`,
          );
        }
        if (summary.purchased.length === 0 && summary.failed.length === 0) {
          setNotice(`Nothing was left to purchase on the trip to ${trip.destination}.`);
        }
      }
      setConfirmFor(null);
      await Promise.all([loadCore(), loadTrips()]);
    } catch (e) {
      // Policy refusals (409: PAYROLL_HELD, TX_LIMIT_EXCEEDED, …) — server message verbatim, panel stays open.
      setPurchaseError(spendError(e));
    }
    setPurchasing(false);
  }

  // ---------------------------------------------------------- creator helpers
  function setRow(id: number, patch: Partial<BuilderRow>) {
    setRows((cur) => cur.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }

  function addRow() {
    rowSeq += 1;
    setRows((cur) => [...cur, { id: rowSeq, kind: "OTHER", label: "", merchant: "", amount: "" }]);
  }

  function removeRow(id: number) {
    setRows((cur) => cur.filter((r) => r.id !== id));
  }

  const builderTotal = rows.reduce((sum, r) => sum + (num(r.amount) ?? 0), 0);

  async function createTrip(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    setNotice(null);
    if (!formEmployerId) { setFormError("Choose an employer first."); return; }
    if (!formContractorId) { setFormError("Pick the contractor travelling."); return; }
    if (!formDestination.trim()) { setFormError("Enter the destination."); return; }
    if (!formStart || !formEnd) { setFormError("Enter the start and end dates."); return; }
    if (formEnd < formStart) { setFormError("The end date is before the start date."); return; }
    if (!formPurpose.trim()) { setFormError("Enter the purpose of the trip."); return; }

    const components: { kind: TravelComponentKind; label: string; merchant: string; amount: string; currency: string }[] = [];
    for (const r of rows) {
      if (!r.label.trim() && !r.merchant.trim() && !r.amount.trim()) continue;
      const a = num(r.amount);
      if (!r.label.trim() || !r.merchant.trim() || a === null || a <= 0) {
        setFormError("Each component needs a label, a merchant and a valid amount — or remove the row.");
        return;
      }
      components.push({ kind: r.kind, label: r.label.trim(), merchant: r.merchant.trim(), amount: a.toFixed(2), currency: formCurrency });
    }
    if (components.length === 0) { setFormError("Add at least one component to the trip."); return; }

    setCreating(true);
    try {
      await apiTravel.createTrip({
        employer_id: formEmployerId,
        contractor_id: formContractorId,
        destination: formDestination.trim(),
        start_date: formStart,
        end_date: formEnd,
        purpose: formPurpose.trim(),
        components,
      });
      setFormDestination("");
      setFormPurpose("");
      setFormStart("");
      setFormEnd("");
      setRows([
        { id: ++rowSeq, kind: "TRANSPORT", label: "", merchant: "", amount: "" },
        { id: ++rowSeq, kind: "LODGING", label: "", merchant: "", amount: "" },
      ]);
      setNotice("Trip requested — it is waiting for a decision.");
      await loadTrips();
    } catch (err) {
      setFormError(spendError(err));
    }
    setCreating(false);
  }

  const purchaseBlocked = status?.mode === "VIC_UNAVAILABLE";

  const approveOverCap = approveFor ? approveFor.components.filter((c) => c.over_cap) : [];
  const approveNeedsOverride = approveOverCap.length > 0;

  const confirmComponents: TravelComponent[] = confirmFor
    ? confirmFor.kind === "component"
      ? [confirmFor.comp]
      : confirmFor.trip.components.filter((c) => isPurchasable(confirmFor.trip, c))
    : [];
  const confirmTotal = confirmComponents.reduce((sum, c) => sum + (num(c.amount) ?? 0), 0);

  return (
    <FebShell
      session={session}
      title="Travel"
      subtitle="Business travel and accommodation, bought through Visa VIC (Airi). Each employer has a travel budget with caps per category — travel money never touches wage funds, and a held payroll locks purchases until wages are paid."
      actions={
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading travel…" />}
      {error && <SectionError message={error} onRetry={() => void loadCore()} />}
      {notice && <PageNotice message={notice} />}
      {actionError && (
        <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
          {actionError}
        </p>
      )}

      {status && <VicStatusBanner status={status} subject="Trip purchases" />}

      {/* ------------------------------------------------ budgets */}
      <section className="grid gap-4">
        <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
          <Plane className="size-5 text-coral-400" /> Budgets by employer
        </h2>
        {budgets && budgets.length === 0 && (
          <SectionEmpty
            title="No travel budgets yet"
            body="A budget appears here for each employer. Once one exists, trips can be approved and purchased against its monthly cap and category caps."
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

      {/* ------------------------------------------------ trip creator */}
      <section className="grid gap-4">
        <h2 className="font-display text-xl font-semibold text-sand-100">Request a trip</h2>
        <Card className="p-5">
          <form className="grid gap-4" onSubmit={(e) => void createTrip(e)}>
            <div className="grid gap-4 md:grid-cols-3">
              <PickerField label="Employer">
                <NativeSelect value={formEmployerId} onChange={(e) => setFormEmployerId(e.target.value)} aria-label="Employer for the trip">
                  {(employers ?? []).map((emp) => (
                    <option key={emp.id} value={emp.id}>{emp.name} — {emp.country}</option>
                  ))}
                </NativeSelect>
              </PickerField>
              <PickerField label="Contractor travelling">
                <NativeSelect value={formContractorId} onChange={(e) => setFormContractorId(e.target.value)} aria-label="Contractor for the trip">
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
            <div className="grid gap-4 md:grid-cols-3">
              <label className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">Destination</span>
                <Input value={formDestination} onChange={(e) => setFormDestination(e.target.value)} placeholder="e.g. Berlin, Germany" required aria-label="Destination" />
              </label>
              <label className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">Start date</span>
                <Input type="date" value={formStart} onChange={(e) => setFormStart(e.target.value)} required aria-label="Start date" />
              </label>
              <label className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">End date</span>
                <Input type="date" value={formEnd} onChange={(e) => setFormEnd(e.target.value)} required aria-label="End date" />
              </label>
            </div>
            <label className="grid gap-1.5">
              <span className="text-sm font-extrabold text-sage-400">Purpose</span>
              <Input value={formPurpose} onChange={(e) => setFormPurpose(e.target.value)} placeholder="e.g. Client onboarding workshop" required aria-label="Purpose" />
            </label>

            <fieldset className="grid gap-2">
              <legend className="text-sm font-extrabold text-sage-400">Components</legend>
              {rows.map((r) => (
                <div key={r.id} className="grid gap-3 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-3 md:grid-cols-[auto_1fr_1fr_auto_auto] md:items-end">
                  <label className="grid gap-1.5">
                    <span className="text-xs font-extrabold text-sage-400">Kind</span>
                    <NativeSelect value={r.kind} onChange={(e) => setRow(r.id, { kind: e.target.value as TravelComponentKind })} aria-label="Component kind">
                      {KINDS.map((k) => <option key={k} value={k}>{KIND_LABELS[k]}</option>)}
                    </NativeSelect>
                  </label>
                  <label className="grid gap-1.5">
                    <span className="text-xs font-extrabold text-sage-400">Label</span>
                    <Input value={r.label} onChange={(e) => setRow(r.id, { label: e.target.value })} placeholder="e.g. Return flight" aria-label="Component label" />
                  </label>
                  <label className="grid gap-1.5">
                    <span className="text-xs font-extrabold text-sage-400">Merchant</span>
                    <Input value={r.merchant} onChange={(e) => setRow(r.id, { merchant: e.target.value })} placeholder="e.g. Lufthansa" aria-label="Component merchant" />
                  </label>
                  <label className="grid gap-1.5">
                    <span className="text-xs font-extrabold text-sage-400">Amount ({formCurrency})</span>
                    <Input inputMode="decimal" value={r.amount} onChange={(e) => setRow(r.id, { amount: e.target.value })} placeholder="0.00" className="w-32" aria-label="Component amount" />
                  </label>
                  <Button type="button" variant="ghost" size="sm" onClick={() => removeRow(r.id)} disabled={rows.length <= 1} aria-label="Remove component">
                    <Trash /> Remove
                  </Button>
                </div>
              ))}
              <div>
                <Button type="button" variant="outline" size="sm" onClick={addRow}>
                  <Plus /> Add component
                </Button>
              </div>
            </fieldset>

            {formError && <p role="alert" className="text-sm font-bold text-coral-300">{formError}</p>}
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" size="sm" disabled={creating || !formEmployerId}>
                <Plane /> {creating ? "Requesting…" : "Request trip"}
              </Button>
              <span className="text-sm font-bold text-sage-300">
                Trip total: <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(builderTotal.toFixed(2), formCurrency)}</span>
              </span>
            </div>
          </form>
        </Card>
      </section>

      {/* ------------------------------------------------ trips */}
      <section className="grid gap-4">
        <h2 className="font-display text-xl font-semibold text-sand-100">Trips</h2>

        {tripsLoading && <SectionLoading label="Loading trips…" />}
        {tripsError && <SectionError message={tripsError} onRetry={() => void loadTrips()} />}
        {trips && trips.length === 0 && !tripsLoading && (
          <SectionEmpty
            title="No trips yet"
            body="No one has requested a trip yet. Create the first request with the form above — it lands here waiting for a decision."
          />
        )}

        {trips && trips.length > 0 && (
          <Stagger className="grid gap-4">
            {trips.map((trip) => {
              const purchasable = trip.components.filter((c) => isPurchasable(trip, c));
              const anyTxBlocked = trip.components.some((c) => c.tx_limit_blocked);
              return (
                <Card key={trip.id} className="grid gap-3 p-5">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="flex items-center gap-2 font-display text-lg font-semibold text-sand-100">
                        <MapPin className="size-5 shrink-0 text-coral-400" /> {trip.destination}
                      </p>
                      <p className="mt-0.5 text-sm font-semibold text-sage-400">
                        {trip.contractor_name} · {trip.employer_name} · {fmtDay(trip.start_date)} – {fmtDay(trip.end_date)} · {trip.purpose}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <SourceBadge source={trip.source} />
                      <span className="font-display text-lg font-semibold tabular-nums text-sand-100">
                        {formatMoney(trip.total, tripCurrency(trip))}
                      </span>
                      <TripStatusPill status={trip.status} />
                    </div>
                  </div>

                  {trip.status === "DENIED" && trip.deny_reason && (
                    <p className="text-sm font-semibold text-coral-300">
                      Denied{trip.decided_by_email ? ` by ${trip.decided_by_email}` : ""}: {trip.deny_reason}
                    </p>
                  )}
                  {trip.policy_summary && (
                    <p className="text-sm font-semibold text-sage-400">{trip.policy_summary}</p>
                  )}

                  <div className="overflow-x-auto rounded-2xl border-2 border-ink-700">
                    <table className="w-full min-w-[720px] text-left text-sm">
                      <thead>
                        <tr className="border-b-2 border-ink-700 text-sage-400">
                          <th scope="col" className="px-4 py-2.5 font-extrabold">Component</th>
                          <th scope="col" className="px-3 py-2.5 font-extrabold">Merchant</th>
                          <th scope="col" className="px-3 py-2.5 text-right font-extrabold">Amount</th>
                          <th scope="col" className="px-3 py-2.5 font-extrabold">Policy</th>
                          <th scope="col" className="px-3 py-2.5 font-extrabold">Status</th>
                          <th scope="col" className="px-3 py-2.5 font-extrabold"><span className="sr-only">Action</span></th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-ink-700/70">
                        {trip.components.map((comp) => (
                          <tr key={comp.id} className="bg-ink-900/30 align-top">
                            <td className="px-4 py-3 font-semibold text-sand-100">
                              {comp.label}
                              <span className="block text-xs font-bold text-sage-400">{KIND_LABELS[comp.kind]}</span>
                              {comp.status === "FAILED" && (
                                <span className="block text-xs font-bold text-coral-300">
                                  Last attempt failed{comp.last_failure_code ? <> — reason code <span className="font-mono">{comp.last_failure_code}</span></> : ""}
                                  {comp.failure_detail ? <>: {comp.failure_detail}</> : ""}
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-3 font-semibold text-sage-300">{comp.merchant}</td>
                            <td className="px-3 py-3 text-right font-display font-semibold tabular-nums text-sand-100">{formatMoney(comp.amount, comp.currency)}</td>
                            <td className="px-3 py-3">
                              <span className="flex flex-wrap items-center gap-2">
                                <CapBadge trip={trip} comp={comp} />
                                {comp.cap && !comp.tx_limit_blocked && (
                                  <span className="text-xs font-semibold text-sage-400">Cap {formatMoney(comp.cap, comp.currency)}</span>
                                )}
                              </span>
                            </td>
                            <td className="px-3 py-3">
                              <span className="flex flex-wrap items-center gap-2">
                                <ComponentStatusPill status={comp.status} />
                                {comp.latest_purchase && <ModeBadge mode={comp.latest_purchase.mode} />}
                              </span>
                            </td>
                            <td className="px-3 py-3 text-right">
                              {canDecide && isPurchasable(trip, comp) && (
                                <Button
                                  size="sm" variant={comp.status === "FAILED" ? "outline" : "primary"}
                                  onClick={() => { setConfirmFor({ kind: "component", trip, comp }); setPurchaseError(null); }}
                                  disabled={purchaseBlocked || busyId === trip.id}
                                >
                                  <ShoppingBag /> {comp.status === "FAILED" ? "Retry purchase" : "Purchase"}
                                </Button>
                              )}
                              {comp.status === "PURCHASED" && (
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
                  {anyTxBlocked && (
                    <p className="text-sm font-semibold text-coral-300">{TX_LIMIT_EXPLANATION}</p>
                  )}

                  {canDecide && trip.status === "REQUESTED" && denyFor !== trip.id && (
                    <div className="flex flex-wrap gap-2">
                      <Button variant="sage" size="sm" onClick={() => openApprove(trip)} disabled={busyId === trip.id}>
                        <Check /> Approve
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => { setDenyFor(trip.id); setDenyReason(""); }} disabled={busyId === trip.id}>
                        <X /> Deny
                      </Button>
                    </div>
                  )}
                  {canDecide && trip.status === "REQUESTED" && denyFor === trip.id && (
                    <div className="grid gap-2 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4 sm:grid-cols-[1fr_auto_auto] sm:items-end">
                      <label className="grid gap-1.5">
                        <span className="text-sm font-extrabold text-sage-400">Reason for denying (optional)</span>
                        <Input value={denyReason} onChange={(e) => setDenyReason(e.target.value)} placeholder="e.g. Trip postponed to next quarter" aria-label="Reason for denying" />
                      </label>
                      <Button variant="primary" size="sm" onClick={() => void deny(trip)} disabled={busyId === trip.id}>
                        {busyId === trip.id ? "Working…" : "Confirm deny"}
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setDenyFor(null)} disabled={busyId === trip.id}>Cancel</Button>
                    </div>
                  )}

                  {canDecide && purchasable.length > 0 && (
                    <div className="flex flex-wrap items-center gap-3">
                      <Button
                        size="sm"
                        onClick={() => { setConfirmFor({ kind: "all", trip }); setPurchaseError(null); }}
                        disabled={purchaseBlocked || busyId === trip.id}
                      >
                        <ShoppingBag /> Purchase all ({purchasable.length})
                      </Button>
                      {purchaseBlocked && (
                        <span className="text-sm font-bold text-coral-300">
                          Purchases are refused while the VIC credential is unavailable.
                        </span>
                      )}
                      {trip.approval_expires_at && (
                        <span className="text-sm font-semibold text-sage-400">
                          Approval expires {fmtDate(trip.approval_expires_at)}
                        </span>
                      )}
                    </div>
                  )}
                  {!canDecide && trip.status === "REQUESTED" && (
                    <p className="text-sm font-semibold text-sage-400">Waiting for a decision — owners, admins and approvers can decide.</p>
                  )}
                </Card>
              );
            })}
          </Stagger>
        )}
      </section>

      {/* ------------------------------------------------ approve confirmation (cap override) */}
      {approveFor && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/70 p-4" role="dialog" aria-modal="true" aria-label="Confirm trip approval">
          <Card className="grid max-h-[90dvh] w-full max-w-lg gap-4 overflow-y-auto p-6">
            <h3 className="font-display text-xl font-semibold text-sand-100">Approve this trip</h3>
            <dl className="grid gap-2 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4 text-sm">
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Traveller</dt><dd className="font-bold text-sand-100">{approveFor.contractor_name}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Employer</dt><dd className="font-bold text-sand-100">{approveFor.employer_name}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Destination</dt><dd className="font-bold text-sand-100">{approveFor.destination}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Dates</dt><dd className="font-bold text-sand-100">{fmtDay(approveFor.start_date)} – {fmtDay(approveFor.end_date)}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Total</dt><dd className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(approveFor.total, tripCurrency(approveFor))}</dd></div>
            </dl>
            {approveNeedsOverride ? (
              <div className="grid gap-3">
                <p className="font-extrabold text-sand-100">These components are above their category cap:</p>
                <ul className="grid gap-1.5 text-sm">
                  {approveOverCap.map((c) => (
                    <li key={c.id} className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-3 py-2">
                      <span className="font-semibold text-sand-100">{c.label} <span className="text-sage-400">({KIND_LABELS[c.kind]})</span></span>
                      <span className="font-display font-semibold tabular-nums text-sand-100">
                        {formatMoney(c.amount, c.currency)}{c.cap ? <> <span className="text-sage-400">over cap {formatMoney(c.cap, c.currency)}</span></> : null}
                      </span>
                    </li>
                  ))}
                </ul>
                <label className="flex items-start gap-2.5 text-sm font-bold text-sand-100">
                  <input
                    type="checkbox" checked={overrideChecked} onChange={(e) => setOverrideChecked(e.target.checked)}
                    className="mt-0.5 size-5 shrink-0 accent-coral-500"
                  />
                  Approve with cap override — these components may be purchased above their category caps
                </label>
              </div>
            ) : (
              <p className="text-sm text-sage-300">Every component is within its category cap. Approving locks this itinerary as the purchase snapshot.</p>
            )}
            {approveError && (
              <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
                {approveError}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button variant="sage" onClick={() => void confirmApprove()} disabled={approving || (approveNeedsOverride && !overrideChecked)}>
                <Check /> {approving ? "Approving…" : approveNeedsOverride ? "Approve with override" : "Approve trip"}
              </Button>
              <Button variant="ghost" onClick={() => setApproveFor(null)} disabled={approving}>Cancel</Button>
            </div>
          </Card>
        </div>
      )}

      {/* ------------------------------------------------ purchase confirmation */}
      {confirmFor && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/70 p-4" role="dialog" aria-modal="true" aria-label="Confirm travel purchase">
          <Card className="grid max-h-[90dvh] w-full max-w-lg gap-4 overflow-y-auto p-6">
            <div className="flex items-start justify-between gap-3">
              <h3 className="font-display text-xl font-semibold text-sand-100">Confirm this exact purchase</h3>
              {status && <ModeBadge mode={status.mode === "VIC" ? "VIC" : "SIMULATED"} />}
            </div>
            <p className="text-sm text-sage-300">
              The purchase is locked to the approved snapshot below — the components and total exactly as approved.
              If anything no longer matches, the server refuses it — nothing is substituted.
            </p>
            <dl className="grid gap-2 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4 text-sm">
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Traveller</dt><dd className="font-bold text-sand-100">{confirmFor.trip.contractor_name}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Employer</dt><dd className="font-bold text-sand-100">{confirmFor.trip.employer_name}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Destination</dt><dd className="font-bold text-sand-100">{confirmFor.trip.destination}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Dates</dt><dd className="font-bold text-sand-100">{fmtDay(confirmFor.trip.start_date)} – {fmtDay(confirmFor.trip.end_date)}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Cap override</dt><dd className="font-bold text-sand-100">{confirmFor.trip.caps_overridden ? "Approved" : "Not used"}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Approval expires</dt><dd className="font-bold text-sand-100">{fmtDate(confirmFor.trip.approval_expires_at)}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Approval hash</dt><dd className="break-all font-mono font-bold text-sand-100">{shortHash(confirmFor.trip.approval_hash) ?? "—"}</dd></div>
              <div className="flex justify-between gap-4"><dt className="font-extrabold text-sage-400">Approved by</dt><dd className="font-bold text-sand-100">{confirmFor.trip.decided_by_email ?? "—"}</dd></div>
            </dl>
            <div className="grid gap-1.5 rounded-2xl border-2 border-ink-600 bg-ink-900/50 p-4 text-sm">
              <p className="font-extrabold text-sage-400">{confirmFor.kind === "all" ? "Components to purchase" : "Component to purchase"}</p>
              {confirmComponents.map((c) => (
                <div key={c.id} className="flex justify-between gap-4">
                  <span className="font-semibold text-sand-100">{c.label} <span className="text-sage-400">at {c.merchant}</span></span>
                  <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(c.amount, c.currency)}</span>
                </div>
              ))}
              <div className="mt-1 flex justify-between gap-4 border-t border-ink-600 pt-2">
                <span className="font-extrabold text-sage-400">Total</span>
                <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(confirmTotal.toFixed(2), tripCurrency(confirmFor.trip))}</span>
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
                <ShoppingBag /> {purchasing ? "Purchasing…" : confirmFor.kind === "all" ? "Confirm purchase of all components" : "Confirm purchase"}
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
              const hit = (p.component_id && compByRef.get(p.component_id)) || (p.component_id && p.trip_id ? compByRef.get(`${p.trip_id}:${p.component_id}`) : undefined);
              if (hit) {
                return <>{hit.comp.label} <span className="text-sage-400">at {hit.comp.merchant} · {hit.trip.destination} for {hit.trip.contractor_name}</span></>;
              }
              const parent = p.trip_id ?? p.request_id;
              return <span className="font-mono text-xs">{parent ? `Trip ${parent.slice(0, 8)}…` : "Travel purchase"}</span>;
            }}
          />
        )}
      </section>
    </FebShell>
  );
}
