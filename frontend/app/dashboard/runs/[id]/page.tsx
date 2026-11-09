"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, RefreshCw, ShieldCheck, ShieldAlert } from "lucide-react";

import { AppBar } from "@/components/dashboard/app-bar";
import { RunPanel } from "@/components/dashboard/run-panel";
import { SectionError, SectionLoading } from "@/components/dashboard/states";
import { Badge, toneForStatus } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, ApiError, type RunDetail, type Session } from "@/lib/api";
import { TimelineReveal } from "@/lib/gsap-anim";

const DECIDERS = ["owner", "admin", "approver"];
const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

export default function RunPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [me, detail] = await Promise.all([api.me(), api.run(id)]);
      setSession(me);
      setRun(detail);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace(`/login?next=/dashboard/runs/${id}`);
        return;
      }
      setLoadError(errMsg(err));
    }
    setLoading(false);
  }, [id, router]);

  useEffect(() => {
    void load();
  }, [load]);

  async function refresh() {
    setRefreshing(true);
    try {
      setRun(await api.run(id));
    } catch (err) {
      setLoadError(errMsg(err));
    } finally {
      setRefreshing(false);
    }
  }

  const canDecide = !!session && DECIDERS.includes(session.role);

  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}

      <main className="mx-auto mt-8 grid max-w-6xl gap-8 px-4 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          {run ? (
            <Link href={`/dashboard/employers/${run.employer_id}`} className="inline-flex items-center gap-2 font-bold text-sage-300 hover:text-sand-100">
              <ArrowLeft className="size-4" /> {run.employer_name}
            </Link>
          ) : (
            <Link href="/dashboard" className="inline-flex items-center gap-2 font-bold text-sage-300 hover:text-sand-100">
              <ArrowLeft className="size-4" /> Dashboard
            </Link>
          )}
          {run && (
            <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
              <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
            </Button>
          )}
        </div>

        {loading && <SectionLoading label="Loading this run…" />}
        {loadError && <SectionError message={loadError} onRetry={() => void load()} />}

        {run && (
          <>
            <section className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">
                  {run.period_label ?? "Payroll run"}
                </h1>
                <p className="mt-1 text-sage-300">
                  {run.employer_name} · started {new Date(run.created_at).toLocaleString()}
                  {run.settled_at ? ` · settled ${new Date(run.settled_at).toLocaleString()}` : ""}
                  {run.source ? ` · via ${run.source}` : ""}
                </p>
              </div>
              <Badge tone={toneForStatus(run.status)}>{run.status.replaceAll("_", " ")}</Badge>
            </section>

            <Card className="p-5 sm:p-6">
              <RunPanel key={run.id} run={run} canDecide={canDecide} onChange={setRun} />
            </Card>

            {run.approvals.length > 0 && (
              <section>
                <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">Approvals and Visa TAP</h2>
                <TimelineReveal className="grid gap-3">
                  {run.approvals.map((ap, i) => (
                    <li key={i}>
                      <Card className="p-5">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <p className="font-bold text-sand-100">
                            {ap.decided_by_email} <span className="font-semibold text-sage-300">chose</span> {ap.decision}
                          </p>
                          <Badge tone={ap.verified ? "success" : "warning"}>
                            {ap.verified ? (
                              <><ShieldCheck className="size-3.5" /> TAP verified</>
                            ) : (
                              <><ShieldAlert className="size-3.5" /> TAP not verified</>
                            )}
                          </Badge>
                        </div>
                        <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                          <div>
                            <dt className="font-bold text-sage-500">Kind</dt>
                            <dd className="font-semibold text-sand-200">{ap.kind}</dd>
                          </div>
                          <div>
                            <dt className="font-bold text-sage-500">Decided at</dt>
                            <dd className="font-semibold text-sand-200">{new Date(ap.created_at).toLocaleString()}</dd>
                          </div>
                          <div>
                            <dt className="font-bold text-sage-500">Signed by (TAP key)</dt>
                            <dd className="break-all font-semibold text-sand-200">{ap.tap_key_id ?? "Unsigned — no TAP key recorded"}</dd>
                          </div>
                          <div>
                            <dt className="font-bold text-sage-500">Signature</dt>
                            <dd className="break-all font-semibold text-sand-200">
                              {ap.tap_signature ? `${ap.tap_signature.slice(0, 32)}…` : "—"}
                            </dd>
                          </div>
                        </dl>
                      </Card>
                    </li>
                  ))}
                </TimelineReveal>
              </section>
            )}

            <section>
              <h2 className="mb-4 font-display text-2xl font-semibold text-sand-100">Settlement receipt</h2>
              {run.receipt ? (
                <Card className="p-5">
                  <dl className="grid gap-3 text-sm sm:grid-cols-2">
                    <div>
                      <dt className="font-bold text-sage-500">Chain</dt>
                      <dd className="font-semibold text-sand-200">
                        {run.receipt.chain} · block #{run.receipt.chain_index}
                      </dd>
                    </div>
                    <div>
                      <dt className="font-bold text-sage-500">Transaction hash</dt>
                      <dd className="break-all font-semibold text-sand-200">{run.receipt.tx_hash ?? "Not available for this receipt"}</dd>
                    </div>
                    <div>
                      <dt className="font-bold text-sage-500">Receipt hash</dt>
                      <dd className="break-all font-semibold text-sand-200">{run.receipt.receipt_hash}</dd>
                    </div>
                    <div>
                      <dt className="font-bold text-sage-500">Payload hash</dt>
                      <dd className="break-all font-semibold text-sand-200">{run.receipt.payload_hash}</dd>
                    </div>
                  </dl>
                  <p className="mt-3 text-xs font-semibold text-sage-500">
                    The API does not return a block-explorer URL for Metal receipts yet, so no explorer link is shown rather than guessing one.
                  </p>
                </Card>
              ) : (
                <Card className="p-5">
                  <p className="font-semibold text-sage-400">
                    No receipt yet. A Metal receipt is anchored here once this run settles.
                  </p>
                </Card>
              )}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
