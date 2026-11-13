"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle, ArrowUpRight, Ban, Building2, FileSearch, HandCoins, Inbox,
  Landmark, OctagonAlert, RefreshCw, Scale, ShieldAlert, Siren, TriangleAlert, Wallet,
} from "lucide-react";

import { SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Pagination, usePagination } from "@/components/ui/pagination";
import { apiV3, ApiError, type ExceptionItem } from "@/lib/api";
import { Stagger } from "@/lib/gsap-anim";
import { cn } from "@/lib/utils";

const SEVERITY_ORDER: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };
const SEVERITY_TONE: Record<string, "danger" | "warning" | "sand" | "neutral"> = {
  critical: "danger", high: "danger", medium: "warning", low: "neutral",
};

/** Icon per exception type; unknown types fall back to a generic alert. */
function iconForType(type: string) {
  const t = type.toLowerCase();
  if (t.includes("shortfall") || t.includes("fund")) return Landmark;
  if (t.includes("fee") || t.includes("charge")) return HandCoins;
  if (t.includes("fx") || t.includes("rate")) return Scale;
  if (t.includes("tap") || t.includes("approval") || t.includes("sign")) return ShieldAlert;
  if (t.includes("recon") || t.includes("variance") || t.includes("match")) return FileSearch;
  if (t.includes("ban") || t.includes("block") || t.includes("policy")) return Ban;
  if (t.includes("wallet") || t.includes("balance")) return Wallet;
  if (t.includes("employer") || t.includes("kyc")) return Building2;
  return AlertTriangle;
}

function severityIcon(severity: string) {
  const s = severity.toLowerCase();
  if (s === "critical") return OctagonAlert;
  if (s === "high") return Siren;
  if (s === "medium") return TriangleAlert;
  return AlertTriangle;
}

export default function ExceptionsPage() {
  const { session } = useSession("/dashboard/exceptions");
  const [items, setItems] = useState<ExceptionItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [severityFilter, setSeverityFilter] = useState<string>("all");

  const load = useCallback(async () => {
    setError(null);
    try {
      setItems(await apiV3.exceptions());
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setError(errMsg(e));
    }
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function refresh() {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  }

  const sorted = useMemo(() => {
    const list = [...(items ?? [])];
    list.sort((a, b) => {
      const sa = SEVERITY_ORDER[a.severity.toLowerCase()] ?? 4;
      const sb = SEVERITY_ORDER[b.severity.toLowerCase()] ?? 4;
      if (sa !== sb) return sa - sb;
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });
    return list;
  }, [items]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { critical: 0, high: 0, medium: 0, low: 0 };
    for (const it of items ?? []) {
      const k = it.severity.toLowerCase();
      if (k in c) c[k] += 1;
    }
    return c;
  }, [items]);

  const visible = severityFilter === "all" ? sorted : sorted.filter((i) => i.severity.toLowerCase() === severityFilter);
  const pager = usePagination(visible, 5, { key: "exceptionsPage", resetKey: severityFilter });

  return (
    <FebShell
      session={session}
      title="Exceptions inbox"
      subtitle="Everything that needs a human: policy breaches, shortfalls, and provider problems — worst first, each with an owner and a next action."
      actions={
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading exceptions…" />}
      {error && <SectionError message={error} onRetry={() => void load()} />}

      {items && items.length === 0 && (
        <Card className="flex flex-col items-center gap-2 p-10 text-center">
          <span className="grid size-14 place-items-center rounded-full border-2 border-mint-500/60 bg-mint-500/10 text-mint-400">
            <Inbox className="size-7" />
          </span>
          <p className="font-display text-xl font-semibold text-sand-100">No exceptions — every employer is within policy.</p>
          <p className="max-w-md text-sm font-semibold text-sage-400">
            When a run breaches policy, runs short of funds, or a provider reports a problem, it will appear here with an owner and a next action.
          </p>
        </Card>
      )}

      {items && items.length > 0 && (
        <>
          <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by severity">
            {["all", "critical", "high", "medium", "low"].map((sev) => {
              const active = severityFilter === sev;
              const count = sev === "all" ? items.length : (counts[sev] ?? 0);
              return (
                <button
                  key={sev}
                  type="button"
                  onClick={() => setSeverityFilter(sev)}
                  aria-pressed={active}
                  className={cn(
                    "rounded-full border-2 px-4 py-1.5 text-sm font-extrabold capitalize transition-colors",
                    active ? "border-coral-500 bg-coral-500 text-sand-50" : "border-ink-600 bg-ink-800/70 text-sage-300 hover:bg-ink-700",
                  )}
                >
                  {sev} · {count}
                </button>
              );
            })}
          </div>

          {visible.length === 0 ? (
            <Card className="p-8 text-center">
              <p className="font-bold text-sand-100">No {severityFilter} exceptions right now.</p>
              <p className="mt-1 text-sm font-semibold text-sage-400">Switch the filter to see the rest of the inbox.</p>
            </Card>
          ) : (
            <div ref={pager.listRef} className="scroll-mt-24">
            <Stagger className="grid gap-3">
              {pager.pageItems.map((item) => {
                const TypeIcon = iconForType(item.type);
                const SevIcon = severityIcon(item.severity);
                const sev = item.severity.toLowerCase();
                return (
                  <Card key={item.id} className={cn("p-5", (sev === "critical" || sev === "high") && "border-coral-700/70")}>
                    <div className="flex flex-wrap items-start gap-4">
                      <span
                        className={cn(
                          "grid size-11 shrink-0 place-items-center rounded-2xl border-2",
                          sev === "critical" || sev === "high"
                            ? "border-coral-600/60 bg-coral-500/15 text-coral-300"
                            : sev === "medium"
                              ? "border-sand-400/50 bg-sand-300/10 text-sand-300"
                              : "border-ink-600 bg-ink-800 text-sage-300",
                        )}
                        aria-hidden
                      >
                        <TypeIcon className="size-5" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="font-display text-lg font-semibold text-sand-100">{item.title}</h2>
                          <Badge tone={SEVERITY_TONE[sev] ?? "neutral"}>
                            <SevIcon className="size-3.5" /> {item.severity}
                          </Badge>
                          <Badge tone="neutral">{item.type.replaceAll("_", " ")}</Badge>
                        </div>
                        <p className="mt-1.5 leading-relaxed text-sand-200">{item.detail}</p>
                        <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-3">
                          <div>
                            <dt className="font-bold text-sage-500">Owner</dt>
                            <dd className="font-semibold text-sand-200">{item.owner}</dd>
                          </div>
                          <div>
                            <dt className="font-bold text-sage-500">Next action</dt>
                            <dd className="font-semibold text-sand-200">{item.next_action}</dd>
                          </div>
                          <div>
                            <dt className="font-bold text-sage-500">Evidence</dt>
                            <dd className="font-semibold text-sand-200">{item.evidence_label}</dd>
                          </div>
                        </dl>
                        <p className="mt-2 text-xs font-semibold text-sage-500">
                          Raised {new Date(item.created_at).toLocaleString()}
                        </p>
                      </div>
                      <div className="flex shrink-0 flex-col gap-2">
                        <Button variant="outline" size="sm" asChild>
                          <Link href={`/dashboard/employers/${item.employer_id}`}>
                            {item.employer_name} <ArrowUpRight />
                          </Link>
                        </Button>
                        {item.run_id && (
                          <Button variant="ghost" size="sm" asChild>
                            <Link href={`/dashboard/runs/${item.run_id}`}>Open run <ArrowUpRight /></Link>
                          </Button>
                        )}
                        {item.run_id && (
                          <Button variant="ghost" size="sm" asChild>
                            <Link href="/dashboard/evidence">Evidence <ArrowUpRight /></Link>
                          </Button>
                        )}
                      </div>
                    </div>
                  </Card>
                );
              })}
            </Stagger>
            </div>
          )}
          <Pagination state={pager} label="Exceptions" />
        </>
      )}
    </FebShell>
  );
}
