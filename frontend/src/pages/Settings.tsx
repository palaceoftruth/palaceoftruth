import { useEffect, useState } from "react";
import { CheckCircle2, ClipboardCopy, Key, Loader2, LogOut, ShieldCheck, SlidersHorizontal, X } from "lucide-react";

import { api, clearLegacyBrowserApiKey } from "../api/client";
import type { BrowserSession } from "../api/client";
import type { BrowserExtensionPairingKey } from "../api/types";
import PageHeader from "../components/PageHeader";

const STORAGE_KEY_PER_PAGE = "sb:per_page";
const STORAGE_KEY_SORT = "sb:default_sort";

function readLocalStorage(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeLocalStorage(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export default function Settings() {
  const [perPage, setPerPage] = useState(() => readLocalStorage(STORAGE_KEY_PER_PAGE, "20"));
  const [defaultSort, setDefaultSort] = useState(() => readLocalStorage(STORAGE_KEY_SORT, "created_at|desc"));
  const [browserApiKey, setBrowserApiKey] = useState("");
  const [elevated, setElevated] = useState(false);
  const [session, setSession] = useState<BrowserSession | null>(null);
  const [sessionLoading, setSessionLoading] = useState(true);
  const [saved, setSaved] = useState(false);
  const [apiKeySaved, setApiKeySaved] = useState(false);
  const [apiKeyError, setApiKeyError] = useState<string | null>(null);
  const [storageError, setStorageError] = useState(false);
  const [pairing, setPairing] = useState<BrowserExtensionPairingKey | null>(null);
  const [pairingLoading, setPairingLoading] = useState(false);
  const [pairingError, setPairingError] = useState<string | null>(null);
  const [pairingCopied, setPairingCopied] = useState(false);

  // Pairing keys need the `admin` scope, which a session only holds when the
  // operator asked for it at sign-in.
  const sessionIsElevated = Boolean(session?.scopes.includes("admin"));

  useEffect(() => {
    let cancelled = false;

    const restore = async () => {
      try {
        const current = await api.getBrowserSession();
        if (!cancelled) setSession(current);
      } catch {
        if (!cancelled) setSession(null);
      }
    };

    void restore().finally(() => {
      if (!cancelled) setSessionLoading(false);
    });

    return () => {
      cancelled = true;
    };
  }, []);

  const handleSignIn = async () => {
    const trimmed = browserApiKey.trim();
    if (!trimmed) {
      setApiKeyError("Enter an API key before signing in.");
      setApiKeySaved(false);
      return;
    }
    setApiKeyError(null);
    try {
      const created = await api.createBrowserSession(trimmed, elevated);
      setSession(created);
      setApiKeySaved(true);
      // The key itself is never kept: the session cookie replaces it.
      setBrowserApiKey("");
      clearLegacyBrowserApiKey();
      setTimeout(() => setApiKeySaved(false), 2000);
    } catch (error) {
      setSession(null);
      setApiKeySaved(false);
      setApiKeyError(error instanceof Error ? error.message : "Unable to sign in with that API key.");
    }
  };

  const handleSignOut = async () => {
    try {
      await api.deleteBrowserSession();
      setSession(null);
      setBrowserApiKey("");
      setApiKeySaved(false);
      setApiKeyError(null);
      setStorageError(false);
      clearLegacyBrowserApiKey();
    } catch (error) {
      setApiKeyError(error instanceof Error ? error.message : "Unable to sign out.");
    }
  };

  const handleSave = () => {
    const savedPerPage = writeLocalStorage(STORAGE_KEY_PER_PAGE, perPage);
    const savedDefaultSort = writeLocalStorage(STORAGE_KEY_SORT, defaultSort);
    const nextStorageError = !savedPerPage || !savedDefaultSort;

    setStorageError(nextStorageError);
    setSaved(!nextStorageError);

    if (!nextStorageError) {
      setTimeout(() => setSaved(false), 2000);
    }
  };

  const handleGeneratePairingKey = async () => {
    setPairingLoading(true);
    setPairingError(null);
    setPairingCopied(false);
    try {
      setPairing(await api.issueBrowserExtensionPairingKey());
    } catch (error) {
      setPairing(null);
      setPairingError(error instanceof Error ? error.message : "Unable to generate a pairing key.");
    } finally {
      setPairingLoading(false);
    }
  };

  const handleCopyPairingKey = async () => {
    if (!pairing) return;
    try {
      await navigator.clipboard.writeText(pairing.pairing_key);
      setPairingCopied(true);
    } catch {
      setPairingError("Clipboard access was blocked. Select and copy the key manually.");
    }
  };

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Configuration"
        title="Settings"
        description="Manage local browsing defaults and confirm how the frontend is talking to the backend in this environment."
        meta={
          <>
            <span className="sb-chip sb-chip-inactive">Tenant-scoped access</span>
            <span className="sb-chip sb-chip-inactive">Browser-local preferences</span>
            <span className={session ? "sb-chip sb-chip-active" : "sb-chip sb-chip-inactive"}>
              {session ? "Signed in" : "Sign in needed"}
            </span>
          </>
        }
      />

      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="flex items-start gap-3">
          <div className="rounded-2xl border border-sky-700/30 bg-sky-950/40 p-3">
            <Key className="h-5 w-5 text-sky-300" />
          </div>
          <div>
            <p className="sb-section-title">API access</p>
            <p className="mt-2 text-sm leading-7 text-zinc-300">
              Sign in with a tenant API key to start a session for this browser.
            </p>
            <p className="mt-2 text-sm text-zinc-500">
              The key is sent once and is never stored in the browser. The session it returns is held in a cookie that
              page scripts cannot read, and it expires on its own. Agent and service integrations should use MCP OAuth
              or server-side credentials.
            </p>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="sb-panel-muted p-4">
            <p className="text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">Credential source</p>
            <p className="mt-2 text-sm text-zinc-200">Server-side browser session</p>
          </div>
          <div className="sb-panel-muted p-4">
            <p className="text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">Session state</p>
            <p className={`mt-2 text-sm ${session ? "text-emerald-200" : "text-amber-200"}`}>
              {sessionLoading
                ? "Checking..."
                : session
                  ? `Signed in as ${session.tenant_id}, until ${new Date(session.expires_at).toLocaleString()}`
                  : "Not signed in"}
            </p>
          </div>
        </div>
        {session ? (
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-sm text-zinc-400">
              Session scopes: {session.scopes.join(", ")}
              {session.scopes.includes("admin") ? "" : ". Administration tools are off for this session."}
            </p>
            <button type="button" onClick={handleSignOut} className="sb-button-secondary">
              <LogOut className="h-4 w-4" />
              Sign out
            </button>
          </div>
        ) : (
          <>
            <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
              <div>
                <label htmlFor="settings-api-key" className="mb-1 block text-sm text-zinc-400">
                  Tenant API key
                </label>
                <input
                  id="settings-api-key"
                  type="password"
                  value={browserApiKey}
                  onChange={(e) => setBrowserApiKey(e.target.value)}
                  placeholder="Paste tenant API key"
                  autoComplete="off"
                  className="sb-input"
                />
              </div>
              <div className="flex flex-col gap-2 sm:flex-row md:justify-end">
                <button type="button" onClick={() => void handleSignIn()} className="sb-button-primary">
                  {apiKeySaved ? (
                    <>
                      <CheckCircle2 className="h-4 w-4" />
                      Signed in
                    </>
                  ) : (
                    <>
                      <Key className="h-4 w-4" />
                      Sign in
                    </>
                  )}
                </button>
              </div>
            </div>
            <label className="flex items-start gap-2 text-sm text-zinc-400">
              <input
                type="checkbox"
                checked={elevated}
                onChange={(e) => setElevated(e.target.checked)}
                className="mt-1"
              />
              <span>
                Enable administration tools for this session. Leave this off for ordinary browsing: without it the
                session cannot register MCP clients or mint extension pairing keys, even though the key can.
              </span>
            </label>
          </>
        )}
        <p className={`text-sm ${apiKeyError ? "text-amber-200" : "text-zinc-500"}`}>
          {apiKeyError
            ? apiKeyError
            : session
              ? "Sign out on shared machines when you finish."
              : "The key is exchanged for a session and is not kept in this browser."}
        </p>
      </section>

      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="flex items-start gap-3">
          <div className="rounded-2xl border border-emerald-700/30 bg-emerald-950/40 p-3">
            <ShieldCheck className="h-5 w-5 text-emerald-300" />
          </div>
          <div className="min-w-0 flex-1">
            <p className="sb-section-title">Pair Palace Capture</p>
            <p className="mt-2 text-sm leading-7 text-zinc-300">
              Generate a dedicated one-time key for the browser extension. It can only mint a scoped capture token.
            </p>
            <p className="mt-2 text-sm text-zinc-500">
              The key expires after 10 minutes, works once, and is never stored or shown again by Palace.
            </p>
          </div>
        </div>

        {!pairing ? (
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <button
              type="button"
              onClick={() => void handleGeneratePairingKey()}
              disabled={pairingLoading || !sessionIsElevated}
              className="sb-button-primary"
            >
              {pairingLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Key className="h-4 w-4" />}
              {pairingLoading ? "Generating…" : "Generate pairing key"}
            </button>
            <p className={`text-sm ${pairingError ? "text-amber-200" : "text-zinc-500"}`}>
              {pairingError ??
                (sessionIsElevated
                  ? "Requires tenant admin access."
                  : "Sign in above with administration tools enabled first.")}
            </p>
          </div>
        ) : (
          <div className="rounded-2xl border border-emerald-700/40 bg-emerald-950/25 p-4" data-testid="pairing-key-reveal">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs font-medium uppercase tracking-[0.22em] text-emerald-300">Shown once</p>
                <p className="mt-2 text-sm text-zinc-300">
                  Tenant <span className="font-medium text-zinc-100">{pairing.tenant_id}</span> · Palace Capture token only
                </p>
              </div>
              <button type="button" onClick={() => setPairing(null)} className="sb-button-secondary" aria-label="Dismiss pairing key">
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
              <input aria-label="One-time pairing key" readOnly value={pairing.pairing_key} className="sb-input font-mono" />
              <button type="button" onClick={() => void handleCopyPairingKey()} className="sb-button-primary">
                <ClipboardCopy className="h-4 w-4" />
                {pairingCopied ? "Copied" : "Copy key"}
              </button>
            </div>
            <p className="mt-3 text-sm text-amber-100">
              Expires {new Date(pairing.expires_at).toLocaleString()}. Paste it into Palace Capture now; it becomes invalid after one exchange.
            </p>
            {pairingError ? <p className="mt-2 text-sm text-amber-200">{pairingError}</p> : null}
          </div>
        )}
      </section>

      <section className="sb-panel sb-panel-padding space-y-5">
        <div className="flex items-start gap-3">
          <div className="rounded-2xl border border-zinc-700 bg-zinc-950 p-3">
            <SlidersHorizontal className="h-5 w-5 text-zinc-300" />
          </div>
          <div>
            <p className="sb-section-title">UI preferences</p>
            <p className="mt-2 text-sm text-zinc-400">
              These settings stay in your browser and only affect how the library pages open for you.
            </p>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="sb-panel-muted p-4">
            <label htmlFor="settings-per-page" className="mb-1 block text-sm text-zinc-400">
              Items per page (Library)
            </label>
            <select
              id="settings-per-page"
              value={perPage}
              onChange={(e) => setPerPage(e.target.value)}
              aria-label="Items per page (Library)"
              className="sb-select"
            >
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="50">50</option>
            </select>
          </div>

          <div className="sb-panel-muted p-4">
            <label htmlFor="settings-default-sort" className="mb-1 block text-sm text-zinc-400">
              Default sort order
            </label>
            <select
              id="settings-default-sort"
              value={defaultSort}
              onChange={(e) => setDefaultSort(e.target.value)}
              aria-label="Default sort order"
              className="sb-select"
            >
              <option value="created_at|desc">Newest first</option>
              <option value="created_at|asc">Oldest first</option>
              <option value="title|asc">Title A–Z</option>
            </select>
          </div>
        </div>

        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <button onClick={handleSave} className="sb-button-primary">
            {saved ? (
              <>
                <CheckCircle2 className="h-4 w-4" />
                Saved
              </>
            ) : (
              "Save preferences"
            )}
          </button>
          <p className="text-sm text-zinc-500">
            {storageError
              ? "This browser blocked local storage, so preferences could not be saved."
              : saved
                ? "Preferences updated in local storage."
                : "Applies to this browser only."}
          </p>
        </div>
      </section>
    </div>
  );
}
