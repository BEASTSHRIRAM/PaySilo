"use client";

import { useEffect, useState, type ReactNode } from "react";

import { SiloChatPanel } from "@/components/chat/silo-chat-panel";
import { SideBar } from "@/components/dashboard/side-bar";
import { cn } from "@/lib/utils";

const STORAGE_KEY = "paysilo-sidebar-collapsed";

/**
 * Dashboard shell (v3). A collapsible left sidebar (components/dashboard/
 * side-bar.tsx) carries the grouped navigation on lg+ screens — collapsed
 * it is the slim icon rail, expanded it shows labels and group headings;
 * the choice persists in localStorage. Below lg the sidebar hides and
 * each page's AppBar pill nav takes over. The docked Silo chat panel
 * (issue #11) is hosted here for every page.
 */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved !== null) setCollapsed(saved === "1");
    } catch {
      /* storage unavailable — keep the rail default */
    }
  }, []);

  function toggle() {
    setCollapsed((v) => {
      try {
        window.localStorage.setItem(STORAGE_KEY, v ? "0" : "1");
      } catch {
        /* persistence is best-effort */
      }
      return !v;
    });
  }

  return (
    <>
      <SideBar collapsed={collapsed} onToggle={toggle} />
      <div className={cn("transition-[padding] duration-300 ease-out", collapsed ? "lg:pl-[76px]" : "lg:pl-60")}>
        {children}
      </div>
      <SiloChatPanel />
    </>
  );
}
