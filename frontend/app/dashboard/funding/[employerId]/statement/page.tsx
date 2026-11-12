"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, Download, Printer } from "lucide-react";

import { AppBar } from "@/components/dashboard/app-bar";
import { SectionError, SectionLoading } from "@/components/dashboard/states";
import { Button } from "@/components/ui/button";
import { api, apiFunding, ApiError, type FundingStatement, type Session } from "@/lib/api";
import { fmtDay, STATUS_LABEL } from "@/lib/funding-ui";
import { formatMoney } from "@/lib/utils";

/*
 * Printable funding statement. On screen it sits in the dark dashboard shell; when printed, the app chrome
 * is hidden and the sheet becomes black text on white (see the print: classes).
 */
export default function StatementPage() {
  const { employerId } = useParams<{ employerId: string }>();
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [st, setSt] = useState<FundingStatement | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [me, statement] = await Promise.all([api.me(), apiFunding.statement(employerId)]);
        setSession(me);
        setSt(statement);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          router.replace(`/login?next=/dashboard/funding/${employerId}/statement`);
          return;
        }
        setError(err instanceof ApiError ? err.message : "Could not load the statement.");
      }
    })();
  }, [employerId, router]);

  const wire = st?.wire?.status === "READY" ? st.wire : null;
  const sum = (label: string, value: string, strong = false) => (
    <div className={`flex justify-between gap-4 py-1 ${strong ? "border-t-2 border-sand-300/40 pt-2 font-extrabold print:border-black" : ""}`}>
      <dt>{label}</dt><dd className="tabular-nums">{formatMoney(value)}</dd>
    </div>
  );

  return (
    <div className="min-h-dvh pb-16 print:min-h-0 print:bg-white print:pb-0">
      <div className="print:hidden">{session ? <AppBar session={session} /> : <div className="h-[76px]" />}</div>
      <main className="mx-auto mt-8 grid max-w-3xl gap-4 px-4 sm:px-6 print:mt-0 print:max-w-none print:px-0">
        <div className="flex flex-wrap items-center justify-between gap-2 print:hidden">
          <Button variant="ghost" size="sm" asChild><Link href="/dashboard/funding"><ArrowLeft /> Back to planner</Link></Button>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" asChild><a href={apiFunding.statementCsvUrl(employerId)} download><Download /> CSV</a></Button>
            <Button size="sm" onClick={() => window.print()}><Printer /> Print</Button>
          </div>
        </div>
        {!st && !error && <SectionLoading label="Preparing statement…" />}
        {error && <SectionError message={error} />}
        {st && (
          <article className="rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-6 text-sand-100 sm:p-8 print:rounded-none print:border-0 print:bg-white print:p-0 print:text-black">
            <header className="flex flex-wrap items-start justify-between gap-3 border-b-2 border-ink-700 pb-4 print:border-black">
              <div>
                <p className="text-xs font-extrabold uppercase tracking-widest text-sage-500 print:text-black">Funding statement</p>
                <h1 className="font-display text-3xl font-semibold">{st.employer}</h1>
                <p className="text-sm font-semibold text-sage-300 print:text-black">Prepared by {st.organization} on {fmtDay(st.generated_on)}</p>
              </div>
              <div className="text-right text-sm font-semibold">
                <p>Pay date: <span className="font-extrabold">{fmtDay(st.pay_date)}</span></p>
                <p>Fund by: <span className="font-extrabold">{fmtDay(st.fund_by)}</span></p>
                <p className="text-sage-400 print:text-black">Status: {STATUS_LABEL[st.status]}</p>
              </div>
            </header>

            <table className="mt-5 w-full border-collapse text-left text-sm">
              <thead>
                <tr className="border-b-2 border-ink-700 text-xs font-extrabold uppercase tracking-wide text-sage-500 print:border-black print:text-black">
                  <th className="py-2 pr-2">Contractor</th><th className="py-2 pr-2 text-right">Amount</th>
                  <th className="py-2 pr-2">Method</th><th className="py-2 pr-2 text-right">Fee</th><th className="py-2 text-right">USD value</th>
                </tr>
              </thead>
              <tbody>
                {st.lines.map((l) => (
                  <tr key={l.contractor_id} className="border-b border-ink-800 print:border-gray-300">
                    <td className="py-2 pr-2 font-bold">{l.name}</td>
                    <td className="py-2 pr-2 text-right tabular-nums">{formatMoney(l.amount, l.currency)}</td>
                    <td className="py-2 pr-2">{l.currency} {l.method}</td>
                    <td className="py-2 pr-2 text-right tabular-nums">{formatMoney(l.fee, l.currency)}</td>
                    <td className="py-2 text-right tabular-nums">{formatMoney(l.amount_usd)}</td>
                  </tr>
                ))}
                {st.needs_amount.map((l) => (
                  <tr key={l.contractor_id} className="border-b border-ink-800 print:border-gray-300">
                    <td className="py-2 pr-2 font-bold">{l.name}</td>
                    <td colSpan={4} className="py-2 font-bold text-coral-300 print:text-black">Needs an amount (not included below)</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <dl className="mt-5 ml-auto max-w-sm text-sm font-semibold">
              {sum("Contractor pay and payout fees", st.payroll_usd)}
              {sum("PaySilo run fee", st.run_fee_usd)}
              {sum("Reserve kept in the wallet", st.reserve_floor_usd)}
              {sum("Needs in wallet", st.required_in_wallet_usd, true)}
              {sum("Already in wallet", st.available_usd)}
              {sum("Gap", st.gap_usd, true)}
              {sum("Recommended transfer", st.recommended_transfer_usd, true)}
            </dl>

            <section className="mt-6 rounded-2xl bg-ink-950/40 p-4 print:break-inside-avoid print:border print:border-black print:bg-white">
              <h2 className="font-display text-lg font-semibold">Send funds to</h2>
              {wire ? (
                <dl className="mt-2 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
                  {[["Account name", wire.account_name], ["Account number", wire.account_number],
                    ...wire.routing.filter((r) => r.type !== "swift").map((r) => [r.label, r.value]),
                    ["SWIFT/BIC", wire.swift_code], ["Bank", wire.bank_name], ["Reference", wire.reference]]
                    .filter(([, v]) => v).map(([k, v]) => (
                      <div key={k}><dt className="text-xs font-bold uppercase tracking-wide text-sage-500 print:text-black">{k}</dt>
                        <dd className="break-words font-mono font-semibold">{v}</dd></div>
                    ))}
                </dl>
              ) : (
                <p className="mt-1 text-sm font-semibold text-sage-300 print:text-black">{st.wire?.message ?? "Wire details are not available yet."}</p>
              )}
            </section>
            <p className="mt-4 text-xs font-semibold text-sage-500 print:text-black">
              Amounts use the same fee and reserve rules as the payroll run and this employer&apos;s own wallet only.
              Transfers can take one to two business days to arrive. Public holidays are not considered.
            </p>
          </article>
        )}
      </main>
    </div>
  );
}
