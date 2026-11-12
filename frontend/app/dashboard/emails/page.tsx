"use client";

import { useCallback, useEffect, useState } from "react";
import { Mail, RefreshCw, ShieldCheck } from "lucide-react";

import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell } from "@/components/feb/shell";
import { useSession } from "@/components/feb/use-session";
import { fmtDate, spendError } from "@/components/spend/shared";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  apiEmails, ApiError,
  type EmailConfigStatus, type EmailStatus, type OutboxEmail,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Outbox — every message PaySilo has recorded for sending, newest
 * first. Read-only by design: there is no send action here, and on a
 * deployment without a configured mail transport the banner says
 * plainly that rows are recorded and NOT sent.
 */

const STATUS_TONES: Record<EmailStatus, BadgeProps["tone"]> = {
  SENT: "sage",
  QUEUED: "sand",
  FAILED: "coral",
  NOT_CONFIGURED: "neutral",
};

const STATUS_LABELS: Record<EmailStatus, string> = {
  SENT: "Sent",
  QUEUED: "Queued",
  FAILED: "Failed",
  NOT_CONFIGURED: "Not configured",
};

function EmailStatusPill({ status }: { status: EmailStatus }) {
  return (
    <Badge
      tone={STATUS_TONES[status]}
      className={cn(status === "SENT" && "border-sage-500 bg-sage-500 text-ink-900")}
    >
      {STATUS_LABELS[status]}
    </Badge>
  );
}

/** "onboarding_kit_approved" → "Onboarding kit approved" for display; the raw key stays visible in mono underneath. */
function humanizeTemplate(template: string): string {
  const words = template.replace(/[_-]+/g, " ").trim();
  if (!words) return template;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function EmailStatusBanner({ status }: { status: EmailConfigStatus }) {
  if (!status.configured) {
    return (
      <div className="flex gap-3 rounded-3xl border-2 border-sand-400/50 bg-sand-300/10 px-5 py-4" role="status">
        <Mail className="mt-0.5 size-6 shrink-0 text-sand-300" />
        <div className="grid gap-1">
          <p className="font-extrabold text-sand-100">Email is not configured on this deployment</p>
          <p className="text-sm text-sage-300">
            Email is not configured on this deployment. Messages are recorded in the outbox and are NOT sent.
            Configure a mail transport for this deployment to start delivering them.
          </p>
        </div>
        <Badge tone="warning" className="ml-auto h-fit shrink-0">Not configured</Badge>
      </div>
    );
  }
  return (
    <div className="flex gap-3 rounded-3xl border-2 border-sage-600/60 bg-sage-500/10 px-5 py-4" role="status">
      <ShieldCheck className="mt-0.5 size-6 shrink-0 text-sage-300" />
      <div className="grid gap-1">
        <p className="font-extrabold text-sand-100">Email is configured</p>
        <p className="text-sm text-sage-300">
          Messages are sent from{" "}
          <span className="break-all font-mono font-bold text-sand-100">{status.from_address ?? "the configured sender"}</span>.
        </p>
      </div>
      <Badge tone="sage" className="ml-auto h-fit shrink-0 border-sage-500 bg-sage-500 text-ink-900">Configured</Badge>
    </div>
  );
}

export default function EmailsPage() {
  const { session } = useSession("/dashboard/emails");

  const [status, setStatus] = useState<EmailConfigStatus | null>(null);
  const [outbox, setOutbox] = useState<OutboxEmail[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    const [st, ob] = await Promise.allSettled([apiEmails.status(), apiEmails.outbox()]);
    if (st.status === "fulfilled") setStatus(st.value);
    if (ob.status === "fulfilled") {
      // Newest first, regardless of the order the API returned.
      setOutbox([...ob.value].sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0)));
    }
    const failed = [st, ob].find((r) => r.status === "rejected") as PromiseRejectedResult | undefined;
    if (failed) {
      if (!(failed.reason instanceof ApiError && failed.reason.status === 401)) setError(spendError(failed.reason));
    } else {
      setError(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function refresh() {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  }

  return (
    <FebShell
      session={session}
      title="Emails"
      subtitle="The outbox: every message PaySilo has recorded for sending, newest first. This screen is read-only — messages are written here by the rest of the product, and whether they actually leave depends on the deployment's mail configuration above."
      actions={
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading outbox…" />}
      {error && <SectionError message={error} onRetry={() => void load()} />}

      {status && <EmailStatusBanner status={status} />}

      <section className="grid gap-4">
        <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
          <Mail className="size-5 text-coral-400" /> Outbox
        </h2>
        {outbox && outbox.length === 0 && (
          <SectionEmpty
            title="No messages recorded yet"
            body="When PaySilo prepares a message — a kit decision, a trip approval, a receipt — it is recorded here first, whether or not this deployment can send it."
          />
        )}
        {outbox && outbox.length > 0 && (
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[960px] text-left text-sm">
                <thead>
                  <tr className="border-b-2 border-ink-700 text-sage-400">
                    <th scope="col" className="px-5 py-3 font-extrabold">Recipient</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Template</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Subject</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Related</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Status</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Recorded</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Sent</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-ink-700/70">
                  {outbox.map((mail) => (
                    <tr key={mail.id} className="bg-ink-900/30 align-top">
                      <td className="max-w-52 break-all px-5 py-3 font-bold text-sand-100">{mail.to_address}</td>
                      <td className="px-3 py-3 font-semibold text-sand-100">
                        {humanizeTemplate(mail.template)}
                        <span className="block font-mono text-xs font-semibold text-sage-400">{mail.template}</span>
                      </td>
                      <td className="max-w-64 px-3 py-3 font-semibold text-sand-100">{mail.subject}</td>
                      <td className="max-w-64 px-3 py-3 font-semibold text-sage-300">{mail.related_label}</td>
                      <td className="px-3 py-3"><EmailStatusPill status={mail.status} /></td>
                      <td className="px-3 py-3 font-semibold text-sage-300">{fmtDate(mail.created_at)}</td>
                      <td className="px-3 py-3 font-semibold text-sage-300">{mail.sent_at ? fmtDate(mail.sent_at) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </section>
    </FebShell>
  );
}
