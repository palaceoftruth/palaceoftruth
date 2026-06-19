import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

type StateVariant = "neutral" | "error" | "empty";

const STYLE_BY_VARIANT: Record<StateVariant, string> = {
  neutral: "border-zinc-800 bg-zinc-900/40 text-zinc-100",
  empty: "border-zinc-800 bg-zinc-900/40 text-zinc-100",
  error: "border-rose-800/50 bg-rose-950/20 text-rose-50",
};

const COPY_BY_VARIANT: Record<StateVariant, string> = {
  neutral: "text-zinc-400",
  empty: "text-zinc-400",
  error: "text-rose-200/80",
};

interface StatePanelProps {
  icon: LucideIcon;
  title: string;
  description: string;
  action?: ReactNode;
  variant?: StateVariant;
  compact?: boolean;
}

export default function StatePanel({
  icon: Icon,
  title,
  description,
  action,
  variant = "neutral",
  compact = false,
}: StatePanelProps) {
  return (
    <div
      role={variant === "error" ? "alert" : undefined}
      className={`rounded-[28px] border px-5 py-5 shadow-[0_14px_40px_rgba(2,6,23,0.24)] ${STYLE_BY_VARIANT[variant]} ${
        compact ? "" : "flex min-h-[220px] items-center justify-center"
      }`}
    >
      <div className={`mx-auto max-w-lg ${compact ? "" : "text-center"}`}>
        <div
          className={`inline-flex h-11 w-11 items-center justify-center rounded-2xl border ${
            variant === "error" ? "border-rose-700/50 bg-rose-950/40" : "border-zinc-800 bg-zinc-950/70"
          }`}
        >
          <Icon className={`h-5 w-5 ${variant === "error" ? "text-rose-200" : "text-zinc-300"}`} />
        </div>
        <h2 className="mt-4 text-xl font-semibold tracking-tight">{title}</h2>
        <p className={`mt-3 text-sm leading-7 ${COPY_BY_VARIANT[variant]}`}>{description}</p>
        {action ? <div className={`mt-4 ${compact ? "" : "flex justify-center"}`}>{action}</div> : null}
      </div>
    </div>
  );
}
