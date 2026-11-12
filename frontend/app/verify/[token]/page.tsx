"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Building2, Landmark, Loader2, ShieldAlert, ShieldCheck } from "lucide-react";

import { apiProof, type VerifyResult } from "@/lib/api";
import { cn, formatMoney } from "@/lib/utils";

const DOC_LABEL = { proof: "Proof of payment", notice: "Payment status notice", statement: "Payment statement" } as const;

function VerifyView() {
  const { token } = useParams<{ token: string }>();
  const [res, setRes] = useState<VerifyResult | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    setFailed(false);
    try { setRes(await apiProof.verify(token)); } catch { setFailed(true); }
  }, [token]);
  useEffect(() => { void load(); }, [load]);

  if (failed) {
    return (
      <div className="mx-auto w-full max-w-md rounded-4xl bg-sand-100 p-8 text-center text-ink-900 shadow-2xl" role="alert">
        <h1 className="font-display text-2xl font-semibold">We could not check this document</h1>
        <p className="mt-2 leading-relaxed text-ink-700">The connection to PaySilo failed. This says nothing about the document itself.</p>
        <button type="button" onClick={() => void load()}
          className="mt-5 inline-flex h-11 items-center rounded-2xl bg-coral-500 px-6 text-sm font-extrabold uppercase tracking-wide text-sand-50 shadow-duo [--duo-edge:theme(colors.coral.700)] active:translate-y-[5px] active:shadow-none">
          Try again
        </button>
      </div>
    );
  }
  if (!res) {
    return <div className="flex items-center justify-center gap-3 py-24 font-bold text-sage-300" role="status"><Loader2 className="size-5 animate-spin" /> Checking the document…</div>;
  }

  const f = res.facts;
  const lines = f ? (f.lines ?? (f.line ? [f.line] : [])) : [];
  return (
    <div className="mx-auto w-full max-w-md">
      <article className="overflow-hidden rounded-4xl bg-sand-100 text-ink-900 shadow-2xl">
        <div className={cn("px-7 pb-6 pt-7", res.valid ? "bg-ink-800 text-sand-100" : "bg-clay-600 text-sand-100")}>
          <p className="flex items-center gap-2 text-sm font-extrabold uppercase tracking-widest text-sage-300">
            {res.valid ? <ShieldCheck className="size-4" /> : <ShieldAlert className="size-4" />} Document check
          </p>
          <p className="mt-3 font-display text-5xl font-semibold leading-none">{res.valid ? "VALID" : "INVALID"}</p>
          <p className="mt-3 text-[15px] font-semibold text-sage-300">
            {res.valid && f ? `${DOC_LABEL[f.document]} issued by ${f.agency}` : "PaySilo cannot confirm this document."}
          </p>
        </div>
        <div className="grid gap-5 px-7 py-6">
          {!res.valid && <p className="leading-relaxed text-ink-700">{res.reason}</p>}
          {res.valid && f && (
            <>
              <dl className="grid gap-3 text-[15px]">
                <div className="flex items-center justify-between gap-3">
                  <dt className="inline-flex items-center gap-2 font-bold text-ink-700/70"><Building2 className="size-4" /> Employer</dt>
                  <dd className="text-right font-extrabold">{f.employer}</dd>
                </div>
                {f.period && <div className="flex justify-between gap-3"><dt className="font-bold text-ink-700/70">Period</dt><dd className="font-extrabold">{f.period}</dd></div>}
                <div className="flex justify-between gap-3">
                  <dt className="font-bold text-ink-700/70">Approval</dt>
                  <dd className="text-right font-extrabold">{f.tap_verified ? "Visa TAP signature verified" : "No verified signature"}</dd>
                </div>
              </dl>
              <ul className="grid gap-3">
                {lines.map((l, i) => (
                  <li key={i} className="rounded-2xl border-2 border-ink-700/15 p-4">
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className="font-extrabold">{l.contractor}</span>
                      <span className="font-display text-lg font-semibold tabular-nums">{formatMoney(l.amount, l.currency)}</span>
                    </div>
                    <p className="mt-1 flex flex-wrap items-center gap-x-3 text-sm font-semibold text-ink-700/70">
                      <span className="inline-flex items-center gap-1"><Landmark className="size-3.5" /> {l.bank_masked ?? "account on file"}</span>
                      <span>{l.method}</span>
                      <span className={cn("font-extrabold", l.status === "PAID" ? "text-mint-700" : "text-coral-600")}>{l.status === "PAID" ? "PAID" : `NOT PAID (${l.status})`}</span>
                    </p>
                    {l.transfer_id && <p className="mt-1 break-all text-xs font-semibold text-ink-700/60">Airwallex transfer {l.transfer_id}</p>}
                  </li>
                ))}
              </ul>
              {f.receipt_hash && <p className="break-all text-xs font-semibold text-ink-700/60">Ledger receipt {f.receipt_hash}</p>}
              <p className="text-sm font-semibold leading-relaxed text-ink-700/70">
                VALID means this matches PaySilo&apos;s stored record. PAID is what Airwallex reported; the receiving bank&apos;s own credit time is not available to PaySilo.
              </p>
            </>
          )}
        </div>
      </article>
      <p className="mt-5 text-center text-sm font-semibold text-sage-400">
        Checked by PaySilo. <Link href="/" className="font-bold text-sage-300 underline-offset-4 hover:underline">About PaySilo</Link>
      </p>
    </div>
  );
}

export default function VerifyPage() {
  return <div className="bg-grid min-h-dvh bg-ink-900 px-4 py-10 sm:py-14"><VerifyView /></div>;
}
