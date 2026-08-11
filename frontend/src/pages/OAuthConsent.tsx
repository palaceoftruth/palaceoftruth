import { CheckCircle2, CircleAlert, KeyRound, LoaderCircle, ShieldCheck, XCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, api, hasBrowserSession } from "../api/client";
import type { McpOAuthAuthorizationInteraction } from "../api/types";

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
  const consentBinding = useMemo(() => {
    const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    return {
      session: fragment.get("consent_session")?.trim() ?? "",
      csrfToken: fragment.get("csrf_token")?.trim() ?? "",
    };
  }, []);
  const [interaction, setInteraction] = useState<McpOAuthAuthorizationInteraction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<"approved" | "denied" | null>(null);
  const [needsApiKey, setNeedsApiKey] = useState(false);
  const [apiKey, setApiKey] = useState("");

  useEffect(() => {
    // Remove one-use bindings after React has captured them. Doing this during
    // render loses the binding on Strict Mode's development-only second render.
    if (window.location.hash) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    }
  }, []);

  const loadInteraction = useCallback(async (sessionReady = false) => {
    if (!interactionId) {
      setError("This consent request is missing its interaction identifier.");
      return;
    }
    if (!sessionReady && !hasBrowserSession()) {
      setNeedsApiKey(true);
      setError(null);
      return;
    }
    setNeedsApiKey(false);
    setError(null);
    try {
      if (!consentBinding.session) throw new Error("missing consent binding");
      setInteraction(await api.getMcpAuthorizationInteraction(interactionId, consentBinding.session));
    } catch (reason) {
      if (reason instanceof ApiError && (reason.status === 401 || reason.status === 403)) {
        setNeedsApiKey(true);
        setError("The browser session was not accepted. Enter an active Palace tenant API key.");
        return;
      }
      setError(reason instanceof ApiError ? reason.message : "This consent request is unavailable or has expired.");
    }
  }, [consentBinding.session, interactionId]);

  useEffect(() => {
    document.title = "Approve access · Palace of Truth";
    void loadInteraction();
  }, [loadInteraction]);

  const saveApiKey = async () => {
    const trimmed = apiKey.trim();
    if (!trimmed) {
      setError("Enter the Palace tenant API key.");
      return;
    }
    try {
      await api.createBrowserSession(trimmed, true);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "The Palace tenant API key could not start a browser session.");
      return;
    }
    setApiKey("");
    await loadInteraction(true);
  };

  const decide = async (decision: "approved" | "denied") => {
    const csrfToken = consentBinding.csrfToken;
    if (!csrfToken) {
      setError("This browser session is missing its CSRF binding. Restart the authorization request from the client.");
      return;
    }
    setSubmitting(decision);
    setError(null);
    try {
      const result = await api.decideMcpAuthorizationInteraction(interactionId, decision, csrfToken, consentBinding.session);
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
        {needsApiKey ? <div className="space-y-4 rounded-2xl border border-sky-800/40 bg-sky-950/20 p-5">
          <div>
            <p className="text-sm font-medium text-zinc-100">Authenticate this browser to the Palace tenant</p>
            <p className="mt-2 text-sm leading-6 text-zinc-400">Enter the tenant API key once to review this consent request. Palace exchanges it for a browser session and does not store the key in the browser.</p>
          </div>
          <label className="block text-sm text-zinc-300">Palace tenant API key
            <input className="sb-input mt-2 w-full" type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void saveApiKey(); }} />
          </label>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-xs leading-5 text-emerald-200">Public PKCE client: no client secret is created or stored.</p>
            <button type="button" className="sb-button-primary" onClick={() => void saveApiKey()}><KeyRound className="h-4 w-4" />Save and review request</button>
          </div>
        </div> : null}
        {!interaction && !error && !needsApiKey ? <div className="flex items-center gap-3 py-8 text-sm text-zinc-400"><LoaderCircle className="h-5 w-5 animate-spin" />Loading the tenant-bound request…</div> : null}

        {interaction ? <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sb-panel-muted p-4"><p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">Client</p><p className="mt-2 text-base font-medium text-zinc-100">{interaction.client_name}</p></div>
            <div className="sb-panel-muted p-4"><p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">Palace tenant</p><p className="mt-2 break-all text-base font-medium text-zinc-100">{interaction.tenant_id}</p></div>
          </div>
          <div className="sb-panel-muted p-4"><p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">Protected resource</p><p className="mt-2 break-all text-sm text-zinc-200">{interaction.resource}</p></div>
          {interaction.all_memory_scopes ? <div className="flex gap-3 rounded-2xl border border-amber-700/40 bg-amber-950/30 p-4 text-sm leading-6 text-amber-100"><ShieldCheck className="mt-0.5 h-5 w-5 shrink-0" />This client is requesting read and write access across all memory scopes in this Palace tenant.</div> : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <ScopeList label="Requested scopes" values={interaction.scopes} empty="No scopes were requested." />
            <ScopeList label="Agent restrictions" values={interaction.all_memory_scopes ? [] : interaction.agent_scope_keys} empty={interaction.all_memory_scopes ? "All agent scopes in this tenant." : "No agent-specific access was requested."} />
            <ScopeList label="Workspace restrictions" values={interaction.all_memory_scopes ? [] : interaction.workspace_scope_keys} empty={interaction.all_memory_scopes ? "All workspace and tenant-shared scopes." : "No workspace-specific access was requested."} />
            <div className="sb-panel-muted p-4"><p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">Access lifetime</p><p className="mt-3 text-sm leading-6 text-zinc-300">Access can continue through rotating refresh tokens for up to 30 days, or until you disconnect or a tenant administrator revokes the grant.</p></div>
          </div>
          <div className="flex gap-3 rounded-2xl border border-emerald-800/30 bg-emerald-950/20 p-4 text-sm leading-6 text-emerald-100"><ShieldCheck className="mt-0.5 h-5 w-5 shrink-0" />Your browser session authenticates this decision. The tenant API key is never stored or added to the authorization code or callback.</div>
          <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
            <button type="button" className="sb-button-secondary" disabled={submitting !== null} onClick={() => void decide("denied")}><XCircle className="h-4 w-4" />{submitting === "denied" ? "Denying…" : "Deny"}</button>
            <button type="button" className="sb-button-primary" disabled={submitting !== null} onClick={() => void decide("approved")}><CheckCircle2 className="h-4 w-4" />{submitting === "approved" ? "Approving…" : "Approve access"}</button>
          </div>
        </> : null}
      </section>
    </main>
  );
}
