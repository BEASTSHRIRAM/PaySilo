import Link from "next/link";

import { cn } from "@/lib/utils";

/** Silo's face as a crisp SVG mark (same art as the Lottie mascot). */
export function SiloMark({ className }: { className?: string }) {
  return (
    <svg viewBox="140 96 232 236" className={cn("size-9", className)} aria-hidden="true">
      <path d="M394 330c34-16 44-70 34-112-6-28-30-72-74-80-30-6-56 18-44 40 12 20 44 8 58 34 10 18 4 52-20 78z" fill="#E64833" />
      <path d="M178 206c-18-20-24-56-12-88 8-14 26-6 34 2 16 18 26 32 28 50z" fill="#874F41" />
      <path d="M186 190c-10-14-14-36-6-56 6-8 14-4 18 2 10 12 16 24 16 38z" fill="#F2917F" />
      <path d="M334 206c18-20 24-56 12-88-8-14-26-6-34 2-16 18-26 32-28 50z" fill="#874F41" />
      <path d="M326 190c10-14 14-36 6-56-6-8-14-4-18 2-10 12-16 24-16 38z" fill="#F2917F" />
      <ellipse cx="256" cy="248" rx="110" ry="100" fill="#874F41" />
      <ellipse cx="256" cy="292" rx="60" ry="40" fill="#FBE9D0" />
      <ellipse cx="210" cy="246" rx="18" ry="22" fill="#0C1D23" />
      <ellipse cx="302" cy="246" rx="18" ry="22" fill="#0C1D23" />
      <circle cx="218" cy="234" r="6.5" fill="#fff" />
      <circle cx="310" cy="234" r="6.5" fill="#fff" />
      <ellipse cx="172" cy="288" rx="19" ry="11" fill="#F2917F" opacity=".85" />
      <ellipse cx="340" cy="288" rx="19" ry="11" fill="#F2917F" opacity=".85" />
      <ellipse cx="256" cy="276" rx="13" ry="8" fill="#0C1D23" />
      <path d="M238 292c2 9 14 10 18 4 4 6 16 5 18-4" fill="none" stroke="#0C1D23" strokeWidth="5" strokeLinecap="round" />
      <rect x="248" y="298" width="16" height="12" rx="3" fill="#fff" />
    </svg>
  );
}

export function Logo({ className, href = "/" }: { className?: string; href?: string }) {
  return (
    <Link href={href} className={cn("group inline-flex items-center gap-2.5", className)} aria-label="PaySilo home">
      <span className="grid size-10 place-items-center rounded-2xl bg-sand-200 ring-2 ring-sand-400/60 transition-transform group-hover:-rotate-6">
        <SiloMark className="size-8" />
      </span>
      <span className="font-display text-xl font-semibold tracking-tight text-sand-100">
        Pay<span className="text-coral-400">Silo</span>
      </span>
    </Link>
  );
}
