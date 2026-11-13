"use client";

import { useEffect, useState, type ReactNode } from "react";

import { SiloChatPanel } from "@/components/chat/silo-chat-panel";
import { TourProvider } from "@/components/tour/tour-provider";
import { SideBar } from "@/components/dashboard/side-bar";
import { cn } from "@/lib/utils";

const STORAGE_KEY = "paysilo-sidebar-collapsed";

/**
 * Dashboard shell (v3). A collapsible left sidebar (components/dashboard/
 * side-bar.tsx) carries the grouped navigation on lg+ screens — collapsed
 * it is the slim icon rail, expanded it shows labels and group headings;
 * the choice persists in localStorage. Below lg the sidebar hides and
 * each page's AppBar pill nav takes over. The Silo chat panel is
 * hosted here for every page: a docked right column on xl+ screens (the
 * content reserves its width while open) and a sheet below that.
 */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);

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
    <TourProvider>
      <SideBar collapsed={collapsed} onToggle={toggle} />
      <div
        className={cn(
          "pt-14 transition-[padding] duration-300 ease-out",
          collapsed ? "lg:pl-[76px]" : "lg:pl-60",
          // Open: on xl+ the chat is a docked column, so reserve its width.
          // Closed: leave room under the content for the floating pill.
          chatOpen ? "xl:pr-[420px]" : "pb-20",
        )}
      >
        {children}
      </div>
      <SiloChatPanel open={chatOpen} onOpenChange={setChatOpen} />
    </TourProvider>
  );
}
