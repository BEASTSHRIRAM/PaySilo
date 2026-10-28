import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border-2 px-2.5 py-0.5 text-xs font-extrabold uppercase tracking-wide",
  {
    variants: {
      tone: {
        neutral: "border-ink-600 bg-ink-700/60 text-sage-300",
        sage: "border-sage-600/60 bg-sage-500/15 text-sage-300",
        coral: "border-coral-600/60 bg-coral-500/15 text-coral-300",
        sand: "border-sand-400/50 bg-sand-200/10 text-sand-200",
        success: "border-mint-700/70 bg-mint-500/15 text-mint-400",
        warning: "border-sand-400/50 bg-sand-300/10 text-sand-300",
        danger: "border-coral-700/70 bg-coral-500/15 text-coral-300",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}

export function toneForStatus(status: string | null | undefined): BadgeProps["tone"] {
  switch (status) {
    case "SETTLED":
    case "ACTIVE":
      return "success";
    case "HELD_SHORTFALL":
    case "HELD_BY_HUMAN":
    case "RESERVED":
      return "warning";
    case "REFUSED":
    case "FAILED":
    case "APPROVAL_REJECTED":
      return "danger";
    case "PRICED":
    case "APPROVED":
    case "SENT":
      return "sage";
    default:
      return "neutral";
  }
}
