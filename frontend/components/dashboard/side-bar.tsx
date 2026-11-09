"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronsLeft, ChevronsRight } from "lucide-react";

import { SiloMark } from "@/components/brand/logo";
import { NAV_GROUPS, isActive } from "@/components/dashboard/app-bar";
import { cn } from "@/lib/utils";

/**
 * Left sidebar for the dashboard (v3), modelled on the reference rail:
 * brand mark on top, one icon tile per destination, the active section
 * filled coral. Collapsible — collapsed is the slim icon rail from the
 * reference; expanded adds labels and group headings. The layout owns
 * the collapsed state so the content padding animates with the rail.
 * Below lg the sidebar hides and the top pill nav takes over.
 */
export function SideBar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const pathname = usePathname() ?? "/dashboard";
  return (
    <aside
      aria-label="Dashboard sidebar"
      className={cn(
        "fixed inset-y-0 left-0 z-40 hidden flex-col border-r border-sand-200/10 bg-ink-950/90 backdrop-blur transition-[width] duration-300 ease-out lg:flex",
        collapsed ? "w-[76px]" : "w-60",
      )}
    >
      <div className={cn("flex items-center px-3 pb-2 pt-4", collapsed ? "justify-center" : "gap-2.5 px-4")}>
        <Link href="/dashboard" aria-label="PaySilo home" className="grid size-11 shrink-0 place-items-center rounded-2xl bg-sand-200 ring-2 ring-sand-400/60">
          <SiloMark className="size-9" />
        </Link>
        {!collapsed && (
          <span className="font-display text-xl font-semibold tracking-tight text-sand-100">
            Pay<span className="text-coral-400">Silo</span>
          </span>
        )}
      </div>

      <nav aria-label="Dashboard sections" className="mt-2 flex-1 overflow-y-auto overflow-x-hidden px-2.5 pb-3">
        {NAV_GROUPS.map((group, gi) => (
          <div key={group.label} className={gi > 0 ? "mt-4" : undefined}>
            {collapsed ? (
              gi > 0 && <div aria-hidden className="mx-3 mb-3 border-t border-sand-200/10" />
            ) : (
              <p className="px-2 pb-1.5 text-[11px] font-extrabold uppercase tracking-widest text-sage-500">{group.label}</p>
            )}
            <ul className="space-y-1">
              {group.links.map(({ href, label, icon: Icon }) => {
                const active = isActive(pathname, href);
                return (
                  <li key={href}>
                    <Link
                      href={href}
                      title={collapsed ? label : undefined}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "group flex items-center rounded-2xl font-bold transition-colors",
                        collapsed ? "flex-col gap-1 px-1 py-2" : "gap-3 px-3 py-2.5",
                        active
                          ? "bg-coral-500 text-sand-50 shadow-duo-sm [--duo-edge:theme(colors.coral.700)]"
                          : "text-sage-300 hover:bg-sand-200/10 hover:text-sand-100",
                      )}
                    >
                      <Icon className="size-5 shrink-0" />
                      {collapsed ? (
                        <span className="text-[10px] font-extrabold leading-none">{label.split(" ")[0]}</span>
                      ) : (
                        <span className="text-sm">{label}</span>
                      )}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      <div className="border-t border-sand-200/10 p-2.5">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className={cn(
            "flex w-full items-center rounded-2xl font-bold text-sage-300 transition-colors hover:bg-sand-200/10 hover:text-sand-100",
            collapsed ? "justify-center px-1 py-2.5" : "gap-3 px-3 py-2.5",
          )}
        >
          {collapsed ? <ChevronsRight className="size-5" /> : <ChevronsLeft className="size-5" />}
          {!collapsed && <span className="text-sm">Collapse</span>}
        </button>
      </div>
    </aside>
  );
}
