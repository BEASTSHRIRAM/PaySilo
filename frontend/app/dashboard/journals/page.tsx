"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { BookOpen, Building2, Download, Landmark, RefreshCw, Scale } from "lucide-react";

import { SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell, PickerField } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { isNonZero, num } from "@/components/feb/accessors";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Pagination, usePagination } from "@/components/ui/pagination";
import { NativeSelect } from "@/components/ui/select";
import {
  api, apiV3, v3Downloads, ApiError,
  type Employer, type JournalTxn,
} from "@/lib/api";
import { Stagger } from "@/lib/gsap-anim";
import { cn, formatMoney } from "@/lib/utils";

type Book = "employer" | "platform";

/** Per-currency debit/credit totals for one transaction. */
function totalsByCurrency(txn: JournalTxn): { currency: string; debits: number; credits: number }[] {
  const map = new Map<string, { debits: number; credits: number }>();
  for (const line of txn.lines) {
    const cur = map.get(line.currency) ?? { debits: 0, credits: 0 };
    cur.debits += num(line.debit) ?? 0;
    cur.credits += num(line.credit) ?? 0;
    map.set(line.currency, cur);
  }
  return [...map.entries()].map(([currency, v]) => ({ currency, debits: v.debits, credits: v.credits }));
}

/**
 * A transaction is shown as balanced only when the backend says so AND the
 * line totals agree per currency. Either signal failing shows the coral
 * "Out of balance" badge — it is never suppressed.
 */
function isBalanced(txn: JournalTxn): boolean {
  if (!txn.balanced) return false;
  return totalsByCurrency(txn).every((t) => Math.abs(t.debits - t.credits) <= 0.005);
}

function JournalCard({ txn }: { txn: JournalTxn }) {
  const balanced = isBalanced(txn);
  const totals = totalsByCurrency(txn);
  return (
    <Card className={cn("overflow-hidden", !balanced && "border-coral-700/70")}>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b-2 border-ink-700 px-5 py-4">
        <div className="min-w-0">
          <h3 className="font-display text-lg font-semibold text-sand-100">{txn.memo || "Journal entry"}</h3>
          <p className="mt-0.5 text-xs font-semibold text-sage-500">
            {new Date(txn.date).toLocaleString()} · Entry <span className="break-all">{txn.id}</span>
          </p>
        </div>
        {balanced ? (
          <Badge tone="success"><Scale className="size-3.5" /> Balanced</Badge>
        ) : (
          <Badge tone="danger"><Scale className="size-3.5" /> Out of balance</Badge>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[520px] text-left text-sm">
          <thead>
            <tr className="border-b-2 border-ink-700 text-sage-400">
              <th scope="col" className="px-5 py-2.5 font-extrabold">Account</th>
              <th scope="col" className="px-3 py-2.5 text-right font-extrabold">Debit</th>
              <th scope="col" className="px-3 py-2.5 text-right font-extrabold">Credit</th>
              <th scope="col" className="px-3 py-2.5 font-extrabold">Currency</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-700/70">
            {txn.lines.map((line, i) => (
              <tr key={i} className="bg-ink-900/30">
                <td className="px-5 py-2.5 font-bold text-sand-100">{line.account}</td>
                <td className="px-3 py-2.5 text-right font-display font-semibold tabular-nums text-sand-100">
                  {isNonZero(line.debit) ? formatMoney(line.debit, line.currency) : "—"}
                </td>
                <td className="px-3 py-2.5 text-right font-display font-semibold tabular-nums text-sand-100">
                  {isNonZero(line.credit) ? formatMoney(line.credit, line.currency) : "—"}
                </td>
                <td className="px-3 py-2.5 font-semibold text-sage-300">{line.currency}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            {totals.map((t) => (
              <tr key={t.currency} className="border-t-2 border-ink-600 bg-ink-950/40">
                <td className="px-5 py-2.5 font-extrabold text-sand-100">Total ({t.currency})</td>
                <td className="px-3 py-2.5 text-right font-display font-semibold tabular-nums text-sand-100">{formatMoney(t.debits.toFixed(2), t.currency)}</td>
                <td className="px-3 py-2.5 text-right font-display font-semibold tabular-nums text-sand-100">{formatMoney(t.credits.toFixed(2), t.currency)}</td>
                <td className="px-3 py-2.5">
                  {Math.abs(t.debits - t.credits) <= 0.005 ? (
                    <Badge tone="success">Agrees</Badge>
                  ) : (
                    <Badge tone="danger">Differs by {formatMoney(Math.abs(t.debits - t.credits).toFixed(2), t.currency)}</Badge>
                  )}
                </td>
              </tr>
            ))}
          </tfoot>
        </table>
      </div>
      {!balanced && (
        <p className="border-t-2 border-coral-700/50 bg-coral-500/10 px-5 py-3 text-sm font-bold text-coral-300">
          This entry does not balance. A correct double-entry transaction always has equal debits and credits per currency — do not rely on it until the books are corrected.
        </p>
      )}
    </Card>
  );
}

export default function JournalsPage() {
  const { session } = useSession("/dashboard/journals");
  const [book, setBook] = useState<Book>("employer");
  const [employers, setEmployers] = useState<Employer[] | null>(null);
  const [employerId, setEmployerId] = useState("");
  const [txns, setTxns] = useState<JournalTxn[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [dataLoading, setDataLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const pager = usePagination(txns ?? [], 10, { key: "journalPage", resetKey: `${book}|${employerId}` });

  const loadEmployers = useCallback(async () => {
    try {
      const ov = await api.overview();
      setEmployers(ov.employers);
      setEmployerId((cur) => cur || (ov.employers[0]?.id ?? ""));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setError(errMsg(e));
    }
    setLoading(false);
  }, []);

  useEffect(() => { void loadEmployers(); }, [loadEmployers]);

  const loadTxns = useCallback(async () => {
    if (book === "employer" && !employerId) { setTxns(null); return; }
    setDataLoading(true);
    setError(null);
    try {
      setTxns(book === "platform" ? await apiV3.platformJournals() : await apiV3.employerJournals(employerId));
    } catch (e) {
      setTxns(null);
      setError(errMsg(e));
    }
    setDataLoading(false);
  }, [book, employerId]);

  useEffect(() => { void loadTxns(); }, [loadTxns]);

  async function refresh() {
    setRefreshing(true);
    await loadEmployers();
    await loadTxns();
    setRefreshing(false);
  }

  const unbalancedCount = (txns ?? []).filter((t) => !isBalanced(t)).length;
  const grandTotals = useMemo(() => {
    const map = new Map<string, number>();
    for (const t of txns ?? []) for (const tot of totalsByCurrency(t)) {
      map.set(tot.currency, (map.get(tot.currency) ?? 0) + tot.debits);
    }
    return [...map.entries()];
  }, [txns]);

  const exportBase = book === "platform" ? null : employerId;

  return (
    <FebShell
      session={session}
      title="Accounting — journals"
      subtitle="The double-entry books behind every movement. Each transaction must balance per currency: total debits equal total credits, or it is flagged here in coral."
      actions={
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      <Card className="flex flex-wrap items-end gap-4 p-5">
        <div className="grid gap-1.5">
          <span className="text-sm font-extrabold text-sage-400">Book</span>
          <div className="flex gap-2" role="group" aria-label="Choose which book to view">
            <button
              type="button" onClick={() => setBook("employer")} aria-pressed={book === "employer"}
              className={cn("inline-flex h-12 items-center gap-2 rounded-2xl border-2 px-5 text-sm font-extrabold transition-colors",
                book === "employer" ? "border-coral-500 bg-coral-500 text-sand-50" : "border-ink-600 bg-ink-900/80 text-sage-300 hover:bg-ink-700")}
            >
              <Building2 className="size-4" /> Employer book
            </button>
            <button
              type="button" onClick={() => setBook("platform")} aria-pressed={book === "platform"}
              className={cn("inline-flex h-12 items-center gap-2 rounded-2xl border-2 px-5 text-sm font-extrabold transition-colors",
                book === "platform" ? "border-coral-500 bg-coral-500 text-sand-50" : "border-ink-600 bg-ink-900/80 text-sage-300 hover:bg-ink-700")}
            >
              <Landmark className="size-4" /> Platform book
            </button>
          </div>
        </div>

        {book === "employer" && employers && employers.length > 0 && (
          <PickerField label="Employer">
            <NativeSelect value={employerId} onChange={(e) => setEmployerId(e.target.value)} aria-label="Choose an employer">
              {employers.map((e) => (
                <option key={e.id} value={e.id}>{e.name} — {e.country}</option>
              ))}
            </NativeSelect>
          </PickerField>
        )}

        <div className="flex gap-2 pb-0.5">
          {book === "platform" ? (
            <>
              <Button variant="outline" size="sm" asChild><a href={v3Downloads.platformJournals("csv")} download><Download /> CSV</a></Button>
              <Button variant="outline" size="sm" asChild><a href={v3Downloads.platformJournals("json")} download><Download /> JSON</a></Button>
            </>
          ) : exportBase ? (
            <>
              <Button variant="outline" size="sm" asChild><a href={v3Downloads.employerJournals(exportBase, "csv")} download><Download /> CSV</a></Button>
              <Button variant="outline" size="sm" asChild><a href={v3Downloads.employerJournals(exportBase, "json")} download><Download /> JSON</a></Button>
            </>
          ) : null}
        </div>
      </Card>

      {loading && <SectionLoading label="Loading books…" />}
      {error && <SectionError message={error} onRetry={() => void loadTxns()} />}
      {dataLoading && <SectionLoading label="Loading journal entries…" />}

      {book === "employer" && employers && employers.length === 0 && (
        <SectionEmpty title="No employers yet" body="Employer journals appear once an employer exists and payroll has run." />
      )}

      {txns && !dataLoading && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <span className="inline-flex items-center gap-2 rounded-full border-2 border-ink-600 bg-ink-800/70 px-4 py-1.5 text-sm font-extrabold text-sand-100">
              <BookOpen className="size-4 text-sage-400" /> {txns.length} transaction{txns.length === 1 ? "" : "s"}
            </span>
            {grandTotals.map(([cur, total]) => (
              <span key={cur} className="rounded-full border-2 border-ink-600 bg-ink-800/70 px-4 py-1.5 text-sm font-extrabold tabular-nums text-sand-100">
                Total debits {cur} {formatMoney(total.toFixed(2), cur)}
              </span>
            ))}
            {unbalancedCount > 0 && (
              <Badge tone="danger">{unbalancedCount} out of balance</Badge>
            )}
          </div>

          {txns.length === 0 ? (
            <SectionEmpty
              title="No journal entries yet"
              body={book === "platform"
                ? "The platform book is empty. Entries appear when platform charges are posted."
                : "This employer's book is empty. Entries appear when funds move — deposits, FX, payouts, fees, and charges."}
            />
          ) : (
            <div ref={pager.listRef} className="scroll-mt-24">
              <Stagger className="grid gap-5">
                {pager.pageItems.map((txn) => <JournalCard key={txn.id} txn={txn} />)}
              </Stagger>
            </div>
          )}
          <Pagination state={pager} label="Journal entries" />
        </>
      )}
    </FebShell>
  );
}
