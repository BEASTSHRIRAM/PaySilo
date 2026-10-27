import { ButtonHTMLAttributes, forwardRef } from "react";

type Variant = "primary" | "ghost";

const variants: Record<Variant, string> = {
  primary:
    "bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-50 disabled:cursor-not-allowed",
  ghost:
    "bg-transparent text-zinc-300 border border-line hover:bg-panel disabled:opacity-50",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
}

/** shadcn-style button, hand-rolled (no CLI) to keep the build light. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className = "", variant = "primary", ...props }, ref) => (
    <button
      ref={ref}
      className={`inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-zinc-500 ${variants[variant]} ${className}`}
      {...props}
    />
  )
);
Button.displayName = "Button";
