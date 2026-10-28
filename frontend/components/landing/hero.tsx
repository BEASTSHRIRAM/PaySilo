"use client";

import Link from "next/link";
import { motion } from "framer-motion";
import { ArrowRight, Check, Lock, PauseCircle } from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { Button } from "@/components/ui/button";

const chips = [
  { icon: Check, title: "Acme Manufacturing", body: "Settled · EUR 6,025.70", tone: "bg-mint-500 text-ink-950", pos: "left-0 top-10 sm:-left-6", delay: 0.5 },
  { icon: PauseCircle, title: "Borealis Labs", body: "Held · short $2,642.88", tone: "bg-sand-300 text-ink-900", pos: "right-0 top-1/3 sm:-right-8", delay: 0.8 },
  { icon: Lock, title: "Cobalt deposit $10,000", body: "Stays in Cobalt's silo", tone: "bg-coral-500 text-sand-50", pos: "bottom-6 left-4 sm:-left-2", delay: 1.1 },
];

export function Hero() {
  return (
    <section className="relative overflow-hidden pb-20 pt-32 sm:pt-40">
      <div className="bg-grid pointer-events-none absolute inset-0 [mask-image:radial-gradient(ellipse_at_top,black_30%,transparent_75%)]" />
      <div className="pointer-events-none absolute -right-40 top-10 size-[520px] rounded-full bg-coral-500/20 blur-[120px]" />
      <div className="pointer-events-none absolute -left-40 top-60 size-[420px] rounded-full bg-sage-500/15 blur-[120px]" />

      <div className="container relative grid items-center gap-14 lg:grid-cols-[1.1fr_0.9fr]">
        <div>

          <motion.h1
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.08 }}
            className="font-display text-5xl font-semibold leading-[1.02] tracking-tight text-sand-100 text-balance sm:text-6xl lg:text-7xl"
          >
            Payroll for every client.{" "}
            <span className="text-gradient">Never a mixed penny.</span>
          </motion.h1>

          <motion.p
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.16 }}
            className="mt-6 max-w-xl text-lg leading-relaxed text-sage-300"
          >
            PaySilo is the payroll agent for agencies that pay contractors for many employers. Silo prices fees
            first, pays each employer&apos;s people <em className="font-bold not-italic text-sand-100">only</em> from
            that employer&apos;s own wallet, and stops to ask you when money is short.
          </motion.p>

          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.24 }}
            className="mt-9 flex flex-wrap items-center gap-4"
          >
            <Button asChild size="lg">
              <Link href="/signup">
                Start for free <ArrowRight />
              </Link>
            </Button>
            <Button asChild size="lg" variant="outline">
              <Link href="/login?demo=1">Try the live demo</Link>
            </Button>
          </motion.div>

          <p className="mt-6 text-sm font-semibold text-sage-500">
            No card, no keys — the demo runs on the Airwallex simulator.
          </p>
        </div>

        <div className="relative mx-auto w-full max-w-[460px]">
          <div className="absolute inset-8 rounded-full bg-gradient-to-br from-coral-500/30 via-clay-500/20 to-sage-500/20 blur-2xl" />
          <div className="relative aspect-square rounded-full border border-sand-200/10 bg-ink-800/40">
            <LottieAnimation name="silo-idle" className="absolute inset-4" label="Silo the squirrel, PaySilo's payroll agent" />
          </div>
          {chips.map(({ icon: Icon, title, body, tone, pos, delay }) => (
            <motion.div
              key={title}
              initial={{ opacity: 0, scale: 0.8, y: 10 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              transition={{ delay, type: "spring", stiffness: 260, damping: 18 }}
              className={`absolute ${pos}`}
            >
              <div className="glass flex animate-float items-center gap-3 rounded-2xl border border-sand-200/10 px-3.5 py-2.5 shadow-xl">
                <span className={`grid size-8 place-items-center rounded-xl ${tone}`}>
                  <Icon className="size-4" strokeWidth={3} />
                </span>
                <span className="leading-tight">
                  <span className="block text-sm font-extrabold text-sand-100">{title}</span>
                  <span className="block text-xs font-semibold text-sage-300">{body}</span>
                </span>
              </div>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
}
