import type { BadgeProps } from "@/components/ui/badge";
import type { FundingStatus } from "@/lib/api";

/** "2026-11-18" -> "Wed 18 Nov 2026" without any timezone shift. */
export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "Not set";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Date(y, m - 1, d).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function daysPhrase(days: number | null | undefined): string {
  if (days === null || days === undefined) return "";
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days > 1) return `in ${days} days`;
  return days === -1 ? "1 day late" : `${-days} days late`;
}

export const STATUS_LABEL: Record<FundingStatus, string> = {
  FUNDED: "Funded", SHORT: "Short", DUE_SOON: "Due soon", OVERDUE: "Overdue",
  NO_SCHEDULE: "No schedule", NO_LINES: "Nothing to fund", RATE_UNAVAILABLE: "Rate unavailable",
};

export const STATUS_TONE: Record<FundingStatus, BadgeProps["tone"]> = {
  FUNDED: "success", SHORT: "warning", DUE_SOON: "coral", OVERDUE: "danger",
  NO_SCHEDULE: "neutral", NO_LINES: "neutral", RATE_UNAVAILABLE: "neutral",
};

export const FREQUENCY_LABEL = { WEEKLY: "Weekly", BIWEEKLY: "Every 2 weeks", MONTHLY: "Monthly" } as const;

export const KIND_LABEL: Record<string, string> = {
  FUNDING_REMINDER: "Funding reminder", FUNDING_OVERDUE: "Overdue notice", FUNDING_STATEMENT: "Funding statement",
  FUNDS_RECEIVED: "Funds received", APPROVAL_REQUESTED: "Approval needed", HELD_LINES_REMINDER: "Held payments",
  RUN_SETTLED: "Run settled", PAYMENT_FAILED: "Payment problem", INVITE: "Invitation", PAYMENT_NOTICE: "Payment notice", GENERIC: "Message",
};
