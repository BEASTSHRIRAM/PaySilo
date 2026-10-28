"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";

import { cn } from "@/lib/utils";

const Lottie = dynamic(() => import("lottie-react").then((m) => m.Lottie), { ssr: false });

const loaders = {
  "silo-idle": () => import("@/lib/lottie/silo-idle.json"),
  "silo-wave": () => import("@/lib/lottie/silo-wave.json"),
  "silo-cheer": () => import("@/lib/lottie/silo-cheer.json"),
  "silo-shy": () => import("@/lib/lottie/silo-shy.json"),
  silos: () => import("@/lib/lottie/silos.json"),
  shield: () => import("@/lib/lottie/shield.json"),
  burst: () => import("@/lib/lottie/burst.json"),
} as const;

export type LottieName = keyof typeof loaders;

const cache = new Map<LottieName, object>();

function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return reduced;
}

interface Props {
  name: LottieName;
  className?: string;
  loop?: boolean;
  label?: string;
}

/** Plays one of PaySilo's generated Lottie animations. Static (first frame) under reduced motion. */
export function LottieAnimation({ name, className, loop = true, label }: Props) {
  const [data, setData] = useState<object | null>(() => cache.get(name) ?? null);
  const reduced = usePrefersReducedMotion();

  useEffect(() => {
    let alive = true;
    const cached = cache.get(name);
    if (cached) {
      setData(cached);
      return;
    }
    loaders[name]().then((mod) => {
      const json = (mod as { default: object }).default;
      cache.set(name, json);
      if (alive) setData(json);
    });
    return () => {
      alive = false;
    };
  }, [name]);

  return (
    <div className={cn("relative", className)} role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
      {data && <Lottie key={name} src={data} loop={loop && !reduced} autoplay={!reduced} className="size-full" />}
    </div>
  );
}
