"use client";

import type { ReactNode } from "react";

import { SiloChatPanel } from "@/components/chat/silo-chat-panel";

/**
 * Dashboard shell (v3). Navigation lives in the AppBar
 * (components/dashboard/app-bar.tsx), which every dashboard page renders
 * with its session — that keeps one grouped nav (Overview / Payroll /
 * Money movement / Trust & audit) on all v3 routes without double bars.
 * This layout hosts the docked Silo chat panel (issue #11) for every page.
 */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <>
      {children}
      <SiloChatPanel />
    </>
  );
}
