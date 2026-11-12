"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowUpRight, CalendarClock, CalendarDays, ChevronLeft, ChevronRight, Info } from "lucide-react";

import { AppBar } from "@/components/dashboard/app-bar";
import { PayrollScheduleDialog } from "@/components/workflows/payroll-schedule-dialog";
import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, apiV3, apiWorkflows, ApiError, type CalendarEvent, type Employer, type ScheduleOccurrence, type Session } from "@/lib/api";
import { EDITOR_ROLES } from "@/lib/status";
import { cn } from "@/lib/utils";

const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

type EnrichedEvent = CalendarEvent & { employer_id: string; employer_name: string };

const KIND_META: Record<string, { label: string; chip: string; dot: string }> = {
  pay_date: { label: "Pay date", chip: "bg-coral-500 text-sand-50", dot: "bg-coral-500" },
  cutoff: { label: "Cut-off", chip: "bg-sand-300 text-ink-900", dot: "bg-sand-300" },
  funding_due: { label: "Funding due", chip: "bg-clay-400 text-sand-50", dot: "bg-clay-400" },
  period_start: { label: "Period start", chip: "bg-sage-500 text-ink-900", dot: "bg-sage-500" },
};
const kindMeta = (kind: string) => KIND_META[kind] ?? { label: kind.replaceAll("_", " "), chip: "bg-ink-600 text-sand-100", dot: "bg-ink-500" };

/** Only the pay date is a stored fact; the other kinds are derived from it. */
const isDerived = (kind: string) => kind !== "pay_date";

const EMPLOYER_ACCENTS = ["bg-coral-500", "bg-sage-500", "bg-sand-400", "bg-clay-400", "bg-mint-500", "bg-ink-500"];

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function isoOf(d: Date): string {
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

function monthGrid(cursor: Date): Date[] {
  const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
  const startOffset = (first.getDay() + 6) % 7; // Monday-first
  const start = new Date(first);
  start.setDate(first.getDate() - startOffset);
  return Array.from({ length: 42 }, (_, i) => {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    return d;
  });
}

export default function CalendarPage() {
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [employers, setEmployers] = useState<Employer[]>([]);
  const [events, setEvents] = useState<EnrichedEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [partialError, setPartialError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("ALL");
  const [cursor, setCursor] = useState(() => {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1);
  });
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [occurrences, setOccurrences] = useState<ScheduleOccurrence[]>([]);
  const [scheduleOpen, setScheduleOpen] = useState(false);

  const load = useCallback(async () => {
    setLoadError(null);
    setPartialError(null);
    try {
      const [me, overview] = await Promise.all([api.me(), api.overview()]);
      setSession(me);
      setEmployers(overview.employers);
      const results = await Promise.allSettled(
        overview.employers.map(async (e) => {
          const res = await apiV3.calendar(e.id);
          return res.events.map((ev): EnrichedEvent => ({
            ...ev,
            employer_id: ev.employer_id ?? e.id,
            employer_name: ev.employer_name ?? e.name,
          }));
        }),
      );
      const merged: EnrichedEvent[] = [];
      const failed: string[] = [];
      results.forEach((r, i) => {
        if (r.status === "fulfilled") merged.push(...r.value);
        else failed.push(overview.employers[i].name);
      });
      merged.sort((a, b) => a.date.localeCompare(b.date));
      setEvents(merged);
      if (failed.length > 0 && merged.length === 0 && overview.employers.length > 0) {
        setLoadError("The calendar could not be loaded for any employer. Try again.");
      } else if (failed.length > 0) {
        setPartialError(`Calendar unavailable for: ${failed.join(", ")}. Other employers are shown.`);
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login?next=/dashboard/calendar");
        return;
      }
      setLoadError(errMsg(err));
    }
    setLoading(false);
  }, [router]);

  useEffect(() => { void load(); }, [load]);

  // Scheduled workflow occurrences (read-only chips) for the visible 6-week grid.
  const loadOccurrences = useCallback(async () => {
    const g = monthGrid(cursor);
    try { setOccurrences(await apiWorkflows.occurrences(isoOf(g[0]), isoOf(g[g.length - 1]))); }
    catch { setOccurrences([]); }
  }, [cursor]);
  useEffect(() => { void loadOccurrences(); }, [loadOccurrences]);

  const accentFor = useCallback(
    (employerId: string) => {
      const idx = employers.findIndex((e) => e.id === employerId);
      return EMPLOYER_ACCENTS[(idx < 0 ? 0 : idx) % EMPLOYER_ACCENTS.length];
    },
    [employers],
  );

  const visible = useMemo(
    () => events.filter((ev) => filter === "ALL" || ev.employer_id === filter),
    [events, filter],
  );

  const byDay = useMemo(() => {
    const map = new Map<string, EnrichedEvent[]>();
    for (const ev of visible) {
      const list = map.get(ev.date) ?? [];
      list.push(ev);
      map.set(ev.date, list);
    }
    return map;
  }, [visible]);

  const visibleOccurrences = useMemo(
    () => occurrences.filter((o) => filter === "ALL" || o.employer_id === filter || o.employer_id === null),
    [occurrences, filter],
  );
  const occByDay = useMemo(() => {
    const map = new Map<string, ScheduleOccurrence[]>();
    for (const o of visibleOccurrences) map.set(o.date, [...(map.get(o.date) ?? []), o]);
    return map;
  }, [visibleOccurrences]);
  const canCreateSchedule = !!session && EDITOR_ROLES.includes(session.role);
  const grid = useMemo(() => monthGrid(cursor), [cursor]);
  const todayIso = isoOf(new Date());
  const monthName = cursor.toLocaleDateString("en-US", { month: "long", year: "numeric" });

  const agenda = useMemo(() => {
    if (selectedDay) return visible.filter((ev) => ev.date === selectedDay);
    return visible.filter((ev) => {
      const d = new Date(`${ev.date}T00:00:00`);
      return d.getFullYear() === cursor.getFullYear() && d.getMonth() === cursor.getMonth();
    });
  }, [visible, selectedDay, cursor]);

  function shiftMonth(delta: number) {
    setCursor((c) => new Date(c.getFullYear(), c.getMonth() + delta, 1));
    setSelectedDay(null);
  }

  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}

      <main className="mx-auto mt-8 grid max-w-6xl gap-8 px-4 sm:px-6">
        <section className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Payroll calendar</h1>
            <p className="mt-1 max-w-2xl text-sage-300">
              Pay dates, cut-offs and funding deadlines across every employer, from each employer&apos;s calendar.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {canCreateSchedule && employers.length > 0 && (
              <Button size="sm" className="w-full sm:w-auto" onClick={() => setScheduleOpen(true)}><CalendarClock /> Create payroll schedule</Button>
            )}
            <Button variant="outline" size="sm" onClick={() => shiftMonth(-1)} aria-label="Previous month">
              <ChevronLeft />
            </Button>
            <span className="min-w-44 text-center font-display text-lg font-semibold text-sand-100" aria-live="polite">{monthName}</span>
            <Button variant="outline" size="sm" onClick={() => shiftMonth(1)} aria-label="Next month">
              <ChevronRight />
            </Button>
            <Button
              variant="ghost" size="sm"
              onClick={() => { const n = new Date(); setCursor(new Date(n.getFullYear(), n.getMonth(), 1)); setSelectedDay(null); }}
            >
              Today
            </Button>
          </div>
        </section>

        {loading && <SectionLoading label="Loading calendars…" />}
        {loadError && <SectionError message={loadError} onRetry={() => void load()} />}
        {partialError && <SectionError message={partialError} onRetry={() => void load()} />}

        {!loading && !loadError && employers.length === 0 && (
          <SectionEmpty title="No employers yet" body="Calendars appear once an employer with payroll runs exists." />
        )}

        {!loading && employers.length > 0 && (
          <>
            <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by employer">
              <button
                type="button"
                onClick={() => setFilter("ALL")}
                aria-pressed={filter === "ALL"}
                className={cn(
                  "rounded-full border-2 px-4 py-1.5 text-sm font-extrabold transition-colors",
                  filter === "ALL" ? "border-coral-500 bg-coral-500 text-sand-50" : "border-ink-600 bg-ink-850/60 text-sage-300 hover:text-sand-100",
                )}
              >
                All employers
              </button>
              {employers.map((e) => (
                <button
                  key={e.id}
                  type="button"
                  onClick={() => setFilter(e.id)}
                  aria-pressed={filter === e.id}
                  className={cn(
                    "flex items-center gap-2 rounded-full border-2 px-4 py-1.5 text-sm font-extrabold transition-colors",
                    filter === e.id ? "border-sage-400 bg-sage-500/20 text-sand-100" : "border-ink-600 bg-ink-850/60 text-sage-300 hover:text-sand-100",
                  )}
                >
                  <span aria-hidden className={cn("size-2.5 rounded-full", accentFor(e.id))} /> {e.name}
                </button>
              ))}
            </div>

            <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
              <Card className="overflow-hidden">
                <div className="grid grid-cols-[repeat(7,minmax(0,1fr))] bg-ink-900/70 text-center text-xs font-extrabold uppercase tracking-widest text-sage-500">
                  {WEEKDAYS.map((d) => (
                    <div key={d} className="py-3">{d}</div>
                  ))}
                </div>
                <div className="grid grid-cols-[repeat(7,minmax(0,1fr))]">
                  {grid.map((day) => {
                    const iso = isoOf(day);
                    const inMonth = day.getMonth() === cursor.getMonth();
                    const dayEvents = byDay.get(iso) ?? [];
                    const isToday = iso === todayIso;
                    const isSelected = iso === selectedDay;
                    return (
                      <button
                        key={iso}
                        type="button"
                        onClick={() => setSelectedDay(isSelected ? null : iso)}
                        aria-pressed={isSelected}
                        aria-label={`${day.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" })}${dayEvents.length ? `, ${dayEvents.length} event${dayEvents.length === 1 ? "" : "s"}` : ""}`}
                        className={cn(
                          "min-h-24 min-w-0 overflow-hidden border border-ink-800/70 p-1.5 text-left align-top transition-colors sm:min-h-28 sm:p-2",
                          inMonth ? "bg-ink-850/40" : "bg-ink-950/50",
                          isSelected ? "bg-ink-800 ring-2 ring-inset ring-coral-500" : "hover:bg-ink-800/70",
                        )}
                      >
                        <span className={cn(
                          "inline-grid size-7 place-items-center rounded-full font-display text-sm font-semibold tabular-nums",
                          isToday ? "bg-coral-500 text-sand-50" : inMonth ? "text-sand-100" : "text-sage-600",
                        )}>
                          {day.getDate()}
                        </span>
                        <span className="mt-1 grid gap-1">
                          {(occByDay.get(iso) ?? []).slice(0, 2).map((o) => (
                            <Link key={`${o.workflow_id}-${o.date}`} href={`/dashboard/workflows/${o.workflow_id}`}
                                  onClick={(e) => e.stopPropagation()}
                                  title={`Scheduled workflow: ${o.workflow_name} at ${o.time} ${o.timezone}`}
                                  className="flex min-w-0 items-center gap-1 truncate rounded-md border border-sage-500/60 bg-sage-500/15 px-1.5 py-0.5 text-[11px] font-bold leading-tight text-sage-300 hover:bg-sage-500/30">
                              <CalendarClock className="size-3 shrink-0" />
                              <span className="truncate">{o.employer_name ?? o.workflow_name} {o.time}</span>
                            </Link>
                          ))}
                          {dayEvents.slice(0, 3).map((ev, i) => (
                            <span key={`${ev.date}-${ev.kind}-${ev.run_id ?? ""}-${i}`}
                                  className={cn("flex items-center gap-1 truncate rounded-md px-1.5 py-0.5 text-[11px] font-bold leading-tight", kindMeta(ev.kind).chip)}>
                              <span aria-hidden className={cn("size-1.5 shrink-0 rounded-full", accentFor(ev.employer_id))} />
                              <span className="truncate">{ev.label}</span>
                            </span>
                          ))}
                          {dayEvents.length > 3 && (
                            <span className="px-1 text-[11px] font-bold text-sage-400">+{dayEvents.length - 3} more</span>
                          )}
                        </span>
                      </button>
                    );
                  })}
                </div>
                <div className="flex flex-wrap gap-x-5 gap-y-2 border-t-2 border-ink-700 bg-ink-900/50 px-4 py-3">
                  {Object.entries(KIND_META).map(([kind, meta]) => (
                    <span key={kind} className="flex items-center gap-1.5 text-xs font-bold text-sage-300">
                      <span aria-hidden className={cn("size-2.5 rounded-full", meta.dot)} /> {meta.label}
                      {isDerived(kind) && <span className="font-semibold text-sage-500">(derived)</span>}
                    </span>
                  ))}
                  <span className="flex items-center gap-1.5 text-xs font-bold text-sage-300">
                    <CalendarClock className="size-3.5 text-sage-400" /> Scheduled workflow <span className="font-semibold text-sage-500">(read-only, links to the workflow)</span>
                  </span>
                </div>
              </Card>

              <section aria-label="Agenda">
                <h2 className="mb-3 flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
                  <CalendarDays className="size-5 text-sage-400" />
                  {selectedDay
                    ? new Date(`${selectedDay}T00:00:00`).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" })
                    : `Agenda · ${monthName}`}
                </h2>
                {selectedDay && (
                  <Button variant="ghost" size="sm" className="mb-3" onClick={() => setSelectedDay(null)}>
                    Show the whole month
                  </Button>
                )}
                {(selectedDay ? visibleOccurrences.filter((o) => o.date === selectedDay) : visibleOccurrences.filter((o) => o.date.startsWith(`${cursor.getFullYear()}-${`${cursor.getMonth() + 1}`.padStart(2, "0")}`))).map((o) => (
                  <Card key={`agenda-${o.workflow_id}-${o.date}`} className="mb-2 p-4">
                    <p className="flex items-center gap-2 font-bold text-sand-100"><CalendarClock className="size-4 text-sage-400" /> <span className="truncate">{o.workflow_name}</span></p>
                    <p className="mt-1 text-sm font-semibold text-sage-400">
                      {new Date(`${o.date}T00:00:00`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })} · {o.time} {o.timezone}{o.employer_name ? ` · ${o.employer_name}` : ""}
                    </p>
                    <p className="mt-1 text-xs font-semibold text-sage-500">Scheduled workflow: prepares the run, then asks an approver.</p>
                    <Link href={`/dashboard/workflows/${o.workflow_id}`} className="mt-2 inline-flex items-center gap-1 text-sm font-bold text-sage-300 hover:text-sand-100">Open workflow <ArrowUpRight className="size-4" /></Link>
                  </Card>
                ))}
                {agenda.length === 0 ? (
                  <Card className="p-5">
                    <p className="font-semibold text-sage-400">
                      {events.length === 0
                        ? "No calendar events yet. Events appear once runs have pay dates."
                        : "Nothing scheduled in this view. Try another month or employer."}
                    </p>
                  </Card>
                ) : (
                  <ol className="grid gap-2">
                    {agenda.map((ev, i) => (
                      <li key={`${ev.date}-${ev.kind}-${ev.run_id ?? ""}-${i}`}>
                        <Card className="p-4">
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <p className="flex items-center gap-2 font-bold text-sand-100">
                                <span aria-hidden className={cn("size-2.5 shrink-0 rounded-full", accentFor(ev.employer_id))} />
                                <span className="truncate">{ev.label}</span>
                              </p>
                              <p className="mt-1 text-sm font-semibold text-sage-400">
                                {new Date(`${ev.date}T00:00:00`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })}
                                {" · "}{ev.employer_name}
                              </p>
                              {isDerived(ev.kind) && (
                                <p className="mt-1 text-xs font-semibold text-sage-500">Derived from pay date</p>
                              )}
                            </div>
                            <Badge tone={ev.kind === "pay_date" ? "coral" : ev.kind === "funding_due" ? "warning" : "sage"}>
                              {kindMeta(ev.kind).label}
                            </Badge>
                          </div>
                          {ev.run_id && (
                            <Link
                              href={`/dashboard/runs/${ev.run_id}`}
                              className="mt-2 inline-flex items-center gap-1 text-sm font-bold text-sage-300 hover:text-sand-100"
                            >
                              Open run <ArrowUpRight className="size-4" />
                            </Link>
                          )}
                        </Card>
                      </li>
                    ))}
                  </ol>
                )}
                <p className="mt-4 flex gap-2 text-xs font-semibold leading-relaxed text-sage-500">
                  <Info className="size-4 shrink-0" />
                  Pay dates are stored on each run. Cut-off, funding-due and period-start dates are derived from the pay date by the server and labelled as derived.
                </p>
              </section>
            </div>
          </>
        )}
      </main>
      <PayrollScheduleDialog open={scheduleOpen} onClose={() => setScheduleOpen(false)}
        employers={employers.map((e) => ({ id: e.id, name: e.name }))} employerId={filter !== "ALL" ? filter : null}
        onCreated={() => void loadOccurrences()} />
    </div>
  );
}
