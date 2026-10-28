import { Crew } from "@/components/landing/crew";
import { FinalCta, Footer } from "@/components/landing/cta";
import { Hero } from "@/components/landing/hero";
import { HowItWorks } from "@/components/landing/how-it-works";
import { PartnerMarquee, SiloScene } from "@/components/landing/partners";
import { Problem } from "@/components/landing/problem";
import { Safety } from "@/components/landing/safety";
import { Navbar } from "@/components/site/navbar";

export default function LandingPage() {
  return (
    <>
      <Navbar />
      <main>
        <Hero />
        <PartnerMarquee />
        <Problem />
        <HowItWorks />
        <Crew />
        <Safety />
        <SiloScene />
        <FinalCta />
      </main>
      <Footer />
    </>
  );
}
