import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  X,
  Brain,
  ChevronLeft,
  ChevronDown,
  ChevronRight,
} from "lucide-react";
import { CAPTURE_NAV, CORE_NAV, isUtilityRoute, type NavItem, UTILITY_NAV } from "./navigation";

interface SidebarProps {
  open: boolean;
  onClose: () => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
}

function NavSection({
  items,
  collapsed,
  onNavigate,
  quiet = false,
}: {
  items: NavItem[];
  collapsed: boolean;
  onNavigate: () => void;
  quiet?: boolean;
}) {
  return (
    <div className="space-y-1">
      {items.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === "/" || to === "/palace"}
          onClick={onNavigate}
          title={collapsed ? label : undefined}
          className={({ isActive }) =>
            `group flex cursor-pointer items-center gap-3 rounded-2xl transition duration-200 ease-out ${
              collapsed ? "md:justify-center md:px-0 px-3 py-2.5" : "px-3 py-2.5"
            } ${
              isActive
                ? "border border-sky-700/40 bg-sky-950/40 text-sky-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
                : quiet
                  ? "border border-transparent text-zinc-500 hover:border-zinc-800 hover:bg-zinc-900 hover:text-zinc-100"
                  : "border border-transparent text-zinc-400 hover:border-zinc-800 hover:bg-zinc-900 hover:text-zinc-100"
            }`
          }
        >
          <Icon className="h-4 w-4 shrink-0" />
          <span className={`truncate transition-all duration-200 ${collapsed ? "md:hidden" : ""}`}>{label}</span>
        </NavLink>
      ))}
    </div>
  );
}

export default function Sidebar({ open, onClose, collapsed, onToggleCollapse }: SidebarProps) {
  const location = useLocation();
  const [toolsOpen, setToolsOpen] = useState(() => isUtilityRoute(location.pathname));

  useEffect(() => {
    if (isUtilityRoute(location.pathname)) {
      setToolsOpen(true);
    }
  }, [location.pathname]);

  return (
    <>
      {/* Mobile overlay */}
      {open && (
        <div
          className="fixed inset-0 bg-black/60 z-20 md:hidden"
          onClick={onClose}
        />
      )}

      {/* Sidebar */}
      <aside
        className={`
          fixed top-0 left-0 h-full border-r border-zinc-800/80 bg-zinc-950/88 z-30 backdrop-blur-xl
          flex flex-col
          transition-all duration-200 will-change-transform
          ${open ? "translate-x-0 shadow-2xl shadow-black/40" : "-translate-x-[106%]"}
          md:relative md:translate-x-0 md:flex md:shrink-0
          ${collapsed ? "md:w-14 w-56" : "w-56"}
        `}
      >
        {/* Logo */}
        <div
          className={`flex items-center justify-between border-b border-zinc-800 shrink-0 px-4 py-5 ${
            collapsed ? "md:justify-center md:px-0" : ""
          }`}
        >
          <div className={`flex items-center gap-3 ${collapsed ? "md:gap-0" : ""}`}>
            <Brain className="h-5 w-5 shrink-0 text-sky-300" />
            <div className={`min-w-0 transition-all duration-200 ${collapsed ? "md:hidden" : ""}`}>
              <p className="truncate text-sm font-semibold tracking-wide text-zinc-100">Palace of Truth</p>
              <p className="truncate text-[11px] uppercase tracking-[0.22em] text-zinc-500">memory workspace</p>
            </div>
          </div>
          {open ? (
            <button
              onClick={onClose}
              className="rounded-xl p-1.5 text-zinc-400 transition duration-200 ease-out hover:bg-zinc-900 hover:text-zinc-100 md:hidden"
            >
              <X className="h-4 w-4" />
            </button>
          ) : null}
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4">
          <div className="space-y-5">
            {!collapsed ? (
              <div>
                <p className="mb-2 px-3 text-[11px] uppercase tracking-[0.22em] text-zinc-500">Core</p>
                <NavSection items={CORE_NAV} collapsed={collapsed} onNavigate={onClose} />
              </div>
            ) : (
              <NavSection items={CORE_NAV} collapsed={collapsed} onNavigate={onClose} />
            )}

            <div className="border-t border-zinc-800/80 pt-4">
              {!collapsed ? <p className="mb-2 px-3 text-[11px] uppercase tracking-[0.22em] text-zinc-500">Capture</p> : null}
              <NavSection items={CAPTURE_NAV} collapsed={collapsed} onNavigate={onClose} quiet />
            </div>

            <div className="border-t border-zinc-800/80 pt-4">
              {!collapsed ? (
                <button
                  type="button"
                  onClick={() => setToolsOpen((prev) => !prev)}
                  className="flex w-full items-center justify-between rounded-2xl border border-transparent px-3 py-2 text-left transition duration-200 ease-out hover:border-zinc-800 hover:bg-zinc-900"
                >
                  <div>
                    <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">Tools</p>
                    <p className="mt-1 text-xs text-zinc-500">Search, graph, API, and settings live here.</p>
                  </div>
                  <ChevronDown
                    className={`h-4 w-4 shrink-0 text-zinc-500 transition-transform ${toolsOpen ? "rotate-180" : ""}`}
                  />
                </button>
              ) : null}

              {collapsed || toolsOpen ? (
                <div className={collapsed ? "" : "mt-2"}>
                  <NavSection items={UTILITY_NAV} collapsed={collapsed} onNavigate={onClose} quiet />
                </div>
              ) : null}
            </div>
          </div>
        </nav>

        {/* Collapse toggle — desktop only */}
        <div className="hidden shrink-0 justify-end border-t border-zinc-800 p-2 md:flex">
          <button
            onClick={onToggleCollapse}
            className="rounded-xl p-2 text-zinc-500 transition duration-200 ease-out hover:bg-zinc-900 hover:text-zinc-100"
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? (
              <ChevronRight className="h-4 w-4" />
            ) : (
              <ChevronLeft className="h-4 w-4" />
            )}
          </button>
        </div>
      </aside>
    </>
  );
}
