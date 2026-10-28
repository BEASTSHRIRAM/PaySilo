import Link from "next/link";
import { ArrowRight } from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { Logo } from "@/components/brand/logo";
import { Button } from "@/components/ui/button";

export function FinalCta() {
  return (
    <section className="py-24">
      <div className="container">
        <div className="relative overflow-hidden rounded-[2.75rem] bg-coral-500 px-6 py-14 text-center shadow-[0_8px_0_0_theme(colors.coral.700)] sm:px-12">
          <div className="bg-grain pointer-events-none absolute inset-0 opacity-20 mix-blend-overlay" />
          <div className="relative mx-auto size-44">
            <LottieAnimation name="silo-cheer" className="size-full" label="Silo cheering" />
          </div>
          <h2 className="relative mx-auto mt-2 max-w-2xl font-display text-4xl font-semibold text-sand-50 text-balance sm:text-5xl">
            Run payroll your clients can trust — line by line.
          </h2>
          <p className="relative mx-auto mt-4 max-w-xl text-lg font-semibold text-sand-100/90">
            Sign up your agency in a minute, or take the demo for a spin with Vertex Payroll&apos;s three clients.
          </p>
          <div className="relative mt-8 flex flex-wrap justify-center gap-4">
            <Button asChild size="lg" variant="sand">
              <Link href="/signup">
                Create your organization <ArrowRight />
              </Link>
            </Button>
            <Button asChild size="lg" className="bg-ink-800 text-sand-100 [--duo-edge:theme(colors.ink.950)]">
              <Link href="/login?demo=1">Use demo account</Link>
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}

export function Footer() {
  return (
    <footer className="border-t border-ink-700 py-10">
      <div className="container flex flex-col items-center justify-between gap-4 text-sm text-sage-500 sm:flex-row">
        <Logo />
        <p className="text-center">Team STATIC · Airwallex Agentic Banking Hackathon · Starter Kit 06</p>
        <p className="font-semibold text-sage-400">The model reads. The code decides. The API moves the money.</p>
      </div>
    </footer>
  );
}
