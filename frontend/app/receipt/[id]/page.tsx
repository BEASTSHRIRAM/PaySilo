"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import {
  Anchor, Building2, Check, Download, ExternalLink, Landmark, Loader2, Printer,
  ReceiptText, ShieldAlert, ShieldCheck,
} from "lucide-react";

import { apiV3, ApiError, proofDownloads, type ReceiptData } from "@/lib/api";
import { cn, formatMoney } from "@/lib/utils";

function isSimulatorMode(mode: string): boolean {
  return /simulat|mock|fake|sandbox/i.test(mode);
}

function ReceiptView() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const id = params.id;
  const token = search.get("token") ?? "";
  const [data, setData] = useState<ReceiptData | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "notfound" | "error">("loading");

  const load = useCallback(async () => {
    if (!token) { setState("notfound"); return; }
    setState("loading");
    try {
      setData(await apiV3.receipt(id, token));
      setState("ready");
    } catch (e) {
      // A wrong or expired token and a missing receipt look identical on
      // purpose: the page never confirms whether the payment exists.
      if (e instanceof ApiError && [400, 401, 403, 404].includes(e.status)) setState("notfound");
      else setState("error");
    }
  }, [id, token]);

  useEffect(() => { void load(); }, [load]);

  if (state === "loading") {
    return (
      <div className="flex items-center justify-center gap-3 py-24 font-bold text-sage-300" role="status">
        <Loader2 className="size-5 animate-spin" /> Loading your receipt…
      </div>
    );
  }

  if (state === "notfound") {
    return (
      <div className="mx-auto w-full max-w-md rounded-4xl bg-sand-100 p-8 text-center text-ink-900 shadow-2xl">
        <span className="mx-auto grid size-14 place-items-center rounded-full bg-ink-700/10 text-ink-700">
          <ReceiptText className="size-7" />
        </span>
        <h1 className="mt-4 font-display text-2xl font-semibold">Receipt not found</h1>
        <p className="mt-2 leading-relaxed text-ink-700">
          This link may have expired, or the receipt link is incomplete. Please use the exact link sent with your
          payment notification, or ask your employer to resend it.
        </p>
        <p className="mt-4 text-sm font-semibold text-ink-700/70">
          PaySilo never asks for a password or a payment to view a receipt.
        </p>
      </div>
    );
  }

  if (state === "error" || !data) {
    return (
      <div className="mx-auto w-full max-w-md rounded-4xl bg-sand-100 p-8 text-center text-ink-900 shadow-2xl" role="alert">
        <h1 className="font-display text-2xl font-semibold">We could not load this receipt</h1>
        <p className="mt-2 leading-relaxed text-ink-700">The connection to PaySilo failed. Your payment is not affected.</p>
        <button
          type="button" onClick={() => void load()}
          className="mt-5 inline-flex h-11 items-center rounded-2xl bg-coral-500 px-6 text-sm font-extrabold uppercase tracking-wide text-sand-50 shadow-duo [--duo-edge:theme(colors.coral.700)] active:translate-y-[5px] active:shadow-none"
        >
          Try again
        </button>
      </div>
    );
  }

  const simulator = isSimulatorMode(data.verification.mode);
  const settled = ["PAID", "SETTLED", "COMPLETED"].includes(data.status.toUpperCase());

  return (
    <div className="mx-auto w-full max-w-md">
      <article className="overflow-hidden rounded-4xl bg-sand-100 text-ink-900 shadow-2xl">
        {/* Header band */}
        <div className="bg-ink-800 px-7 pb-6 pt-7 text-sand-100">
          <p className="flex items-center gap-2 text-sm font-extrabold uppercase tracking-widest text-sage-300">
            <ReceiptText className="size-4" /> Payment receipt
          </p>
          <p className="mt-4 font-display text-5xl font-semibold tabular-nums leading-none">
            {formatMoney(data.amount, data.currency)}
          </p>
          <p className="mt-3 flex flex-wrap items-center gap-2 text-[15px] font-semibold text-sage-300">
            Paid to <span className="font-extrabold text-sand-100">{data.beneficiary_name}</span>
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-extrabold uppercase tracking-wide",
                settled ? "bg-mint-500 text-ink-950" : "bg-sand-300 text-ink-800",
              )}
            >
              {settled && <Check className="size-3.5" />} {data.status.replaceAll("_", " ")}
            </span>
          </p>
        </div>

        <div className="grid gap-6 px-7 py-6">
          <dl className="grid gap-3 text-[15px]">
            <div className="flex items-center justify-between gap-3">
              <dt className="inline-flex items-center gap-2 font-bold text-ink-700/70"><Building2 className="size-4" /> Employer</dt>
              <dd className="text-right font-extrabold">{data.employer_name}</dd>
            </div>
            <div className="flex items-center justify-between gap-3">
              <dt className="inline-flex items-center gap-2 font-bold text-ink-700/70"><Landmark className="size-4" /> Paid into</dt>
              <dd className="text-right font-extrabold tabular-nums">{data.bank_masked ?? "Bank account on file"}</dd>
            </div>
            <div className="flex items-center justify-between gap-3">
              <dt className="font-bold text-ink-700/70">Method</dt>
              <dd className="font-extrabold">{data.method}</dd>
            </div>
          </dl>

          <div className="border-t-2 border-dashed border-ink-700/20 pt-5">
            <h2 className="mb-4 text-sm font-extrabold uppercase tracking-widest text-ink-700/60">Payment timeline</h2>
            {data.timeline.length === 0 ? (
              <p className="font-semibold text-ink-700/70">No timeline events were reported for this payment.</p>
            ) : (
              <ol className="grid gap-0">
                {data.timeline.map((step, i) => {
                  const last = i === data.timeline.length - 1;
                  return (
                    <li key={i} className="relative flex gap-3 pb-5 last:pb-0">
                      {!last && <span aria-hidden className="absolute left-[11px] top-6 h-[calc(100%-20px)] w-0.5 bg-ink-700/20" />}
                      <span className={cn("z-10 grid size-6 shrink-0 place-items-center rounded-full", last && settled ? "bg-mint-500 text-ink-950" : "bg-ink-700 text-sand-100")}>
                        <Check className="size-3.5" />
                      </span>
                      <div>
                        <p className="font-extrabold leading-snug">{step.label}</p>
                        <p className="text-sm font-semibold tabular-nums text-ink-700/60">{new Date(step.at).toLocaleString()}</p>
                      </div>
                    </li>
                  );
                })}
              </ol>
            )}
          </div>

          {/* Verification panel */}
          <div className="rounded-3xl bg-ink-800 p-5 text-sand-100">
            <h2 className="mb-3 flex items-center gap-2 text-sm font-extrabold uppercase tracking-widest text-sage-300">
              <ShieldCheck className="size-4" /> How this payment is verified
            </h2>
            <ul className="grid gap-2.5 text-[15px] font-semibold">
              <li className="flex items-start gap-2.5">
                {data.verification.tap_verified ? (
                  <ShieldCheck className="mt-0.5 size-5 shrink-0 text-mint-400" />
                ) : (
                  <ShieldAlert className="mt-0.5 size-5 shrink-0 text-sand-300" />
                )}
                <span>
                  {data.verification.tap_verified
                    ? "Approved and signed by the employer — approval signature verified."
                    : "This payment's approval does not carry a verified approval signature."}
                </span>
              </li>
              <li className="flex items-start gap-2.5">
                <Anchor className="mt-0.5 size-5 shrink-0 text-sage-300" />
                <span>
                  {data.verification.metal_tx_hash ? (
                    <>
                      Anchored on Metal.{" "}
                      {data.verification.metal_explorer_url ? (
                        <a href={data.verification.metal_explorer_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-extrabold text-sand-100 underline underline-offset-4">
                          View the anchor transaction <ExternalLink className="size-3.5" />
                        </a>
                      ) : (
                        <span className="break-all">Transaction {data.verification.metal_tx_hash.slice(0, 18)}…</span>
                      )}
                    </>
                  ) : (
                    "No on-chain anchor was reported for this payment."
                  )}
                </span>
              </li>
            </ul>
            <p className="mt-4 inline-flex rounded-full border-2 border-sand-200/25 px-3 py-1 text-xs font-extrabold uppercase tracking-wide text-sand-200">
              {simulator ? "Simulator payment — no live money moved" : `Mode: ${data.verification.mode || "Live"}`}
            </p>
          </div>

          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-bold text-ink-700/60">Keep this page as your proof of payment.</p>
            <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
              <a
                href={proofDownloads.receipt(id, token)} download
                className="inline-flex h-10 items-center gap-2 rounded-xl bg-coral-500 px-4 text-xs font-extrabold uppercase tracking-wide text-sand-50 shadow-duo-sm [--duo-edge:theme(colors.coral.700)] active:translate-y-[3px] active:shadow-none"
              >
                <Download className="size-4" /> Download PDF
              </a>
              <button
                type="button" onClick={() => window.print()}
                className="inline-flex h-10 items-center gap-2 rounded-xl border-2 border-ink-700/25 px-4 text-xs font-extrabold uppercase tracking-wide text-ink-800 hover:bg-ink-700/5"
              >
                <Printer className="size-4" /> Print
              </button>
            </div>
          </div>
        </div>
      </article>
      <p className="mt-5 text-center text-sm font-semibold text-sage-400">
        Issued by PaySilo on behalf of {data.employer_name}.{" "}
        <Link href="/" className="font-bold text-sage-300 underline-offset-4 hover:underline">About PaySilo</Link>
      </p>
    </div>
  );
}

export default function ReceiptPage() {
  return (
    <div className="bg-grid min-h-dvh bg-ink-900 px-4 py-10 sm:py-14">
      <Suspense
        fallback={
          <div className="flex items-center justify-center gap-3 py-24 font-bold text-sage-300" role="status">
            <Loader2 className="size-5 animate-spin" /> Loading your receipt…
          </div>
        }
      >
        <ReceiptView />
      </Suspense>
    </div>
  );
}
