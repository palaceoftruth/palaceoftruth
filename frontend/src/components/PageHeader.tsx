import type { ReactNode } from "react";

interface PageHeaderProps {
  eyebrow?: string;
  title: string;
  description: string;
  actions?: ReactNode;
  meta?: ReactNode;
}

export default function PageHeader({
  eyebrow = "Workspace",
  title,
  description,
  actions,
  meta,
}: PageHeaderProps) {
  return (
    <header className="sb-header-grid">
      <div>
        <p className="sb-kicker">{eyebrow}</p>
        <h1 className="sb-page-title">{title}</h1>
        <p className="sb-page-description">{description}</p>
        {meta ? <div className="mt-4 sb-chip-group">{meta}</div> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}
