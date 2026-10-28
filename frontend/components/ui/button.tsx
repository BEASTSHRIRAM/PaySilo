import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * Duolingo-style "3D" buttons: a solid face sitting on a darker edge
 * (box-shadow) that presses down on click. `--duo-edge` sets the edge colour.
 */
const buttonVariants = cva(
  "relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap font-extrabold uppercase tracking-wide transition-[transform,box-shadow,background-color,filter] duration-100 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60 disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-5 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary:
          "bg-coral-500 text-sand-50 [--duo-edge:theme(colors.coral.700)] shadow-duo hover:brightness-110 active:translate-y-[5px] active:shadow-none",
        sand:
          "bg-sand-200 text-ink-800 [--duo-edge:theme(colors.sand.400)] shadow-duo hover:bg-sand-100 active:translate-y-[5px] active:shadow-none",
        sage:
          "bg-sage-500 text-ink-900 [--duo-edge:theme(colors.sage.700)] shadow-duo hover:brightness-105 active:translate-y-[5px] active:shadow-none",
        success:
          "bg-mint-500 text-ink-950 [--duo-edge:theme(colors.mint.700)] shadow-duo hover:brightness-105 active:translate-y-[5px] active:shadow-none",
        outline:
          "border-2 border-ink-600 bg-ink-800/70 text-sand-200 [--duo-edge:theme(colors.ink.600)] shadow-duo hover:bg-ink-700 active:translate-y-[5px] active:shadow-none",
        ghost: "text-sage-300 normal-case tracking-normal hover:bg-ink-700/70 hover:text-sand-100",
        link: "normal-case tracking-normal font-bold text-coral-400 underline-offset-4 hover:underline",
      },
      size: {
        sm: "h-10 rounded-xl px-4 text-xs",
        md: "h-12 rounded-2xl px-6 text-sm",
        lg: "h-14 rounded-2xl px-8 text-base",
        icon: "size-11 rounded-full",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />;
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
