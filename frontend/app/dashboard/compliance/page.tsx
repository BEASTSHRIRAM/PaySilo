"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Download, RefreshCw } from "lucide-react";

import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { NOT_TAX_ADVICE } from "@/components/contractors/trust-panels";
import { FebShell } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Pagination, usePagination } from "@/components/ui/pagination";
import { apiTrust, ApiError, trustDownloads, type ComplianceOverview } from "@/lib/api";
import { formatMoney } from "@/lib/utils";

const ISSUE_LABEL: Record<string, { label: string; tone: "danger" | "warning" | "neutral" }> = {
  missing: { label: "Form missing", tone: "warning" },
  expired: { label: "Form expired", tone: "danger" },
  expiring_soon: { label: "Expiring soon", tone: "warning" },
  status_unknown: { label: "Tax status not set", tone: "neutral" },
};
const FORM: Record<string, string> = { W9: "W-9", W8BEN: "W-8BEN", W8BENE: "W-8BEN-E", NONE: "No form" };

function ageLabel(hours: number | null): string {
  if (hours === null) return "";
  if (hours < 1) return "just now";
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)} days ago`;
}

function Stat({ label, value, sub, tone }: { label: string; value: number; sub: string; tone: "ok" | "warn" }) {
  return (
    <Card className="p-5">
      <p className="text-sm font-bold text-sage-400">{label}</p>
      <p className={`mt-1 font-display text-3xl font-semibold tabular-nums ${value === 0 || tone === "ok" ? "text-sand-100" : "text-sand-300"}`}>{value}</p>
      <p className="mt-1 text-sm text-sage-400">{value === 0 ? "Nothing to do" : sub}</p>
    </Card>
  );
}

function Row({ children, href }: { children: React.ReactNode; href: string }) {
  return (
    <li>
      <Link href={href} className="grid gap-2 bg-ink-850/80 px-5 py-4 transition-colors hover:bg-ink-800 sm:grid-cols-[1.3fr_1fr_auto] sm:items-center sm:gap-4">
        {children}
      </Link>
    </li>
  );
}
const Who = ({ name, employer }: { name: string; employer: string }) => (
  <span className="min-w-0">
    <span className="block truncate font-bold text-sand-100">{name}</span>
    <span className="block truncate text-xs font-semibold text-sage-500">{employer}</span>
  </span>
);
const List = ({ children }: { children: React.ReactNode }) => (
  <div className="overflow-hidden rounded-4xl border-2 border-ink-700"><ul className="divide-y-2 divide-ink-700">{children}</ul></div>
);

export default function CompliancePage() {
  const { session } = useSession("/dashboard/compliance");
  const [data, setData] = useState<ComplianceOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const bankPager = usePagination(data?.bank_pending ?? [], 5, { key: "bankPage" });
  const taxPager = usePagination(data?.tax_forms ?? [], 10, { key: "taxPage" });
  const watchPager = usePagination(data?.watch_1099 ?? [], 10, { key: "watchPage" });
  const ytdPager = usePagination(data?.ytd ?? [], 10, { key: "ytdPage" });

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await apiTrust.complianceOverview());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setError(errMsg(e));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const link = (employerId: string) => `/dashboard/employers/${employerId}#contractors`;

  return (
    <FebShell
      session={session}
      title="Compliance"
      subtitle="Who still needs a verified bank change, a tax form, or a closer look at year-to-date payments."
      actions={
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" disabled={refreshing} onClick={async () => { setRefreshing(true); await load(); setRefreshing(false); }}>
            <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
          </Button>
          <Button asChild variant="sand" size="sm"><a href={trustDownloads.complianceYtdCsv} download><Download /> Export YTD (CSV)</a></Button>
        </div>
      }
    >
      {!data && !error && <SectionLoading label="Loading compliance overview…" />}
      {error && <SectionError message={error} onRetry={() => void load()} />}

      {data && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Stat label="Bank changes to verify" value={data.counts.bank_pending} sub="Held until a second person verifies" tone="warn" />
            <Stat label="Tax forms needing attention" value={data.counts.tax_forms} sub="Missing, expired or expiring within 60 days" tone="warn" />
            <Stat label="1099 watch" value={data.counts.watch_1099} sub={`US persons at or over USD ${Number(data.threshold_usd).toLocaleString()} in ${data.year}`} tone="warn" />
          </div>

          <section>
            <h2 className="mb-1 font-display text-2xl font-semibold text-sand-100">Bank changes waiting for verification</h2>
            <p className="mb-4 text-sm text-sage-300">Contractors on this list should not be paid until someone other than the person who made the change confirms it with the contractor.</p>
            {data.bank_pending.length === 0 ? (
              <SectionEmpty title="No pending bank changes" body="When a contractor's payout details change, they show up here until verified." lottie={false} />
            ) : (
              <>
              <div ref={bankPager.listRef} className="scroll-mt-24">
              <List>
                {bankPager.pageItems.map((r) => (
                  <Row key={r.contractor_id} href={link(r.employer_id)}>
                    <Who name={r.full_name} employer={r.employer_name} />
                    <span className="text-sm font-semibold text-sage-300">
                      Changed {ageLabel(r.age_hours)}{r.requested_by ? ` by ${r.requested_by}` : ""}
                    </span>
                    <Badge tone="warning">Pending verification</Badge>
                  </Row>
                ))}
              </List>
              </div>
              <Pagination state={bankPager} label="bank_pending" />
              </>
            )}
          </section>

          <section>
            <h2 className="mb-1 font-display text-2xl font-semibold text-sand-100">Tax forms</h2>
            <p className="mb-4 text-sm text-sage-300">W-9 for US persons, W-8BEN for non-US individuals (usually valid about three years).</p>
            {data.tax_forms.length === 0 ? (
              <SectionEmpty title="Every contractor's tax form is in order" body="Missing, expired or soon-to-expire forms will appear here." lottie={false} />
            ) : (
              <>
              <div ref={taxPager.listRef} className="scroll-mt-24">
              <List>
                {taxPager.pageItems.map((r) => {
                  const info = ISSUE_LABEL[r.issue];
                  return (
                    <Row key={r.contractor_id} href={link(r.employer_id)}>
                      <Who name={r.full_name} employer={r.employer_name} />
                      <span className="text-sm font-semibold text-sage-300">
                        {r.tax_status === "US_PERSON" ? "US person" : r.tax_status === "NON_US" ? "Non-US person" : "Tax status not set"}
                        {r.tax_form_type !== "NONE" ? ` · ${FORM[r.tax_form_type]}` : ""}
                        {r.days_to_expiry !== null && r.issue === "expiring_soon" ? ` · ${r.days_to_expiry} days left` : ""}
                        {r.issue === "expired" && r.tax_form_expires_at ? ` · expired ${r.tax_form_expires_at}` : ""}
                      </span>
                      <Badge tone={info.tone}>{info.label}</Badge>
                    </Row>
                  );
                })}
              </List>
              </div>
              <Pagination state={taxPager} label="tax_forms" />
              </>
            )}
          </section>

          <section>
            <h2 className="mb-1 font-display text-2xl font-semibold text-sand-100">1099 watch</h2>
            <p className="mb-4 text-sm text-sage-300">
              US-person contractors paid USD {Number(data.threshold_usd).toLocaleString()} or more so far in {data.year}. A threshold of USD 2,000
              is reported for payments made after 2025-12-31; the figure is configurable. Confirm with your tax advisor.
            </p>
            {data.watch_1099.length === 0 ? (
              <SectionEmpty title="No one is at the threshold yet" body="US-person contractors appear here once their year-to-date payments reach it." lottie={false} />
            ) : (
              <>
              <div ref={watchPager.listRef} className="scroll-mt-24">
              <List>
                {watchPager.pageItems.map((r) => (
                  <Row key={r.contractor_id} href={link(r.employer_id)}>
                    <Who name={r.full_name} employer={r.employer_name} />
                    <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(r.paid_ytd_usd, "USD")} <span className="text-xs font-semibold text-sage-500">· {r.payments} payments</span></span>
                    <Badge tone={r.w9_on_file ? "success" : "danger"}>{r.w9_on_file ? "W-9 on file" : "W-9 needed"}</Badge>
                  </Row>
                ))}
              </List>
              </div>
              <Pagination state={watchPager} label="watch_1099" />
              </>
            )}
          </section>

          <section>
            <h2 className="mb-1 font-display text-2xl font-semibold text-sand-100">Paid so far in {data.year}</h2>
            <p className="mb-4 text-sm text-sage-300">USD equivalents use the rate stored with each payroll run at the time it was priced.</p>
            {data.ytd.length === 0 ? (
              <SectionEmpty title="No contractors yet" body="Add contractors to an employer to see their year-to-date payments." lottie={false} />
            ) : (
              <>
              <div ref={ytdPager.listRef} className="scroll-mt-24">
              <List>
                {ytdPager.pageItems.map((r) => (
                  <Row key={r.contractor_id} href={link(r.employer_id)}>
                    <Who name={r.full_name} employer={r.employer_name} />
                    <span className="text-sm font-semibold text-sage-300">
                      {r.payments === 0 ? "No payments yet" : Object.entries(r.paid_by_currency).map(([c, v]) => formatMoney(v, c)).join(" + ")}
                      {r.unpriced_lines > 0 ? ` · ${r.unpriced_lines} not converted` : ""}
                    </span>
                    <span className="font-display font-semibold tabular-nums text-sand-100">{formatMoney(r.paid_ytd_usd, "USD")}</span>
                  </Row>
                ))}
              </List>
              </div>
              <Pagination state={ytdPager} label="ytd" />
              </>
            )}
          </section>

          <p className="rounded-2xl border-2 border-ink-700 bg-ink-900/50 px-4 py-3 text-sm font-semibold text-sage-300">{NOT_TAX_ADVICE}</p>
        </>
      )}
    </FebShell>
  );
}
