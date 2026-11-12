"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { CalendarClock, Check, TriangleAlert, X } from "lucide-react";

import { ScheduleFields, type EmployerOption } from "@/components/workflows/schedule-fields";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/select";
import { ApiError, apiWorkflows, type WorkflowDetail } from "@/lib/api";
import { DEFAULT_TIMEZONE } from "@/lib/workflows";

/**
 * "Create payroll schedule": a prefilled wizard (employer, day, time) that builds the standard
 * Schedule -> Prepare payroll run -> Wait for approval workflow for ONE employer.
 * Nothing is switched on without the explicit confirm on the second step. The workflow only prepares the run and
 * asks a human; it never approves or pays.
 */
export function PayrollScheduleDialog({
  open, onClose, employers, employerId, onCreated,
}: {
  open: boolean;
  onClose: () => void;
  employers: EmployerOption[];
  employerId?: string | null;
  onCreated?: (wf: WorkflowDetail) => void;
}) {
  const [employer, setEmployer] = useState("");
  const [data, setData] = useState<Record<string, unknown>>({ cadence: "monthly", day: 3, time: "09:00", timezone: DEFAULT_TIMEZONE, weekend_rule: "as_is" });
  const [step, setStep] = useState<"form" | "confirm" | "done">("form");
  const [understood, setUnderstood] = useState(false);
  const [duplicates, setDuplicates] = useState<{ id: string; name: string; summary: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<WorkflowDetail | null>(null);

  useEffect(() => {
    if (!open) return;
    setEmployer(employerId ?? employers[0]?.id ?? "");
    setStep("form"); setUnderstood(false); setError(null); setCreated(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, employerId]);

  useEffect(() => {
    if (!open || !employer) { setDuplicates([]); return; }
    let alive = true;
    apiWorkflows.employerSchedules(employer).then((d) => { if (alive) setDuplicates(d); }).catch(() => { if (alive) setDuplicates([]); });
    return () => { alive = false; };
  }, [open, employer]);

  if (!open) return null;
  const employerName = employers.find((e) => e.id === employer)?.name ?? "this employer";
  const lastDay = data.last_day === true;
  const day = Number(data.day);
  const dayOk = lastDay || (Number.isInteger(day) && day >= 1 && day <= 31);
  const whenText = lastDay ? "the last day of every month" : `day ${day} of every month`;

  async function create(enable: boolean) {
    setBusy(true); setError(null);
    try {
      const wf = await apiWorkflows.createEmployerSchedule({
        employer_id: employer, day: lastDay ? null : day, last_day: lastDay,
        time: String(data.time ?? "09:00"), timezone: String(data.timezone ?? DEFAULT_TIMEZONE),
        weekend_rule: String(data.weekend_rule ?? "as_is"), enabled: enable,
      });
      setCreated(wf); setStep("done"); onCreated?.(wf);
    } catch (e) {
      setError(e instanceof ApiError ? (e.validationErrors[0] ?? e.message) : "Could not create the schedule. Try again.");
    }
    setBusy(false);
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto bg-ink-950/70 p-4" role="dialog" aria-modal="true" aria-label="Create payroll schedule">
      <Card className="grid max-h-[92dvh] w-full max-w-lg gap-4 overflow-y-auto p-5 sm:p-6">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100"><CalendarClock className="size-5 text-coral-400" /> Create payroll schedule</h3>
            <p className="mt-0.5 text-sm font-semibold text-sage-400">On the chosen day PaySilo prepares that employer&apos;s payroll run, emails the approvers, and waits. A person approves; the workflow never does.</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="rounded-full p-1.5 text-sage-300 hover:bg-ink-700"><X className="size-5" /></button>
        </div>

        {step === "form" && (
          <>
            <label className="grid gap-1.5">
              <span className="text-sm font-extrabold text-sage-400">Employer *</span>
              <NativeSelect value={employer} onChange={(e) => setEmployer(e.target.value)} aria-label="Employer">
                {employers.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
              </NativeSelect>
            </label>
            <ScheduleFields data={{ ...data, employer_id: employer }} onChange={(p) => setData((d) => ({ ...d, ...p }))} employers={employers} monthlyOnly />
            {duplicates.length > 0 && (
              <div role="alert" className="grid gap-1 rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-3 py-2.5" data-testid="duplicate-warning">
                <p className="flex items-center gap-2 text-sm font-extrabold text-sand-200"><TriangleAlert className="size-4" /> {employerName} already has a payroll schedule</p>
                {duplicates.map((d) => (
                  <p key={d.id} className="text-xs font-semibold text-sand-200"><Link href={`/dashboard/workflows/${d.id}`} className="font-bold underline">{d.name}</Link>{d.summary ? `: ${d.summary}` : ""}</p>
                ))}
                <p className="text-xs font-semibold text-sand-200">Two enabled schedules would each try to prepare a run. Edit the existing one instead, or continue knowing you will have duplicates.</p>
              </div>
            )}
            {error && <p role="alert" className="text-sm font-bold text-coral-300">{error}</p>}
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
              <Button size="sm" disabled={!employer || !dayOk} onClick={() => setStep("confirm")}>Review</Button>
            </div>
          </>
        )}

        {step === "confirm" && (
          <>
            <div className="grid gap-2 rounded-2xl border-2 border-ink-600 bg-ink-950/50 p-4">
              <p className="text-sm font-extrabold text-sand-100">{employerName}: {whenText}, {String(data.time)} {String(data.timezone)}</p>
              <ol className="ml-4 list-decimal text-sm font-semibold text-sage-300">
                <li>Prepare the payroll run from the employer&apos;s active contractors and default amounts.</li>
                <li>If its own wallet covers it: email the approvers and wait for a person to approve.</li>
                <li>If not: raise a funding reminder instead. Nothing is paid.</li>
              </ol>
            </div>
            <label className="flex items-start gap-2.5 text-sm font-bold text-sand-100">
              <input type="checkbox" className="mt-0.5 size-5 shrink-0 accent-coral-500" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} aria-label="I understand" />
              I understand this will prepare {employerName}&apos;s payroll run on these dates and ask approvers to decide. It never approves or pays.
            </label>
            {error && <p role="alert" className="text-sm font-bold text-coral-300">{error}</p>}
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setStep("form")} disabled={busy}>Back</Button>
              <Button variant="outline" size="sm" onClick={() => void create(false)} disabled={busy}>Create, keep disabled</Button>
              <Button size="sm" onClick={() => void create(true)} disabled={busy || !understood}>{busy ? "Creating…" : "Create and enable"}</Button>
            </div>
          </>
        )}

        {step === "done" && created && (
          <div className="grid gap-3">
            <p className="flex items-center gap-2 text-sm font-extrabold text-mint-400"><Check className="size-4" /> {created.enabled ? "Schedule created and enabled." : "Schedule created, still disabled."}</p>
            <p className="text-sm font-semibold text-sage-300">{created.name}. {created.enabled ? "It will run at the next occurrence shown on the calendar." : "Open it to review and switch it on."}</p>
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
              <Button size="sm" asChild><Link href={`/dashboard/workflows/${created.id}`}>Open workflow</Link></Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
