"use client";

import { motion, useReducedMotion } from "framer-motion";

import { SiloMark } from "@/components/brand/logo";
import { LottieAnimation, type LottieName } from "@/components/brand/lottie";
import { cn } from "@/lib/utils";
import type { Pose } from "@/lib/tour/steps";

const POSE_LOTTIE: Record<Pose, LottieName> = {
  wave: "silo-wave",
  idle: "silo-idle",
  cheer: "silo-cheer",
  shy: "silo-shy",
};

interface Props {
  pose: Pose;
  /** Square size in px. */
  size?: number;
  /** Degrees to lean toward the highlighted element. */
  tilt?: number;
  /** Changing this value makes Silo hop (a new step). */
  hopKey?: string | number;
  className?: string;
}

/**
 * Silo in a sand tile, like the logo and the Ask Silo button. Poses reuse the existing Lottie art
 * (wave, idle, cheer, shy); framer-motion adds the lean toward the target and a small hop per step.
 * Under prefers-reduced-motion it is the crisp static SVG mark, with no hop or lean.
 */
export function Mascot({ pose, size = 72, tilt = 0, hopKey, className }: Props) {
  const reduced = useReducedMotion();
  return (
    <motion.div
      key={hopKey}
      className={cn("grid shrink-0 place-items-center overflow-hidden rounded-[28%] bg-sand-200 ring-2 ring-sand-400/70 shadow-duo-sm [--duo-edge:theme(colors.sand.400)]", className)}
      style={{ width: size, height: size }}
      initial={reduced ? false : { y: 0, rotate: 0 }}
      animate={reduced ? { rotate: 0 } : { y: [0, -14, 2, 0], rotate: tilt }}
      transition={reduced ? { duration: 0 } : { y: { duration: 0.5, times: [0, 0.4, 0.75, 1], ease: "easeOut" }, rotate: { type: "spring", stiffness: 160, damping: 14 } }}
      aria-hidden="true"
    >
      {reduced ? (
        <SiloMark className="size-[82%]" />
      ) : (
        <LottieAnimation name={POSE_LOTTIE[pose]} className="size-full" />
      )}
    </motion.div>
  );
}
