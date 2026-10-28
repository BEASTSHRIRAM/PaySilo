import type { Metadata, Viewport } from "next";
import { Nunito } from "next/font/google";

import "./globals.css";

const nunito = Nunito({ subsets: ["latin"], weight: ["400", "500", "600", "700", "800", "900"], variable: "--font-nunito" });

export const metadata: Metadata = {
  title: { default: "PaySilo — payroll that never mixes money", template: "%s · PaySilo" },
  description:
    "PaySilo is an agentic payroll executor for agencies that pay contractors for many employers. Every employer's money stays in its own silo — enforced in code.",
};

export const viewport: Viewport = { themeColor: "#11262E" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={nunito.variable}>
      <head>
        {/* Fontshare: Chillax for display headings */}
        <link rel="preconnect" href="https://api.fontshare.com" crossOrigin="" />
        <link href="https://api.fontshare.com/v2/css?f[]=chillax@500,600,700&display=swap" rel="stylesheet" />
      </head>
      <body>{children}</body>
    </html>
  );
}
