import type { LucideIcon } from "lucide-react";

interface StatsCardProps {
  label: string;
  value: number | string;
  icon: LucideIcon;
  detail?: string;
}

export default function StatsCard({ label, value, icon: Icon, detail }: StatsCardProps) {
  return (
    <div className="sb-stat-card flex items-center gap-4">
      <div className="rounded-2xl border border-sky-700/30 bg-sky-950/40 p-3">
        <Icon className="h-5 w-5 text-sky-300" />
      </div>
      <div>
        <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">{label}</p>
        <p className="mt-2 text-3xl font-semibold tracking-tight text-zinc-100">{value}</p>
        {detail ? <p className="mt-1 text-xs text-zinc-500">{detail}</p> : null}
      </div>
    </div>
  );
}
