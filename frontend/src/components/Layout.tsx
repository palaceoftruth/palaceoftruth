import { Suspense, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { LoaderCircle, Menu } from "lucide-react";
import Sidebar from "./Sidebar";
import { routeLabel } from "./navigation";
import { ToastProvider } from "../context/ToastContext";

const COLLAPSE_KEY = "sb_sidebar_collapsed";

export default function Layout() {
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(COLLAPSE_KEY) === "true";
    } catch {
      return false;
    }
  });

  const handleToggleCollapse = () => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(COLLAPSE_KEY, String(next));
      } catch {
        // ignore storage errors
      }
      return next;
    });
  };

  return (
    <ToastProvider>
      <div className="flex h-screen overflow-hidden bg-transparent text-gray-100">
        <Sidebar
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          collapsed={sidebarCollapsed}
          onToggleCollapse={handleToggleCollapse}
        />

        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <header className="shrink-0 border-b border-zinc-800/80 bg-zinc-950/85 backdrop-blur-xl md:hidden">
            <div className="flex items-center gap-3 px-4 py-3">
              <button
                onClick={() => setSidebarOpen(true)}
                className="-ml-1 rounded-xl p-1.5 text-zinc-400 transition duration-200 ease-out hover:bg-zinc-900 hover:text-zinc-100"
                aria-label="Open menu"
              >
                <Menu className="h-5 w-5" />
              </button>
              <div className="min-w-0">
                <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">Palace of Truth workspace</p>
                <p className="truncate text-sm font-semibold text-zinc-100">{routeLabel(location.pathname)}</p>
              </div>
            </div>
          </header>

          <main className="flex-1 overflow-auto px-4 py-4 md:px-6 md:py-6">
            <div className="mx-auto w-full max-w-[1500px]">
              <Suspense fallback={<RouteLoading routeName={routeLabel(location.pathname)} />}>
                <Outlet />
              </Suspense>
            </div>
          </main>
        </div>
      </div>
    </ToastProvider>
  );
}

function RouteLoading({ routeName }: { routeName: string }) {
  return (
    <div className="sb-panel sb-panel-padding">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="sb-kicker">Opening</p>
          <h1 className="mt-3 text-2xl font-semibold tracking-tight text-zinc-100">{routeName}</h1>
          <p className="mt-2 max-w-xl text-sm leading-6 text-zinc-400">
            Loading the route bundle and preparing the page state.
          </p>
        </div>
        <div className="inline-flex h-12 w-12 items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-950/80">
          <LoaderCircle className="h-5 w-5 animate-spin text-zinc-300" />
        </div>
      </div>

      <div className="mt-8 grid gap-4 lg:grid-cols-[1.4fr,0.9fr]">
        <div className="space-y-4">
          <div className="h-12 animate-pulse rounded-2xl border border-zinc-800 bg-zinc-900/70" />
          <div className="h-56 animate-pulse rounded-[24px] border border-zinc-800 bg-zinc-900/60" />
        </div>
        <div className="space-y-4">
          <div className="h-24 animate-pulse rounded-[24px] border border-zinc-800 bg-zinc-900/60" />
          <div className="h-28 animate-pulse rounded-[24px] border border-zinc-800 bg-zinc-900/60" />
          <div className="h-20 animate-pulse rounded-[24px] border border-zinc-800 bg-zinc-900/60" />
        </div>
      </div>
    </div>
  );
}
