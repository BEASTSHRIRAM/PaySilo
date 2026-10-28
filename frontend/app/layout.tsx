import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PaySilo — Multi-Employer Payroll Executor",
  description:
    "Payroll that respects whose money it is. One platform, many employers, zero mixed money.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {/* Fontshare: Satoshi (body) + Clash Display (headings) */}
        <link
          href="https://api.fontshare.com/v2/css?f[]=satoshi@400,500,700&f[]=clash-display@500,600,700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
