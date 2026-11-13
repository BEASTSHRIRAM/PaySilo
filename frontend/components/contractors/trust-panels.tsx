"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy, Loader2, ShieldCheck, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Pagination, usePagination } from "@/components/ui/pagination";
import { NativeSelect } from "@/components/ui/select";
import { PageError } from "@/components/dashboard/states";
import {
  apiTrust, ApiError,
  type BankValidation, type Contractor, type ContractorHistory, type ContractorPatch, type OnboardingLink,
  type TaxFormStatus, type TaxFormType, type TaxStatus,
} from "@/lib/api";
import { formatMoney } from "@/lib/utils";

const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

export const NOT_TAX_ADVICE =
  "PaySilo tracks form status and dates only, never tax ids or documents. This is not tax advice - confirm requirements with your tax advisor.";

const FORM_LABEL: Record<TaxFormType, string> = { W9: "W-9", W8BEN: "W-8BEN", W8BENE: "W-8BEN-E", NONE: "No form" };

/** Plain-language field names for provider validation errors. */
export function friendlyField(path: string): string {
  const key = path.split(".").pop() ?? path;
  const map: Record<string, string> = {
    iban: "IBAN", swift_code: "SWIFT/BIC code", account_number: "account number",
    account_routing_value1: "routing / sort code", bank_details: "bank details",
  };
  return map[key] ?? key.replace(/_/g, " ");
}

// ------------------------------------------------------------------ badges
export function TrustBadges({ c, watch1099 }: { c: Contractor; watch1099: boolean }) {
  const pending = c.bank_verification_status === "PENDING_VERIFICATION";
  const form = c.tax_form_status;
  return (
    <span className="mt-1.5 flex flex-wrap gap-1.5">
      <Badge tone={pending ? "warning" : "success"}>{pending ? "Pending verification" : "Bank verified"}</Badge>
      {c.tax_status === "UNKNOWN" && <Badge tone="neutral">Tax status unknown</Badge>}
      {c.tax_status !== "UNKNOWN" && (
        <Badge tone={form === "RECEIVED" ? "success" : form === "EXPIRED" ? "danger" : "warning"}>
          {form === "RECEIVED"
            ? `${FORM_LABEL[c.tax_form_type]} received`
            : form === "EXPIRED"
              ? `${FORM_LABEL[c.tax_form_type]} expired`
              : `${c.tax_status === "US_PERSON" ? "W-9" : "W-8BEN"} missing`}
        </Badge>
      )}
      {watch1099 && <Badge tone="coral">1099 watch</Badge>}
    </span>
  );
}

// ------------------------------------------------------------------ modal shell
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-[60] grid place-items-center bg-ink-950/80 p-4" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title}
        className="max-h-[90dvh] w-full max-w-lg overflow-y-auto rounded-4xl border-2 border-ink-600 bg-ink-850 p-6 shadow-2xl focus:outline-none">
        <div className="mb-4 flex items-start justify-between gap-3">
          <h3 className="font-display text-xl font-semibold text-sand-100">{title}</h3>
          <button type="button" onClick={onClose} aria-label="Close" className="rounded-full p-1.5 text-sage-300 hover:bg-ink-700 hover:text-sand-100">
            <X className="size-5" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ verify
export function VerifyBankDialog({
  employerId, contractor, currentUserId, onClose, onVerified,
}: {
  employerId: string; contractor: Contractor; currentUserId: string; onClose: () => void; onVerified: (c: Contractor) => void;
}) {
  const [history, setHistory] = useState<ContractorHistory | null>(null);
  const [check, setCheck] = useState<BankValidation | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const iRequested = contractor.bank_change_requested_by_id === currentUserId;

  useEffect(() => {
    let live = true;
    Promise.all([apiTrust.history(employerId, contractor.id), apiTrust.bankCheck(employerId, contractor.id)])
      .then(([h, v]) => { if (live) { setHistory(h); setCheck(v); } })
      .catch((e) => live && setError(errMsg(e)))
      .finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [employerId, contractor.id]);

  const change = history?.changes.find((c) => (c.group === "bank" || c.group === "onboarding") && !/verified/i.test(c.summary.split(":")[0]));
  const when = contractor.bank_details_changed_at ? new Date(contractor.bank_details_changed_at).toLocaleString() : null;

  async function verify() {
    setBusy(true);
    setError(null);
    try {
      onVerified(await apiTrust.verifyBank(employerId, contractor.id));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Verify bank change for ${contractor.full_name}`} onClose={onClose}>
      <div className="grid gap-4">
        {error && <PageError message={error} />}
        <div className="rounded-2xl bg-ink-950/50 p-4">
          <p className="text-xs font-extrabold uppercase tracking-widest text-sage-500">What changed{when ? ` · ${when}` : ""}</p>
          {loading ? (
            <p className="mt-2 text-sm text-sage-400">Loading…</p>
          ) : (
            <p className="mt-2 text-sm font-semibold text-sand-100">{change?.summary ?? "Bank details were updated."}</p>
          )}
          {change && <p className="mt-1 text-xs font-semibold text-sage-500">By {change.changed_by ?? "unknown"}. Only the last 4 characters are shown.</p>}
        </div>
        <div className="rounded-2xl bg-ink-950/50 p-4">
          <p className="text-xs font-extrabold uppercase tracking-widest text-sage-500">Bank check</p>
          {loading && <p className="mt-2 text-sm text-sage-400">Checking with the bank network…</p>}
          {check && check.valid && <p className="mt-2 flex items-center gap-2 text-sm font-bold text-mint-400"><Check className="size-4" /> These details pass validation.</p>}
          {check && !check.valid && check.checked && (
            <p className="mt-2 text-sm font-bold text-coral-300">
              These details don&apos;t pass validation. Fix: {check.errors.map(friendlyField).join(", ") || "bank details"}.
            </p>
          )}
          {check && !check.checked && <p className="mt-2 text-sm font-bold text-sand-300">The check could not run right now. Try again shortly.</p>}
        </div>

        {iRequested ? (
          <div className="rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 p-4 text-sm font-semibold text-sand-200">
            <p className="font-extrabold text-sand-100">You made this change, so a second person must verify it.</p>
            <p className="mt-1">Ask a colleague (owner, admin or approver) to confirm it with {contractor.full_name} by phone or another channel you already trust, then verify it here.</p>
          </div>
        ) : (
          <>
            <label className="flex cursor-pointer items-start gap-3 rounded-2xl border-2 border-ink-600 bg-ink-900/60 p-4">
              <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} className="mt-1 size-5 accent-coral-500" />
              <span className="text-sm font-semibold text-sand-100">
                I confirmed this change with the contractor through a known, independent channel (not by replying to the message that asked for it).
              </span>
            </label>
            <div className="flex flex-wrap gap-3">
              <Button variant="success" disabled={!confirmed || busy || loading || (check?.checked === true && !check.valid)} onClick={() => void verify()}>
                {busy ? <Loader2 className="animate-spin" /> : <ShieldCheck />} Verify bank details
              </Button>
              <Button variant="ghost" onClick={onClose}>Cancel</Button>
            </div>
          </>
        )}
        {iRequested && <div><Button variant="outline" size="sm" onClick={onClose}>Close</Button></div>}
      </div>
    </Modal>
  );
}

// ------------------------------------------------------------------ edit
export function EditContractorPanel({
  employerId, contractor, onSaved, onClose,
}: {
  employerId: string; contractor: Contractor; onSaved: (c: Contractor, note: string) => void; onClose: () => void;
}) {
  const c = contractor;
  const [form, setForm] = useState({
    full_name: c.full_name, email: c.email ?? "", country: c.country, currency: c.currency, payout_method: c.payout_method as "SWIFT" | "LOCAL",
    default_amount: c.default_amount ?? "", priority: String(c.priority ?? 100), iban: "", swift_code: "", account_number: "", routing_code: "",
    tax_status: c.tax_status as TaxStatus, tax_form_type: c.tax_form_type as TaxFormType,
    tax_form_status: (c.tax_form_status === "EXPIRED" ? "RECEIVED" : c.tax_form_status) as TaxFormStatus,
    tax_form_received_at: c.tax_form_received_at ?? "", tax_form_expires_at: c.tax_form_expires_at ?? "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  const bankTouched =
    !!(form.iban || form.swift_code || form.account_number || form.routing_code) ||
    form.payout_method !== c.payout_method || form.currency.toUpperCase() !== c.currency;

  async function save(ev: React.FormEvent) {
    ev.preventDefault();
    setSaving(true);
    setError(null);
    setFields({});
    const body: ContractorPatch = {};
    if (form.full_name.trim() !== c.full_name) body.full_name = form.full_name.trim();
    if ((form.email.trim() || null) !== c.email) body.email = form.email.trim() || null;
    if (form.country.trim().toUpperCase() !== c.country) body.country = form.country.trim().toUpperCase();
    if (form.currency.trim().toUpperCase() !== c.currency) body.currency = form.currency.trim().toUpperCase();
    if (form.payout_method !== c.payout_method) body.payout_method = form.payout_method;
    if ((form.default_amount.trim() || null) !== c.default_amount) body.default_amount = form.default_amount.trim() || null;
    if (form.priority.trim() !== "" && Number(form.priority) !== (c.priority ?? 100)) body.priority = Math.max(0, Math.min(9999, Math.floor(Number(form.priority)) || 0));
    for (const k of ["iban", "swift_code", "account_number", "routing_code"] as const) {
      if (form[k].trim()) body[k] = form[k].trim(); // blank = keep what is stored
    }
    if (form.tax_status !== c.tax_status) body.tax_status = form.tax_status;
    if (form.tax_form_type !== c.tax_form_type) body.tax_form_type = form.tax_form_type;
    if (form.tax_form_status !== c.tax_form_status) body.tax_form_status = form.tax_form_status;
    if ((form.tax_form_received_at || null) !== c.tax_form_received_at) body.tax_form_received_at = form.tax_form_received_at || null;
    if ((form.tax_form_expires_at || null) !== c.tax_form_expires_at) body.tax_form_expires_at = form.tax_form_expires_at || null;
    if (Object.keys(body).length === 0) {
      setError("Nothing changed yet.");
      setSaving(false);
      return;
    }
    try {
      const res = await apiTrust.patchContractor(employerId, c.id, body);
      let note = `${res.contractor.full_name} updated.`;
      if (res.bank_change_pending) {
        note = `Bank details saved for ${res.contractor.full_name}. They are on hold until a second person verifies the change.`;
        if (res.bank_validation && !res.bank_validation.valid && res.bank_validation.checked) {
          note += ` Heads up: the bank check flagged ${res.bank_validation.errors.map(friendlyField).join(", ")}.`;
        }
      }
      onSaved(res.contractor, note);
    } catch (e) {
      if (e instanceof ApiError) { setFields(e.fields); setError(e.message); } else setError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }

  const fe = (n: string) => (fields[n] ? <p className="mt-1 text-sm font-bold text-coral-300">{fields[n]}</p> : null);

  return (
    <Card className="p-5">
      <form onSubmit={(e) => void save(e)} className="grid gap-6" noValidate>
        <div className="flex items-start justify-between gap-3">
          <h3 className="font-display text-xl font-semibold text-sand-100">Edit {c.full_name}</h3>
          <Button type="button" variant="ghost" size="sm" onClick={onClose}>Close</Button>
        </div>
        {error && <PageError message={error} />}

        <fieldset className="grid gap-4 sm:grid-cols-2">
          <legend className="mb-2 text-xs font-extrabold uppercase tracking-widest text-sage-500">Profile</legend>
          <div><Label htmlFor="e-name">Full name</Label><Input id="e-name" value={form.full_name} onChange={set("full_name")} className="mt-1.5" />{fe("full_name")}</div>
          <div><Label htmlFor="e-email">Email</Label><Input id="e-email" type="email" value={form.email} onChange={set("email")} className="mt-1.5" />{fe("email")}</div>
          <div><Label htmlFor="e-country">Country (2-letter code)</Label><Input id="e-country" value={form.country} onChange={set("country")} maxLength={2} className="mt-1.5" />{fe("country")}</div>
          <div><Label htmlFor="e-amount">Default amount</Label><Input id="e-amount" type="number" min="0" step="0.01" value={form.default_amount} onChange={set("default_amount")} className="mt-1.5" />{fe("default_amount")}</div>
          <div className="sm:col-span-2">
            <Label htmlFor="e-priority">Payment priority</Label>
            <Input id="e-priority" type="number" min="0" max="9999" step="1" value={form.priority} onChange={set("priority")} className="mt-1.5 sm:max-w-40" />{fe("priority")}
            <p className="mt-1 text-xs font-semibold text-sage-500">Lower numbers are paid first (default 100). It only matters when the employer is set to Partial and the wallet is short.</p>
          </div>
        </fieldset>

        <fieldset className="grid gap-4 sm:grid-cols-2">
          <legend className="mb-2 text-xs font-extrabold uppercase tracking-widest text-sage-500">Payout details</legend>
          <p className="text-sm text-sage-300 sm:col-span-2">
            Stored: {c.payout_method}{c.iban_last4 ? ` · IBAN ····${c.iban_last4}` : ""}{c.account_last4 ? ` · account ····${c.account_last4}` : ""}.
            Leave a field blank to keep what is stored.
          </p>
          <div><Label htmlFor="e-currency">Pay currency</Label><Input id="e-currency" value={form.currency} onChange={set("currency")} maxLength={3} className="mt-1.5" />{fe("currency")}</div>
          <div>
            <Label htmlFor="e-method">Payout method</Label>
            <NativeSelect id="e-method" value={form.payout_method} onChange={set("payout_method")} className="mt-1.5">
              <option value="SWIFT">SWIFT</option><option value="LOCAL">Local</option>
            </NativeSelect>
          </div>
          <div><Label htmlFor="e-iban">New IBAN</Label><Input id="e-iban" value={form.iban} onChange={set("iban")} autoComplete="off" className="mt-1.5" />{fe("iban")}</div>
          <div><Label htmlFor="e-swift">New SWIFT / BIC code</Label><Input id="e-swift" value={form.swift_code} onChange={set("swift_code")} autoComplete="off" className="mt-1.5" />{fe("swift_code")}</div>
          <div><Label htmlFor="e-acct">New account number</Label><Input id="e-acct" value={form.account_number} onChange={set("account_number")} autoComplete="off" className="mt-1.5" />{fe("account_number")}</div>
          <div><Label htmlFor="e-routing">New routing / sort code</Label><Input id="e-routing" value={form.routing_code} onChange={set("routing_code")} autoComplete="off" className="mt-1.5" /></div>
          {bankTouched && (
            <p className="rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-4 py-3 text-sm font-semibold text-sand-200 sm:col-span-2">
              Changing payout details puts {c.full_name} on hold until a second person confirms the change with them through a known channel.
            </p>
          )}
        </fieldset>

        <fieldset className="grid gap-4 sm:grid-cols-2">
          <legend className="mb-2 text-xs font-extrabold uppercase tracking-widest text-sage-500">Tax status and form</legend>
          <div>
            <Label htmlFor="e-tax">Tax status</Label>
            <NativeSelect id="e-tax" value={form.tax_status} onChange={set("tax_status")} className="mt-1.5">
              <option value="UNKNOWN">Not set</option><option value="US_PERSON">US person</option><option value="NON_US">Non-US person</option>
            </NativeSelect>
          </div>
          <div>
            <Label htmlFor="e-ftype">Form</Label>
            <NativeSelect id="e-ftype" value={form.tax_form_type} onChange={set("tax_form_type")} className="mt-1.5">
              <option value="NONE">None</option><option value="W9">W-9 (US person)</option>
              <option value="W8BEN">W-8BEN (individual, non-US)</option><option value="W8BENE">W-8BEN-E (entity, non-US)</option>
            </NativeSelect>
          </div>
          <div>
            <Label htmlFor="e-fstatus">Form status</Label>
            <NativeSelect id="e-fstatus" value={form.tax_form_status} onChange={set("tax_form_status")} className="mt-1.5">
              <option value="MISSING">Missing</option><option value="RECEIVED">Received</option>
            </NativeSelect>
          </div>
          <div><Label htmlFor="e-frecv">Received on</Label><Input id="e-frecv" type="date" value={form.tax_form_received_at} onChange={set("tax_form_received_at")} className="mt-1.5" /></div>
          <div><Label htmlFor="e-fexp">Expires on (W-8BEN is usually valid about 3 years)</Label><Input id="e-fexp" type="date" value={form.tax_form_expires_at} onChange={set("tax_form_expires_at")} className="mt-1.5" />{fe("tax_form_expires_at")}</div>
          <p className="text-xs font-semibold text-sage-500 sm:col-span-2">{NOT_TAX_ADVICE}</p>
        </fieldset>

        <div className="flex flex-wrap gap-3">
          <Button type="submit" variant="sage" disabled={saving}>{saving ? <Loader2 className="animate-spin" /> : <Check />} Save changes</Button>
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
        </div>
      </form>
    </Card>
  );
}

// ------------------------------------------------------------------ onboarding link
export function OnboardingLinkPanel({ employerId, contractor, onClose }: { employerId: string; contractor: Contractor; onClose: () => void }) {
  const [link, setLink] = useState<OnboardingLink | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      setLink(await apiTrust.createOnboardingLink(employerId, contractor.id));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }
  const url = link?.invite_path ? `${window.location.origin}${link.invite_path}` : "";
  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      setError("Could not copy automatically. Select the link and copy it by hand.");
    }
  }
  async function revoke() {
    if (!link) return;
    try {
      setLink(await apiTrust.revokeOnboardingLink(employerId, contractor.id, link.id));
    } catch (e) {
      setError(errMsg(e));
    }
  }

  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-xl font-semibold text-sand-100">Onboarding link for {contractor.full_name}</h3>
          <p className="mt-1 max-w-xl text-sm text-sage-300">
            Send this link to {contractor.full_name.split(" ")[0]}. They enter their own payout details and tax status; you then verify
            the details before any payment. The link works once and expires in 14 days.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
      </div>
      {error && <div className="mt-3"><PageError message={error} /></div>}
      {!link && (
        <div className="mt-4"><Button variant="sage" onClick={() => void create()} disabled={busy}>{busy ? <Loader2 className="animate-spin" /> : null} Create link</Button></div>
      )}
      {link && link.status === "PENDING" && (
        <div className="mt-4 grid gap-3">
          <div className="flex flex-wrap gap-2">
            <Input readOnly value={url} aria-label="Onboarding link" onFocus={(e) => e.currentTarget.select()} className="min-w-0 flex-1 basis-72" />
            <Button variant={copied ? "success" : "sand"} onClick={() => void copy()}>{copied ? <Check /> : <Copy />} {copied ? "Copied" : "Copy link"}</Button>
          </div>
          <p className="text-sm font-bold text-sand-300">This link is shown only once. Copy it now; creating a new one cancels this one.</p>
          <div><Button variant="ghost" size="sm" onClick={() => void revoke()}>Revoke this link</Button></div>
        </div>
      )}
      {link && link.status !== "PENDING" && <p className="mt-4 text-sm font-bold text-sage-300">This link is {link.status.toLowerCase()}.</p>}
    </Card>
  );
}

// ------------------------------------------------------------------ history
export function HistoryPanel({ employerId, contractor, onClose }: { employerId: string; contractor: Contractor; onClose: () => void }) {
  const [data, setData] = useState<ContractorHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const changesPager = usePagination(data?.changes ?? [], 5, { syncUrl: false });
  const paymentsPager = usePagination(data?.payments ?? [], 5, { syncUrl: false });
  useEffect(() => {
    apiTrust.history(employerId, contractor.id).then(setData).catch((e) => setError(errMsg(e)));
  }, [employerId, contractor.id]);

  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <h3 className="font-display text-xl font-semibold text-sand-100">History for {contractor.full_name}</h3>
        <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
      </div>
      {error && <div className="mt-3"><PageError message={error} /></div>}
      {!data && !error && <p className="mt-3 text-sm text-sage-400">Loading…</p>}
      {data && (
        <div className="mt-4 grid gap-6 md:grid-cols-2">
          <div>
            <p className="mb-2 text-xs font-extrabold uppercase tracking-widest text-sage-500">Changes</p>
            {data.changes.length === 0 ? <p className="text-sm text-sage-400">No changes recorded yet.</p> : (
              <ul className="grid gap-2">
                {changesPager.pageItems.map((ch) => (
                  <li key={ch.id} className="rounded-2xl bg-ink-950/40 px-4 py-3">
                    <p className="flex flex-wrap items-center gap-2 text-sm font-semibold text-sand-100">
                      <Badge tone={ch.group === "bank" ? "warning" : "neutral"}>{ch.group}</Badge>
                      <span className="min-w-0 break-words">{ch.summary}</span>
                    </p>
                    <p className="mt-1 text-xs font-semibold text-sage-500">{ch.changed_by ?? "System"} · {new Date(ch.created_at).toLocaleString()}</p>
                  </li>
                ))}
              </ul>
            )}
            <Pagination state={changesPager} label="Changes" sizes={[5, 10]} />
          </div>
          <div>
            <p className="mb-2 text-xs font-extrabold uppercase tracking-widest text-sage-500">Payments</p>
            {data.payments.length === 0 ? <p className="text-sm text-sage-400">No payments yet.</p> : (
              <ul className="grid gap-2">
                {paymentsPager.pageItems.map((p, i) => (
                  <li key={`${p.run_id}-${i}`} className="flex items-baseline justify-between gap-3 rounded-2xl bg-ink-950/40 px-4 py-3">
                    <span className="min-w-0 text-sm font-semibold text-sage-300">
                      <span className="block truncate">{p.period_label ?? "Payroll run"}</span>
                      <span className="block text-xs text-sage-500">{new Date(p.created_at).toLocaleDateString()} · {p.status}</span>
                    </span>
                    <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(p.amount, p.currency)}</span>
                  </li>
                ))}
              </ul>
            )}
            <Pagination state={paymentsPager} label="Payments" sizes={[5, 10]} />
          </div>
        </div>
      )}
    </Card>
  );
}
