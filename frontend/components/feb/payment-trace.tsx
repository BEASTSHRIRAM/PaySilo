"use client";

import { useState } from "react";
import { ChevronDown, CircleHelp, Loader2 } from "lucide-react";

import { apiProof, type PaymentTrace } from "@/lib/api";
import { errMsg } from "@/components/feb/use-session";
import { cn } from "@/lib/utils";

const STATE_LABEL: Record<PaymentTrace["state"], string> = {
  SCHEDULED: "Not sent yet", SENT: "On its way", PAID: "Paid", FAILED: "Failed", HELD: "On hold", RETURNED: "Returned",
};

/** "Where is my money?" expandable panel: the deterministic trace for one pay line. */
export function PaymentTracePanel({ runId, lineId }: { runId: string; lineId: string }) {
  const [open, setOpen] = useState(false);
  const [trace, setTrace] = useState<PaymentTrace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next) {
      setLoading(true); setError(null);
      try { setTrace(await apiProof.trace(runId, lineId)); } catch (e) { setError(errMsg(e)); } finally { setLoading(false); }
    }
  }

  return (
    <div className="mt-4 rounded-2xl border-2 border-ink-700 bg-ink-900/40">
      <button
        type="button" onClick={() => void toggle()} aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-sm font-extrabold text-sand-100"
      >
        <span className="flex items-center gap-2"><CircleHelp className="size-4 text-sage-400" /> Where is my money?</span>
        <ChevronDown className={cn("size-4 shrink-0 text-sage-400 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="grid gap-2 border-t-2 border-ink-700 px-4 py-3 text-sm" role="region" aria-label="Payment trace">
          {loading && <p className="flex items-center gap-2 font-semibold text-sage-400"><Loader2 className="size-4 animate-spin" /> Checking…</p>}
          {error && <p role="alert" className="font-semibold text-coral-300">{error}</p>}
          {trace && (
            <>
              <p className="font-extrabold text-sand-100">
                {STATE_LABEL[trace.state]}{trace.failure_code ? ` (Airwallex code ${trace.failure_code})` : ""}
              </p>
              <p className="break-words font-semibold leading-relaxed text-sand-200">{trace.diagnosis}</p>
              <p className="break-words font-semibold leading-relaxed text-sage-300"><span className="font-extrabold text-sage-200">Next step:</span> {trace.next_step}</p>
              {trace.transfer_id && <p className="break-all text-xs font-semibold text-sage-500">Airwallex transfer id: {trace.transfer_id}</p>}
            </>
          )}
        </div>
      )}
    </div>
  );
}
