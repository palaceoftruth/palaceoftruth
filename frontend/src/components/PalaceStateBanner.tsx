import type { PalaceStateBanner as PalaceStateBannerType } from "../api/types";

const STYLE_BY_KIND: Record<PalaceStateBannerType["kind"], string> = {
  redirected: "border-sky-700/60 bg-sky-950/40 text-sky-200",
  conflict: "border-rose-700/60 bg-rose-950/40 text-rose-200",
  fallback: "border-amber-700/60 bg-amber-950/40 text-amber-200",
  stale: "border-zinc-700/60 bg-zinc-900/80 text-zinc-200",
  indexing: "border-emerald-700/60 bg-emerald-950/40 text-emerald-200",
};

export default function PalaceStateBanner({ banner }: { banner: PalaceStateBannerType }) {
  return (
    <div className={`rounded-xl border px-4 py-3 text-sm ${STYLE_BY_KIND[banner.kind]}`}>
      <p className="font-medium">{banner.message}</p>
      {banner.detail ? <p className="mt-1 text-xs opacity-80">{banner.detail}</p> : null}
    </div>
  );
}
