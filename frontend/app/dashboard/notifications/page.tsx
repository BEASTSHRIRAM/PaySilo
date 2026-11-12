"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Mail, RefreshCw, X } from "lucide-react";

import { AppBar } from "@/components/dashboard/app-bar";
import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/select";
import { api, apiFunding, ApiError, type AppNotification, type Session } from "@/lib/api";
import { fmtDateTime, KIND_LABEL } from "@/lib/funding-ui";
import { cn } from "@/lib/utils";

const PAGE = 25;

function StatusBadge({ n }: { n: AppNotification }) {
  if (n.status === "FAILED") return <Badge tone="danger">Failed</Badge>;
  if (n.status === "QUEUED") return <Badge tone="warning">Queued</Badge>;
  return <Badge tone="success">{n.simulated ? "Sent (simulated)" : "Sent"}</Badge>;
}

export default function NotificationsPage() {
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [items, setItems] = useState<AppNotification[]>([]);
  const [total, setTotal] = useState(0);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<AppNotification | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [me, list] = await Promise.all([api.me(), apiFunding.notifications({ kind, status, limit: PAGE, offset })]);
      setSession(me);
      setItems(list.items);
      setTotal(list.total);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login?next=/dashboard/notifications");
        return;
      }
      setError(err instanceof ApiError ? err.message : "Could not load notifications.");
    }
    setLoading(false);
  }, [kind, status, offset, router]);

  useEffect(() => { void load(); }, [load]);

  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}
      <main className="mx-auto mt-8 grid max-w-6xl gap-6 px-4 sm:px-6">
        <section className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Notifications</h1>
            <p className="mt-1 max-w-2xl text-sage-300">Every email PaySilo has prepared for your employers. Without a mail provider, delivery is simulated and nothing leaves this machine.</p>
          </div>
          <Button variant="outline" onClick={() => { setLoading(true); void load(); }}><RefreshCw /> Refresh</Button>
        </section>

        <div className="flex flex-wrap gap-3">
          <div className="grid gap-1.5">
            <Label htmlFor="f-kind">Type</Label>
            <NativeSelect id="f-kind" className="w-56" value={kind} onChange={(e) => { setKind(e.target.value); setOffset(0); }}>
              <option value="">All types</option>
              {Object.entries(KIND_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </NativeSelect>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="f-status">Status</Label>
            <NativeSelect id="f-status" className="w-44" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
              <option value="">Any status</option>
              <option value="SENT">Sent</option>
              <option value="QUEUED">Queued</option>
              <option value="FAILED">Failed</option>
            </NativeSelect>
          </div>
        </div>

        {loading && <SectionLoading label="Loading the outbox…" />}
        {error && <SectionError message={error} onRetry={() => void load()} />}
        {!loading && !error && items.length === 0 && (
          <SectionEmpty title="Nothing in the outbox" body="Send a funding reminder from the Funding planner, or run the reminder sweep, and the messages show up here." />
        )}

        {!loading && items.length > 0 && (
          <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]">
            <Card className="overflow-hidden p-0">
              <ul className="divide-y divide-ink-800">
                {items.map((n) => (
                  <li key={n.id}>
                    <button
                      type="button" onClick={() => setSelected(n)} aria-pressed={selected?.id === n.id}
                      className={cn("grid w-full gap-1.5 px-4 py-3 text-left transition-colors hover:bg-ink-800/60 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-inset focus-visible:ring-sage-400/50",
                        selected?.id === n.id && "bg-ink-800/80")}
                    >
                      <span className="flex flex-wrap items-center gap-2">
                        <Badge tone={n.kind === "FUNDING_OVERDUE" ? "danger" : "sage"}>{KIND_LABEL[n.kind] ?? n.kind}</Badge>
                        <StatusBadge n={n} />
                        <span className="ml-auto text-xs font-semibold text-sage-500">{fmtDateTime(n.sent_at ?? n.created_at)}</span>
                      </span>
                      <span className="break-words font-bold text-sand-100">{n.subject}</span>
                      <span className="break-all text-sm font-semibold text-sage-400">To {n.recipient}{n.employer_name ? ` · ${n.employer_name}` : ""}</span>
                    </button>
                  </li>
                ))}
              </ul>
              <div className="flex items-center justify-between gap-3 border-t-2 border-ink-800 px-4 py-3 text-sm font-semibold text-sage-400">
                <span>{offset + 1}-{Math.min(offset + PAGE, total)} of {total}</span>
                <span className="flex gap-2">
                  <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Newer</Button>
                  <Button size="sm" variant="outline" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Older</Button>
                </span>
              </div>
            </Card>

            <Card className="p-5 lg:sticky lg:top-40">
              {!selected ? (
                <p className="flex items-center gap-2 text-sm font-semibold text-sage-400"><Mail className="size-4" /> Select a message to read it.</p>
              ) : (
                <div className="grid gap-3">
                  <div className="flex items-start justify-between gap-2">
                    <h2 className="break-words font-display text-lg font-semibold text-sand-100">{selected.subject}</h2>
                    <button type="button" aria-label="Close message" onClick={() => setSelected(null)} className="shrink-0 rounded-full p-1 hover:bg-ink-700"><X className="size-4 text-sage-300" /></button>
                  </div>
                  <dl className="grid gap-0.5 text-sm font-semibold text-sage-300">
                    <div className="break-all"><dt className="inline text-sage-500">To: </dt><dd className="inline">{selected.recipient}</dd></div>
                    <div><dt className="inline text-sage-500">Sent: </dt><dd className="inline">{fmtDateTime(selected.sent_at ?? selected.created_at)}</dd></div>
                  </dl>
                  <div className="flex flex-wrap gap-2"><StatusBadge n={selected} /></div>
                  {selected.simulated && (
                    <p className="rounded-2xl bg-sand-200/10 px-3 py-2 text-xs font-bold text-sand-300">
                      Simulated delivery: this message was recorded here but not sent to anyone.
                    </p>
                  )}
                  {selected.error && <p role="alert" className="text-sm font-bold text-coral-300">{selected.error}</p>}
                  <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-2xl bg-ink-950/50 p-4 font-sans text-sm leading-relaxed text-sand-100">{selected.body}</pre>
                </div>
              )}
            </Card>
          </div>
        )}
      </main>
    </div>
  );
}
