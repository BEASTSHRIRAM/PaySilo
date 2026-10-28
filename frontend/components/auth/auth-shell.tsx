"use client";

import { AnimatePresence, motion } from "framer-motion";

import { LottieAnimation, type LottieName } from "@/components/brand/lottie";
import { Logo } from "@/components/brand/logo";

interface Props {
  mood: Extract<LottieName, "silo-wave" | "silo-idle" | "silo-shy" | "silo-cheer">;
  speech: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}

/** Split auth layout: Silo (reacting to the form) on the left, the form on the right. */
export function AuthShell({ mood, speech, children, footer }: Props) {
  return (
    <div className="relative min-h-dvh overflow-hidden lg:grid lg:grid-cols-[1fr_1.05fr]">
      <aside className="relative hidden flex-col justify-between overflow-hidden bg-gradient-to-br from-ink-700 via-ink-800 to-ink-950 p-10 lg:flex">
        <div className="bg-grid pointer-events-none absolute inset-0 opacity-60" />
        <div className="pointer-events-none absolute -bottom-32 -left-24 size-[460px] rounded-full bg-coral-500/25 blur-[110px]" />
        <Logo className="relative" />

        <div className="relative mx-auto flex w-full max-w-md flex-col items-center">
          <AnimatePresence mode="wait">
            <motion.div
              key={speech}
              initial={{ opacity: 0, y: 8, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -6, scale: 0.98 }}
              transition={{ type: "spring", stiffness: 380, damping: 26 }}
              className="relative mb-2 rounded-3xl border-2 border-sand-300 bg-sand-200 px-5 py-4 text-center text-[17px] font-bold text-ink-800 shadow-[0_5px_0_0_theme(colors.sand.400)]"
            >
              {speech}
              <span className="absolute -bottom-[11px] left-1/2 size-5 -translate-x-1/2 rotate-45 border-b-2 border-r-2 border-sand-300 bg-sand-200" />
            </motion.div>
          </AnimatePresence>
          <div className="relative size-[min(26rem,52vh)]">
            <LottieAnimation key={mood} name={mood} className="size-full" label="Silo" />
          </div>
        </div>

        <p className="relative text-sm font-semibold text-sage-400">
          Every employer&apos;s money in its own silo. Enforced in code, signed with Visa TAP.
        </p>
      </aside>

      <main className="relative flex min-h-dvh flex-col px-5 py-8 sm:px-10">
        <div className="flex items-center justify-between lg:hidden">
          <Logo />
          <div className="size-16">
            <LottieAnimation key={mood} name={mood} className="size-full" />
          </div>
        </div>
        <div className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center py-10">{children}</div>
        {footer && <div className="mx-auto w-full max-w-md text-center text-sm text-sage-400">{footer}</div>}
      </main>
    </div>
  );
}
