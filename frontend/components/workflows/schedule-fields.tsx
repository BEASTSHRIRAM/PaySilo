"use client";

import { useEffect, useState } from "react";
import { CalendarClock, TriangleAlert } from "lucide-react";

import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/select";
import { apiWorkflows, type SchedulePreview } from "@/lib/api";
import {
  COMMON_TIMEZONES, DEFAULT_TIMEZONE, SCHEDULE_CADENCES, SCHEDULE_WEEKDAYS, WEEKEND_RULES,
  cleanNodeData, validateScheduleData,
} from "@/lib/workflows";

export interface EmployerOption { id: string; name: string }

const label = "text-sm font-extrabold text-sage-400";
const help = "text-xs font-semibold text-sage-500";
const errCls = "text-xs font-bold text-coral-300";

/**
 * Schedule trigger configuration: scope (one employer), recurrence, time + timezone, weekend rule, and a
 * "Next 3 runs" preview computed by the SERVER (the browser never does calendar maths).
 * Controlled: `data` is the trigger node's data, `onChange` merges a patch.
 */
export function ScheduleFields({
  data, onChange, employers, requireEmployer = false, monthlyOnly = false,
}: {
  data: Record<string, unknown>;
  onChange: (patch: Record<string, unknown>) => void;
  employers: EmployerOption[];
  /** True when the graph has a payroll action: scope must be one employer or every employer. */
  requireEmployer?: boolean;
  /** The payroll wizard: the employer is chosen elsewhere and only the monthly recurrence is offered. */
  monthlyOnly?: boolean;
}) {
  const clean = cleanNodeData(data);
  const cadence = String(clean.cadence ?? "monthly");
  const lastDay = clean.last_day === true;
  const scope = clean.employer_id ? String(clean.employer_id) : clean.fan_out === "employers" ? "__all" : "__org";
  const errors = validateScheduleData(clean);
  const [preview, setPreview] = useState<SchedulePreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  // Server-computed next runs, debounced.
  const configKey = JSON.stringify({ ...clean, __: 0 });
  useEffect(() => {
    if (Object.keys(errors).length > 0) { setPreview(null); return; }
    let alive = true;
    const t = setTimeout(() => {
      apiWorkflows.schedulePreview(clean, 3)
        .then((p) => { if (alive) { setPreview(p); setPreviewError(null); } })
        .catch(() => { if (alive) setPreviewError("Could not compute the next runs right now."); });
    }, 300);
    return () => { alive = false; clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configKey]);

  function setScope(v: string) {
    if (v === "__all") onChange({ employer_id: undefined, fan_out: "employers" });
    else if (v === "__org") onChange({ employer_id: undefined, fan_out: "org" });
    else onChange({ employer_id: v, fan_out: "org" });
  }
  function setCadence(v: string) {
    if (v === "weekly") onChange({ cadence: v, day: 4, last_day: undefined, date: undefined });
    else if (v === "once") onChange({ cadence: v, day: undefined, last_day: undefined });
    else onChange({ cadence: v, day: 3, date: undefined });
  }
  const timezones = COMMON_TIMEZONES.includes(String(clean.timezone ?? DEFAULT_TIMEZONE))
    ? COMMON_TIMEZONES : [String(clean.timezone), ...COMMON_TIMEZONES];

  return (
    <div className="grid gap-4">
      {!monthlyOnly && <label className="grid gap-1.5">
        <span className={label}>Applies to {requireEmployer ? "*" : ""}</span>
        <NativeSelect value={scope} onChange={(e) => setScope(e.target.value)} aria-label="Scope: employer">
          <option value="__org" disabled={requireEmployer}>Whole organisation</option>
          <option value="__all">Every employer (one run each)</option>
          {employers.map((e) => <option key={e.id} value={e.id}>{e.name} only</option>)}
        </NativeSelect>
        <span className={help}>
          {requireEmployer
            ? "A payroll workflow prepares ONE employer's run from that employer's own wallet. Pick the employer."
            : "Pick one employer to scope the schedule to it."}
        </span>
        {requireEmployer && scope === "__org" && <span role="alert" className={errCls}>Choose an employer for a payroll schedule.</span>}
      </label>}

      {!monthlyOnly && <label className="grid gap-1.5">
        <span className={label}>Repeats *</span>
        <NativeSelect value={cadence} onChange={(e) => setCadence(e.target.value)} aria-label="Recurrence">
          {SCHEDULE_CADENCES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </NativeSelect>
        {errors.cadence && <span role="alert" className={errCls}>{errors.cadence}</span>}
      </label>}

      {cadence === "monthly" && (
        <div className="grid gap-2">
          <label className="grid gap-1.5">
            <span className={label}>Day of the month *</span>
            <Input type="number" min={1} max={31} inputMode="numeric" disabled={lastDay} aria-label="Day of the month"
              value={lastDay ? "" : String(clean.day ?? "")} placeholder="3"
              onChange={(e) => onChange({ day: e.target.value === "" ? undefined : Number(e.target.value) })} />
          </label>
          <label className="flex items-center gap-2.5 text-sm font-bold text-sand-100">
            <input type="checkbox" className="size-5 shrink-0 accent-coral-500" checked={lastDay} aria-label="Last day of the month"
              onChange={(e) => onChange(e.target.checked ? { last_day: true, day: undefined } : { last_day: undefined, day: 3 })} />
            Last day of the month
          </label>
          <span className={help}>If a month is shorter than the day (say 31), the last day of that month is used.</span>
          {errors.day && <span role="alert" className={errCls}>{errors.day}</span>}
        </div>
      )}
      {cadence === "weekly" && (
        <label className="grid gap-1.5">
          <span className={label}>Weekday *</span>
          <NativeSelect value={String(clean.day ?? 4)} onChange={(e) => onChange({ day: Number(e.target.value) })} aria-label="Weekday">
            {SCHEDULE_WEEKDAYS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </NativeSelect>
        </label>
      )}
      {cadence === "once" && (
        <label className="grid gap-1.5">
          <span className={label}>Date *</span>
          <Input type="date" value={String(clean.date ?? "")} onChange={(e) => onChange({ date: e.target.value || undefined })} aria-label="Run date" />
          {errors.date && <span role="alert" className={errCls}>{errors.date}</span>}
        </label>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
        <label className="grid gap-1.5">
          <span className={label}>Time of day</span>
          <Input type="time" value={String(clean.time ?? "09:00")} onChange={(e) => onChange({ time: e.target.value })} aria-label="Time of day" />
          {errors.time && <span role="alert" className={errCls}>{errors.time}</span>}
        </label>
        <label className="grid gap-1.5">
          <span className={label}>Timezone</span>
          <NativeSelect value={String(clean.timezone ?? DEFAULT_TIMEZONE)} onChange={(e) => onChange({ timezone: e.target.value })} aria-label="Timezone">
            {timezones.map((z) => <option key={z} value={z}>{z}</option>)}
          </NativeSelect>
        </label>
      </div>

      <label className="grid gap-1.5">
        <span className={label}>If it lands on a weekend</span>
        <NativeSelect value={String(clean.weekend_rule ?? "as_is")} onChange={(e) => onChange({ weekend_rule: e.target.value })} aria-label="Weekend rule">
          {WEEKEND_RULES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </NativeSelect>
        <span className={help}>Weekends only. Public holidays are not modelled, so check the date yourself.</span>
      </label>

      <div className="grid gap-2 rounded-2xl border-2 border-ink-600 bg-ink-950/50 p-3" aria-live="polite" data-testid="next-runs">
        <p className="flex items-center gap-2 text-xs font-extrabold uppercase tracking-wider text-sage-400">
          <CalendarClock className="size-4" /> Next 3 runs
        </p>
        {Object.keys(errors).length > 0 && <p className={help}>Finish the schedule above to see when it will run.</p>}
        {previewError && <p className={errCls}>{previewError}</p>}
        {preview && !preview.valid && (
          <p role="alert" className="flex items-start gap-1.5 text-sm font-bold text-coral-300"><TriangleAlert className="mt-0.5 size-4 shrink-0" /> {preview.message}</p>
        )}
        {preview?.valid && (
          <>
            <ol className="grid gap-1">
              {preview.next_runs.map((r) => (
                <li key={r.key} className="text-sm font-bold text-sand-100">
                  {r.weekday}, {r.local} <span className="font-semibold text-sage-500">{r.timezone}</span>
                </li>
              ))}
              {preview.next_runs.length === 0 && <li className="text-sm font-semibold text-sage-400">No future runs: that date has passed.</li>}
            </ol>
            <p className="text-xs font-semibold text-sage-500">{preview.summary}. Calculated by the server.</p>
          </>
        )}
      </div>
    </div>
  );
}
