"use client";

import type { ReactNode } from "react";

import { SiloChatPanel } from "@/components/chat/silo-chat-panel";

/** Every dashboard page gets the docked Silo chat panel (issue #11). */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <>
      {children}
      <SiloChatPanel />
    </>
  );
}
