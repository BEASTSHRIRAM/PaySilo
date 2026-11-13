import type { Metadata } from "next";
import dynamic from "next/dynamic";

import { FinalCta, Footer } from "@/components/landing/cta";
import { Hero } from "@/components/landing/hero";
import { Navbar } from "@/components/site/navbar";
import "@/components/landing/landing.css";

// Heavy, below the fold sections load as separate chunks.
const Pipeline = dynamic(() => import("@/components/landing/pipeline").then((m) => m.Pipeline));
const Bento = dynamic(() => import("@/components/landing/bento").then((m) => m.Bento));
const Trust = dynamic(() => import("@/components/landing/trust").then((m) => m.Trust));
const Month = dynamic(() => import("@/components/landing/month").then((m) => m.Month));

const TITLE = "PaySilo: payroll for every client, never a mixed penny";
const DESCRIPTION =
  "An agent crew prepares, prices and pays contractor payroll for each client from that client's own wallet. You sign every run.";

export const metadata: Metadata = {
  title: { absolute: TITLE },
  description: DESCRIPTION,
  openGraph: { title: TITLE, description: DESCRIPTION, type: "website", siteName: "PaySilo" },
  twitter: { card: "summary", title: TITLE, description: DESCRIPTION },
};

export default function LandingPage() {
  return (
    <>
      <Navbar />
      <main>
        <Hero />
        <Pipeline />
        <Bento />
        <Trust />
        <Month />
        <FinalCta />
      </main>
      <Footer />
    </>
  );
}
