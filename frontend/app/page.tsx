"use client";

import { useEffect, useState } from "react";
import { api, DemoReport, Employer } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge, toneForStatus } from "@/components/ui/badge";

const FALLBACK_EMPLOYERS: Employer[] = [
  { key: "acme", name: "Acme Manufacturing GmbH", contact: "Ana Weber", funding_usd: "20000", balances: {}, status: "NOT_RUN" },
  { key: "borealis", name: "Borealis Labs AB", contact: "Bo Lindqvist", funding_usd: "5000", balances: {}, status: "NOT_RUN" },
  { key: "cobalt", name: "Cobalt Retail LLC", contact: "Cleo Nakamura", funding_usd: "0", balances: {}, status: "NOT_RUN" },
];

function Balances({ balances }: { balances: Record<string, string> }) {
  const entries = Object.entries(balances);
  if (entries.length === 0) return <span className="text-zinc-500">—</span>;
  return (
    <span className="tabular-nums">
      {entries.map(([ccy, amt]) => `${ccy} ${amt}`).join("  ·  ")}
    </span>
  );
}

export default function DashboardPage() {
  const [employers, setEmployers] = useState<Employer[]>(FALLBACK_EMPLOYERS);
  const [report, setReport] = useState<DemoReport | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.employers().then(setEmployers).catch(() => setEmployers(FALLBACK_EMPLOYERS));
  }, []);

  async function runDemo() {
    setRunning(true);
    setError(null);
    try {
      const result = await api.runDemo();
      setReport(result);
      api.employers().then(setEmployers).catch(() => undefined);
    } catch {
      setError("Backend unreachable. Start it with: uvicorn app.main:app --port 8000 (demo mode needs no keys).");
    } finally {
      setRunning(false);
    }
  }

  const heldRuns = report
    ? Object.values(report.employers).filter((e) => e.status.startsWith("HELD"))
    : [];

  return (
    <main className="mx-auto max-w-6xl px-6 py-10">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-line pb-6">
        <div>
          <h1 className="font-display text-3xl font-semibold tracking-tight text-zinc-50">PaySilo</h1>
          <p className="mt-1 max-w-xl text-sm text-zinc-400">
            Multi-Employer Payroll Executor. One platform, many employers, zero mixed money —
            every employer&apos;s funds stay in its own silo.
          </p>
        </div>
        <Button onClick={runDemo} disabled={running}>
          {running ? "Running payroll…" : "Run demo payroll"}
        </Button>
      </header>

      {error && (
        <div className="mt-6 rounded-md border border-red-900 bg-red-950 px-4 py-3 text-sm text-red-200">{error}</div>
      )}

      <section className="mt-8 grid gap-4 md:grid-cols-3">
        {employers.map((employer) => {
          const result = report?.employers[employer.key];
          const status = employer.key === "cobalt" && report ? report.cobalt.payroll_status : (result?.status ?? employer.status);
          const balances =
            employer.key === "cobalt" && report ? report.cobalt.balances : (result?.balances ?? employer.balances);
          return (
            <Card key={employer.key}>
              <CardHeader>
                <div className="flex items-start justify-between gap-2">
                  <CardTitle>{employer.name}</CardTitle>
                  <Badge tone={toneForStatus(status)}>{status}</Badge>
                </div>
                <CardDescription>Contact: {employer.contact}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-zinc-500">Wallet</span>
                  <Balances balances={balances} />
                </div>
                {result?.assessment && (
                  <>
                    <div className="flex justify-between">
                      <span className="text-zinc-500">Required</span>
                      <span className="tabular-nums">EUR {result.assessment.total_required}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-zinc-500">Run fee</span>
                      <span className="tabular-nums">USD {result.fee_charged_usd}</span>
                    </div>
                  </>
                )}
                {employer.key === "cobalt" && report && (
                  <div className="flex justify-between">
                    <span className="text-zinc-500">Mid-run deposit</span>
                    <span className="tabular-nums">USD {report.cobalt.deposit_usd}</span>
                  </div>
                )}
              </CardContent>
            </Card>
          );
        })}
      </section>

      {report && (
        <section className="mt-8 grid gap-4 lg:grid-cols-5">
          <Card className="lg:col-span-3">
            <CardHeader>
              <CardTitle>Run timeline</CardTitle>
              <CardDescription>
                Platform fees collected: USD {report.platform.fees_collected_usd} · Ledger balanced:{" "}
                {report.ledger_balanced ? "yes" : "no"} · Settlement chain valid:{" "}
                {report.settlement_chain_valid ? "yes" : "no"}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ol className="space-y-2 text-sm leading-relaxed text-zinc-300">
                {report.audit.map((line, i) => (
                  <li key={i} className="border-l-2 border-line pl-3">{line}</li>
                ))}
              </ol>
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>Approval inbox</CardTitle>
              <CardDescription>Decisions a human had to make. Nothing here is a blank cheque.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {heldRuns.length === 0 && <p className="text-zinc-500">No escalations.</p>}
              {heldRuns.map((run) => (
                <div key={run.employer} className="rounded-md border border-amber-900 bg-amber-950/40 p-3">
                  <div className="flex items-center justify-between">
                    <span className="font-medium text-zinc-100">{run.employer}</span>
                    <Badge tone="warning">{run.status}</Badge>
                  </div>
                  <p className="mt-2 text-zinc-400">{run.explanation}</p>
                  {run.assessment && (
                    <p className="mt-2 tabular-nums text-zinc-300">
                      Shortfall: USD {run.assessment.shortfall_usd} — held until the employer&apos;s own funds arrive.
                    </p>
                  )}
                </div>
              ))}
              <div className="rounded-md border border-line p-3 text-zinc-400">{report.cross_tenant_guard}</div>
            </CardContent>
          </Card>
        </section>
      )}

      <footer className="mt-10 border-t border-line pt-4 text-xs text-zinc-500">
        Team STATIC · Starter Kit 6 · The model reads, the code decides, the API moves the money.
      </footer>
    </main>
  );
}
