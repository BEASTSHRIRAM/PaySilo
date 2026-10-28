import * as React from "react";
import { ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";

/** Native select styled to match Input: accessible and mobile-friendly by default. */
const NativeSelect = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, children, ...props }, ref) => (
    <div className="relative">
      <select
        ref={ref}
        className={cn(
          "flex h-12 w-full appearance-none rounded-2xl border-2 border-ink-600 bg-ink-900/80 pl-4 pr-10 text-[15px] font-semibold text-sand-100 focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25",
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute right-3 top-1/2 size-5 -translate-y-1/2 text-sage-300" />
    </div>
  ),
);
NativeSelect.displayName = "NativeSelect";

export { NativeSelect };
