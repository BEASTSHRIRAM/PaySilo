import Link from "next/link";
import { ArrowRight } from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { Logo } from "@/components/brand/logo";
import { Button } from "@/components/ui/button";
import { InView } from "./motion";

export function FinalCta() {
  return (
    <section className="py-16 lg:py-24">
      <div className="container">
        <div className="relative overflow-hidden rounded-[2.75rem] bg-coral-500 px-6 py-14 text-center shadow-[0_8px_0_0_theme(colors.coral.700)] sm:px-12">
          <div className="bg-grain pointer-events-none absolute inset-0 opacity-20 mix-blend-overlay" />
          <div className="relative mx-auto size-44">
            <InView className="absolute inset-0">
              <LottieAnimation name="silo-cheer" className="size-full" label="Silo cheering" />
            </InView>
          </div>
          <h2 className="relative mx-auto mt-2 max-w-2xl font-display text-4xl font-semibold text-sand-50 text-balance sm:text-5xl">
            Run your next payroll with Silo.
          </h2>
          <p className="relative mx-auto mt-4 max-w-xl text-lg font-semibold text-sand-100/90">
            Set up your agency in a minute, or open the demo with three clients already in it.
          </p>
          <div className="relative mt-8 flex flex-wrap justify-center gap-4">
            <Button asChild size="lg" variant="sand">
              <Link href="/signup">
                Start free <ArrowRight />
              </Link>
            </Button>
            <Button asChild size="lg" className="bg-ink-800 text-sand-100 [--duo-edge:theme(colors.ink.950)]">
              <Link href="/login?demo=1">Try the demo</Link>
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
      <div className="container flex flex-col items-center justify-between gap-6 text-sm text-sage-500 md:flex-row">
        <Logo />
        <nav aria-label="Footer" className="flex flex-wrap justify-center gap-x-6 gap-y-2 font-bold text-sage-300">
          <a className="hover:text-sand-100" href="https://github.com/BEASTSHRIRAM/PaySilo" target="_blank" rel="noreferrer">GitHub</a>
          <a className="hover:text-sand-100" href="https://github.com/BEASTSHRIRAM/PaySilo#readme" target="_blank" rel="noreferrer">Docs</a>
          <Link className="hover:text-sand-100" href="/login">Log in</Link>
          <Link className="hover:text-sand-100" href="/signup">Sign up</Link>
        </nav>
        <p className="text-center text-xs md:text-right">
          Team STATIC · Airwallex Agentic Banking Hackathon
          <br />
          The model reads. The code decides. The API moves the money.
        </p>
      </div>
    </footer>
  );
}
