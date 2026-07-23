import { CheckCircle2, CircleAlert, KeyRound, LoaderCircle, ShieldCheck, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { ApiError, api, readBrowserApiKey } from "../api/client";
import type { McpOAuthAuthorizationInteraction } from "../api/types";

function csrfTokenFromCookie(): string {
  return document.cookie
    .split("; ")
    .find((entry) => entry.startsWith("palace_oauth_consent_csrf="))
    ?.split("=")[1] ?? "";
}

function ScopeList({ label, values, empty }: { label: string; values: string[]; empty: string }) {
  return (
    <div className="sb-panel-muted p-4">
      <p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">{label}</p>
      {values.length ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {values.map((value) => <span key={value} className="sb-chip sb-chip-inactive">{value}</span>)}
        </div>
      ) : <p className="mt-3 text-sm leading-6 text-zinc-400">{empty}</p>}
    </div>
  );
}

export default function OAuthConsent() {
  const interactionId = useMemo(() => new URLSearchParams(window.location.search).get("interaction_id")?.trim() ?? "", []);
  const [interaction, setInteraction] = useState<McpOAuthAuthorizationInteraction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<"approved" | "denied" | null>(null);

  useEffect(() => {
    document.title = "Approve access · Palace of Truth";
    if (!interactionId) {
      setError("This consent request is missing its interaction identifier.");
      return;
    }
    if (!readBrowserApiKey()) {
      setError("A browser API key is required to review this request. Save the tenant key in Palace Settings, then reopen this consent request.");
      return;
    }
    api.getMcpAuthorizationInteraction(interactionId).then(setInteraction).catch((reason: unknown) => {
      setError(reason instanceof ApiError ? reason.message : "This consent request is unavailable or has expired.");
    });
  }, [interactionId]);

  const decide = async (decision: "approved" | "denied") => {
    const csrfToken = csrfTokenFromCookie();
    if (!csrfToken) {
      setError("This browser session is missing its CSRF binding. Restart the authorization request from the client.");
      return;
    }
    setSubmitting(decision);
    setError(null);
    try {
      const result = await api.decideMcpAuthorizationInteraction(interactionId, decision, csrfToken);
      // Only the validated server response determines the external callback location.
      window.location.assign(result.redirect_uri);
    } catch (reason) {
      setSubmitting(null);
      setError(reason instanceof ApiError ? reason.message : "The consent decision could not be completed.");
    }
  };

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl items-center px-4 py-10 sm:px-6">
      <section className="sb-panel sb-panel-padding w-full space-y-6">
        <div className="flex items-start gap-4">
          <div className="rounded-2xl border border-sky-700/40 bg-sky-950/40 p-3"><KeyRound className="h-6 w-6 text-sky-200" /></div>
          <div>
            <p className="sb-kicker">Palace authorization</p>
            <h1 className="sb-page-title">Review access request</h1>
            <p className="mt-2 max-w-2xl text-sm leading-7 text-zinc-400">Approve only if this client and requested access match what you intend to connect.</p>
          </div>
        </div>

        {error ? <div role="alert" className="flex gap-3 rounded-2xl border border-amber-700/40 bg-amber-950/30 p-4 text-sm leading-6 text-amber-100"><CircleAlert className="mt-0.5 h-5 w-5 shrink-0" />{error}</div> : null}
        {!interaction && !error ? <div className="flex items-center gap-3 py-8 text-sm text-zinc-400"><LoaderCircle className="h-5 w-5 animate-spin" />Loading the tenant-bound request…</div> : null}

        {interaction ? <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sb-panel-muted p-4"><p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">Client</p><p className="mt-2 text-base font-medium text-zinc-100">{interaction.client_name}</p></div>
            <div className="sb-panel-muted p-4"><p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">Palace tenant</p><p className="mt-2 break-all text-base font-medium text-zinc-100">{interaction.tenant_id}</p></div>
          </div>
          <div className="sb-panel-muted p-4"><p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">Protected resource</p><p className="mt-2 break-all text-sm text-zinc-200">{interaction.resource}</p></div>
          <div className="grid gap-3 sm:grid-cols-2">
            <ScopeList label="Requested scopes" values={interaction.scopes} empty="No scopes were requested." />
            <ScopeList label="Agent restrictions" values={interaction.agent_scope_keys} empty="No agent-specific restriction was requested." />
            <ScopeList label="Workspace restrictions" values={interaction.workspace_scope_keys} empty="No workspace-specific restriction was requested." />
            <div className="sb-panel-muted p-4"><p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">Access lifetime</p><p className="mt-3 text-sm leading-6 text-zinc-300">Access can continue through rotating refresh tokens for up to 30 days, or until you disconnect or a tenant administrator revokes the grant.</p></div>
          </div>
          <div className="flex gap-3 rounded-2xl border border-emerald-800/30 bg-emerald-950/20 p-4 text-sm leading-6 text-emerald-100"><ShieldCheck className="mt-0.5 h-5 w-5 shrink-0" />Your browser API key authenticates this decision but is never added to the authorization code or callback.</div>
          <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
            <button type="button" className="sb-button-secondary" disabled={submitting !== null} onClick={() => void decide("denied")}><XCircle className="h-4 w-4" />{submitting === "denied" ? "Denying…" : "Deny"}</button>
            <button type="button" className="sb-button-primary" disabled={submitting !== null} onClick={() => void decide("approved")}><CheckCircle2 className="h-4 w-4" />{submitting === "approved" ? "Approving…" : "Approve access"}</button>
          </div>
        </> : null}
      </section>
    </main>
  );
}
