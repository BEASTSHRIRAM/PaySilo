"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Lock, Send } from "lucide-react";

import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/select";
import { api, ApiError, type NotificationTemplate, type NotificationTemplates } from "@/lib/api";
import { cn } from "@/lib/utils";

const GROUP_ORDER = ["Account", "Employers and contractors", "Funding", "Payroll", "Spend", "Workflows"];

function SubTabs<T extends string>({ value, onChange, items }: { value: T; onChange: (v: T) => void; items: [T, string][] }) {
  return (
    <div role="tablist" className="inline-flex gap-1 rounded-2xl bg-ink-900/80 p-1">
      {items.map(([k, label]) => (
        <button key={k} type="button" role="tab" aria-selected={value === k} onClick={() => onChange(k)}
                className={cn("rounded-xl px-4 py-1.5 text-sm font-extrabold transition-colors focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/50",
                  value === k ? "bg-ink-700 text-sand-100" : "text-sage-400 hover:text-sand-100")}>
          {label}
        </button>
      ))}
    </div>
  );
}

export function TemplatesPanel({ userEmail }: { userEmail: string }) {
  const [data, setData] = useState<NotificationTemplates | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [preview, setPreview] = useState<"email" | "text">("email");
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await api.notificationTemplates();
      setData(d);
      setSelectedKey((k) => k ?? d.items[0]?.key ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the email templates.");
    }
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const groups = useMemo(() => {
    const map = new Map<string, NotificationTemplate[]>();
    for (const t of data?.items ?? []) map.set(t.group, [...(map.get(t.group) ?? []), t]);
    return [...map.entries()].sort(([a], [b]) => {
      const ia = GROUP_ORDER.indexOf(a), ib = GROUP_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
  }, [data]);

  const selected = data?.items.find((t) => t.key === selectedKey) ?? null;

  function select(key: string) {
    setSelectedKey(key);
    setResult(null);
  }

  async function sendTest() {
    if (!selected) return;
    setSending(true);
    setResult(null);
    try {
      const r = await api.sendTemplateTest(selected.key);
      setResult({ ok: true, message: `Sent to ${r.recipient || userEmail}` });
    } catch (err) {
      setResult({ ok: false, message: err instanceof ApiError ? err.message : "Could not send the test email." });
    }
    setSending(false);
  }

  if (loading && !data) return <SectionLoading label="Loading the email templates…" />;
  if (error) return <SectionError message={error} onRetry={() => void load()} />;
  if (!data || data.items.length === 0) {
    return <SectionEmpty title="No email templates" body="Templates for the emails PaySilo sends will show up here." />;
  }

  return (
    <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,18rem)_minmax(0,1fr)]">
      {/* Mobile: a select above the preview */}
      <div className="grid gap-1.5 lg:hidden">
        <Label htmlFor="tpl-select">Email</Label>
        <NativeSelect id="tpl-select" value={selectedKey ?? ""} onChange={(e) => select(e.target.value)}>
          {groups.map(([group, items]) => (
            <optgroup key={group} label={group}>
              {items.map((t) => <option key={t.key} value={t.key}>{t.label}{t.sensitive ? " (private link)" : ""}</option>)}
            </optgroup>
          ))}
        </NativeSelect>
      </div>

      <Card className="hidden overflow-hidden p-0 lg:block">
        <nav aria-label="Email templates" className="max-h-[44rem] overflow-y-auto">
          {groups.map(([group, items]) => (
            <div key={group}>
              <p className="border-b-2 border-ink-800 bg-ink-900/60 px-4 py-2 text-xs font-extrabold uppercase tracking-widest text-sage-500">{group}</p>
              <ul className="divide-y divide-ink-800">
                {items.map((t) => (
                  <li key={t.key}>
                    <button type="button" onClick={() => select(t.key)} aria-pressed={selectedKey === t.key}
                            className={cn("flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left text-sm font-bold text-sand-100 transition-colors hover:bg-ink-800/60 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-inset focus-visible:ring-sage-400/50",
                              selectedKey === t.key && "bg-ink-800/80")}>
                      <span className="break-words">{t.label}</span>
                      {t.sensitive && <Badge tone="sand">Private link</Badge>}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
      </Card>

      <Card className="min-w-0 p-5">
        {!selected ? (
          <p className="text-sm font-semibold text-sage-400">Select an email to preview it.</p>
        ) : (
          <div className="grid gap-5">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-display text-xl font-semibold text-sand-100">{selected.label}</h2>
              {selected.sensitive && <Badge tone="sand"><Lock className="size-3" /> Private link</Badge>}
            </div>
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs font-extrabold uppercase tracking-widest text-sage-500">When it is sent</dt>
                <dd className="mt-1 font-semibold text-sage-200">{selected.when}</dd>
              </div>
              <div>
                <dt className="text-xs font-extrabold uppercase tracking-widest text-sage-500">Who receives it</dt>
                <dd className="mt-1 font-semibold text-sage-200">{selected.recipient}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-xs font-extrabold uppercase tracking-widest text-sage-500">Subject</dt>
                <dd className="mt-1 break-words font-bold text-sand-100">{selected.subject}</dd>
              </div>
            </dl>

            <div className="grid gap-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 className="text-xs font-extrabold uppercase tracking-widest text-sage-500">Preview</h3>
                <SubTabs value={preview} onChange={setPreview} items={[["email", "Email"], ["text", "Plain text"]]} />
              </div>
              {preview === "email" ? (
                <div className="overflow-hidden rounded-2xl bg-white">
                  <iframe sandbox="" srcDoc={selected.html} title={`Preview of ${selected.label}`} className="block h-[640px] w-full border-0 bg-white" />
                </div>
              ) : (
                <pre className="max-h-[640px] overflow-auto whitespace-pre-wrap break-words rounded-2xl bg-ink-950/50 p-4 font-mono text-sm leading-relaxed text-sand-100">{selected.text}</pre>
              )}
            </div>

            <div className="grid gap-3">
              <div>
                <Button variant="sage" disabled={!data.can_send_test || sending} onClick={() => void sendTest()}>
                  <Send /> {sending ? "Sending…" : "Send test to me"}
                </Button>
              </div>
              {!data.can_send_test && (
                <p className="text-sm font-semibold text-sand-300">Email delivery is not set up. Add the Brevo SMTP settings to send test emails.</p>
              )}
              {result && (
                <p role={result.ok ? "status" : "alert"} className={cn("text-sm font-bold", result.ok ? "text-mint-400" : "text-coral-300")}>{result.message}</p>
              )}
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
