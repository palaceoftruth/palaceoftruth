import type { PalaceSectionFreshness } from "../api/types";

const STYLE_BY_STATUS: Record<PalaceSectionFreshness["status"], string> = {
  fresh: "bg-emerald-950/50 text-emerald-200 border border-emerald-700/40",
  stale: "bg-zinc-900 text-zinc-300 border border-zinc-700/50",
  indexing: "bg-amber-950/40 text-amber-200 border border-amber-700/40",
  redirected: "bg-sky-950/40 text-sky-200 border border-sky-700/40",
};

export default function PalaceFreshnessPill({
  label,
  freshness,
}: {
  label: string;
  freshness: PalaceSectionFreshness;
}) {
  return (
    <div className={`rounded-full px-2.5 py-1 text-[11px] ${STYLE_BY_STATUS[freshness.status]}`}>
      <span className="font-medium">{label}</span>
      <span className="ml-1 opacity-80">{freshness.status}</span>
    </div>
  );
}
