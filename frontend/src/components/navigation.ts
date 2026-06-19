import {
  Compass,
  FileCode2,
  LayoutDashboard,
  Library,
  Link2,
  MessageSquare,
  Network,
  type LucideIcon,
  Rss,
  Search,
  Settings,
  Upload,
  Youtube,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  shortLabel?: string;
}

export const CORE_NAV: NavItem[] = [
  { to: "/", label: "Home", shortLabel: "Home", icon: LayoutDashboard },
  { to: "/browse", label: "Library", shortLabel: "Library", icon: Library },
  { to: "/palace", label: "Palace", shortLabel: "Palace", icon: Compass },
  { to: "/chat", label: "Chat", shortLabel: "Chat", icon: MessageSquare },
];

export const CAPTURE_NAV: NavItem[] = [
  { to: "/ingest", label: "Capture", shortLabel: "Capture", icon: Upload },
  { to: "/saved-web", label: "Saved Web", shortLabel: "Saved Web", icon: Link2 },
  { to: "/sources", label: "Sources", shortLabel: "Sources", icon: Youtube },
  { to: "/feeds", label: "Feeds", shortLabel: "Feeds", icon: Rss },
];

export const UTILITY_NAV: NavItem[] = [
  { to: "/search", label: "Search", shortLabel: "Search", icon: Search },
  { to: "/graph", label: "Graph", shortLabel: "Graph", icon: Network },
  { to: "/api-docs", label: "API", shortLabel: "API", icon: FileCode2 },
  { to: "/settings", label: "Settings", shortLabel: "Settings", icon: Settings },
];

export function routeLabel(pathname: string): string {
  if (pathname.startsWith("/palace/control-tower")) {
    return "Control Tower";
  }

  for (const item of [...CORE_NAV, ...CAPTURE_NAV, ...UTILITY_NAV]) {
    if (item.to === "/" && pathname === "/") {
      return item.shortLabel ?? item.label;
    }
    if (item.to !== "/" && pathname.startsWith(item.to)) {
      return item.shortLabel ?? item.label;
    }
  }

  return "Workspace";
}

export function isUtilityRoute(pathname: string): boolean {
  return UTILITY_NAV.some((item) => pathname.startsWith(item.to));
}
