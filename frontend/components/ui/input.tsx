import * as React from "react";

import { cn } from "@/lib/utils";

const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, type, ...props }, ref) => (
    <input
      type={type}
      ref={ref}
      className={cn(
        "flex h-12 w-full rounded-2xl border-2 border-ink-600 bg-ink-900/80 px-4 text-[15px] font-semibold text-sand-100 placeholder:font-medium placeholder:text-muted-foreground/60 transition-colors focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25 disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-coral-500 aria-[invalid=true]:focus-visible:ring-coral-500/25",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export { Input };
