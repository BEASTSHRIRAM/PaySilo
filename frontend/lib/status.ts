import type { BadgeProps } from "@/components/ui/badge";
import { toneForStatus } from "@/components/ui/badge";

export type StatusTone = BadgeProps["tone"];

export interface StatusInfo {
  label: string;
  tone: StatusTone;
  /** Short explanation for post-payout / partial states; null for ordinary states. */
  detail: string | null;
}

interface RunLike {
  status: string;
  pay_lines?: { status: string }[];
}

const POST_PAYOUT: Record<string, { label: string; detail: string }> = {
  SENT_FEE_FAILED: {
    label: "Paid, fee failed",
    detail: "Contractors were paid, but the PaySilo run fee could not be charged. Follow up before the next run.",
  },
  SENT_UNRECONCILED: {
    label: "Paid, unreconciled",
    detail: "Contractors were paid, but the ledger does not yet match the wallet. Review reconciliation.",
  },
  ATTEST_PENDING: {
    label: "Paid, receipt pending",
    detail: "Contractors were paid. The settlement receipt is still being anchored.",
  },
};

export function paidCounts(run: RunLike): { paid: number; total: number } | null {
  if (!run.pay_lines || run.pay_lines.length === 0) return null;
  return { paid: run.pay_lines.filter((l) => l.status === "PAID").length, total: run.pay_lines.length };
}

/** Display mapping for run statuses, including the newer partial / post-payout states. */
export function runStatusInfo(run: RunLike): StatusInfo {
  const s = run.status;
  if (s === "PARTIALLY_PAID") {
    const c = paidCounts(run);
    return {
      label: "Partially paid",
      tone: "warning",
      detail: c
        ? `${c.paid} of ${c.total} paid. The rest are held until the employer's wallet can cover them.`
        : "Some lines were paid; the rest are held until the employer's wallet can cover them.",
    };
  }
  const post = POST_PAYOUT[s];
  if (post) return { label: post.label, tone: "warning", detail: post.detail };
  return { label: s.replaceAll("_", " "), tone: toneForStatus(s), detail: null };
}

/** Compact label for tables: "Partially paid" gets its count when lines are known. */
export function runStatusLabel(run: RunLike): string {
  const info = runStatusInfo(run);
  if (run.status === "PARTIALLY_PAID") {
    const c = paidCounts(run);
    if (c) return `${info.label} · ${c.paid} of ${c.total}`;
  }
  return info.label;
}

/** Run statuses where contractor money has moved (mirrors the backend MONEY_MOVED set). */
export const MONEY_MOVED_STATUSES = new Set([
  "SETTLED", "PARTIALLY_PAID", "SENT_FEE_FAILED", "SENT_UNRECONCILED", "ATTEST_PENDING",
]);
export const isMoneyMoved = (status: string | null | undefined): boolean => MONEY_MOVED_STATUSES.has(status ?? "");

/** Statuses after which a run no longer changes without a new human action. */
export const TERMINAL_RUN_STATUSES = new Set([
  ...MONEY_MOVED_STATUSES, "FAILED", "REFUSED", "APPROVAL_REJECTED", "HELD_SHORTFALL", "HELD_BY_HUMAN",
]);

export const DECIDER_ROLES = ["owner", "admin", "approver"];
export const EDITOR_ROLES = ["owner", "admin", "editor"];
