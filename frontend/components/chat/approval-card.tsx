"use client";

import { Check, Loader2, PauseCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { ChatApproval } from "@/lib/api";

export interface ApprovalCardState extends ChatApproval {
  state: "pending" | "busy" | "approve" | "hold";
  error?: string;
}

/**
 * Inline approval, unchanged in behaviour from v1: a human clicks, the
 * panel posts the decision to /runs/{id}/decision, and the chat itself
 * never approves anything on its own.
 */
export function ApprovalCard({
  card,
  onDecide,
}: {
  card: ApprovalCardState;
  onDecide: (card: ApprovalCardState, decision: "approve" | "hold") => void;
}) {
  const decided = card.state === "approve" || card.state === "hold";
  return (
    <div className="grid gap-2 rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-3 py-3">
      <p className="text-sm font-bold text-sand-100">Approval needed · {card.employer_name}</p>
      {card.summary && <p className="text-sm leading-snug text-sand-200">{card.summary}</p>}
      {decided ? (
        <p className="flex items-center gap-2 text-sm font-bold text-sage-200" role="status">
          {card.state === "approve" ? (
            <Check className="size-4 text-mint-400" aria-hidden />
          ) : (
            <PauseCircle className="size-4 text-sand-300" aria-hidden />
          )}
          You chose to {card.state === "approve" ? "approve & sign" : "hold"} this run.
        </p>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="success"
            size="sm"
            disabled={card.state === "busy"}
            onClick={() => onDecide(card, "approve")}
          >
            {card.state === "busy" ? <Loader2 className="animate-spin" /> : <Check />} Approve &amp; sign
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={card.state === "busy"}
            onClick={() => onDecide(card, "hold")}
          >
            <PauseCircle /> Hold
          </Button>
        </div>
      )}
      {card.error && (
        <p className="text-sm font-bold text-coral-300" role="alert">
          {card.error}
        </p>
      )}
    </div>
  );
}
