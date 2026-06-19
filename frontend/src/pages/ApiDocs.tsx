import { ApiReferenceReact } from "@scalar/api-reference-react";
import "@scalar/api-reference-react/style.css";
import { ExternalLink, FileCode2, LockKeyhole, Sparkles, Workflow } from "lucide-react";
import { useEffect } from "react";

import PageHeader from "../components/PageHeader";

const REFERENCE_NOTES = [
  {
    title: "Live schema source",
    description: "Every path, request body, and response model is rendered from the backend's current OpenAPI document.",
    icon: Workflow,
  },
  {
    title: "Read-only inspection",
    description: "This explorer is intentionally read-only so the utility route stays focused on contract review instead of live request execution.",
    icon: LockKeyhole,
  },
  {
    title: "Palace-first workflow",
    description: "Stay inside the workspace shell while you inspect the contract instead of jumping into the raw backend docs.",
    icon: Sparkles,
  },
] as const;

export default function ApiDocs() {
  useEffect(() => {
    document.title = "Palace of Truth API";
  }, []);

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Utility"
        title="API Docs"
        description="Inspect the live OpenAPI contract without leaving the Palace shell. Use this reference to verify routes, payload shapes, and auth expectations before wiring new clients."
        meta={
          <>
            <span className="sb-chip sb-chip-active">Live OpenAPI</span>
            <span className="sb-chip sb-chip-inactive">Read-only reference</span>
            <span className="sb-chip sb-chip-inactive">Current tenant auth shape</span>
          </>
        }
        actions={
          <a
            href="/api/openapi.json"
            target="_blank"
            rel="noreferrer"
            className="sb-button-secondary"
          >
            <ExternalLink className="h-4 w-4" />
            Open raw spec
          </a>
        }
      />

      <section className="sb-panel sb-panel-padding space-y-4" aria-labelledby="api-reference-surface-title">
        <div className="flex items-start gap-3">
          <div className="rounded-2xl border border-sky-700/30 bg-sky-950/40 p-3">
            <FileCode2 className="h-5 w-5 text-sky-300" />
          </div>
          <div>
            <h2 id="api-reference-surface-title" className="sb-section-title">
              Reference surface
            </h2>
            <p className="mt-2 max-w-3xl text-sm leading-7 text-zinc-300">
              This route renders the same schema the backend publishes at
              {" "}
              <code className="rounded bg-zinc-900 px-1.5 py-0.5 text-xs text-zinc-200">/api/openapi.json</code>
              {" "}
              inside the Palace utility shell, so operators can inspect endpoints without dropping into the raw backend docs.
            </p>
          </div>
        </div>

        <div className="grid gap-3 xl:grid-cols-3">
          {REFERENCE_NOTES.map(({ title, description, icon: Icon }) => (
            <article key={title} className="sb-panel-muted p-4 md:p-5">
              <div className="flex items-start gap-3">
                <div className="rounded-2xl border border-zinc-700/80 bg-zinc-950/90 p-2.5">
                  <Icon className="h-4 w-4 text-zinc-200" />
                </div>
                <div>
                  <h2 className="text-sm font-medium text-zinc-100">{title}</h2>
                  <p className="mt-2 text-sm leading-6 text-zinc-400">{description}</p>
                </div>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="sb-tool-panel" aria-labelledby="api-contract-title">
        <div className="sb-tool-panel-header">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div>
              <h2 id="api-contract-title" className="sb-section-title">
                Contract explorer
              </h2>
              <p className="mt-2 max-w-3xl text-sm text-zinc-400">
                Endpoint groups, request bodies, and response models come directly from the current backend schema.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <span className="sb-chip sb-chip-inactive">Tenant-aware backend</span>
              <span className="sb-chip sb-chip-inactive">Generated from current app state</span>
            </div>
          </div>
        </div>

        <div className="sb-tool-panel-body api-docs-shell">
          <ApiReferenceReact
            configuration={{
              url: "/api/openapi.json",
              theme: "moon",
              darkMode: true,
              hideDarkModeToggle: true,
              hideTestRequestButton: true,
              telemetry: false,
              defaultHttpClient: { targetKey: "shell", clientKey: "curl" },
              metaData: { title: "Palace of Truth API" },
            }}
          />
        </div>
      </section>
    </div>
  );
}
