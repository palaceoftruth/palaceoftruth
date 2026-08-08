import { useState } from "react";
import { CheckCircle2, ClipboardCopy, Key, Loader2, ShieldCheck, SlidersHorizontal, Trash2, X } from "lucide-react";

import { api, BROWSER_API_KEY_STORAGE_KEY, readBrowserApiKey } from "../api/client";
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
  const [hasBrowserApiKey, setHasBrowserApiKey] = useState(() => Boolean(readBrowserApiKey()));
  const [saved, setSaved] = useState(false);
  const [apiKeySaved, setApiKeySaved] = useState(false);
  const [apiKeyError, setApiKeyError] = useState<string | null>(null);
  const [storageError, setStorageError] = useState(false);
  const [pairing, setPairing] = useState<BrowserExtensionPairingKey | null>(null);
  const [pairingLoading, setPairingLoading] = useState(false);
  const [pairingError, setPairingError] = useState<string | null>(null);
  const [pairingCopied, setPairingCopied] = useState(false);

  const handleSaveApiKey = () => {
    const trimmed = browserApiKey.trim();
    if (!trimmed) {
      setApiKeyError("Enter an API key before saving.");
      setApiKeySaved(false);
      return;
    }
    const savedApiKey = writeLocalStorage(BROWSER_API_KEY_STORAGE_KEY, trimmed);
    setApiKeyError(savedApiKey ? null : "This browser blocked local storage, so the API key could not be saved.");
    setApiKeySaved(savedApiKey);
    setHasBrowserApiKey(savedApiKey);
    if (savedApiKey) {
      setBrowserApiKey("");
      setTimeout(() => setApiKeySaved(false), 2000);
    }
  };

  const handleClearApiKey = () => {
    try {
      localStorage.removeItem(BROWSER_API_KEY_STORAGE_KEY);
      setBrowserApiKey("");
      setHasBrowserApiKey(false);
      setApiKeySaved(false);
      setApiKeyError(null);
      setStorageError(false);
    } catch {
      setApiKeyError("This browser blocked local storage, so the API key could not be cleared.");
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
            <span className={hasBrowserApiKey ? "sb-chip sb-chip-active" : "sb-chip sb-chip-inactive"}>
              {hasBrowserApiKey ? "Browser API key saved" : "Browser API key needed"}
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
              Store a tenant API key in this browser so hosted UI requests can authenticate with the backend.
            </p>
            <p className="mt-2 text-sm text-zinc-500">
              The key stays in local storage on this device. Agent and service integrations should use MCP OAuth or server-side credentials.
            </p>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="sb-panel-muted p-4">
            <p className="text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">Credential source</p>
            <p className="mt-2 text-sm text-zinc-200">Browser-local tenant API key</p>
          </div>
          <div className="sb-panel-muted p-4">
            <p className="text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">Browser key state</p>
            <p className={`mt-2 text-sm ${hasBrowserApiKey ? "text-emerald-200" : "text-amber-200"}`}>
              {hasBrowserApiKey ? "Saved on this device" : "Not configured"}
            </p>
          </div>
        </div>
        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
          <div>
            <label htmlFor="settings-api-key" className="mb-1 block text-sm text-zinc-400">
              Browser API key
            </label>
            <input
              id="settings-api-key"
              type="password"
              value={browserApiKey}
              onChange={(e) => setBrowserApiKey(e.target.value)}
              placeholder={hasBrowserApiKey ? "Stored API key unchanged" : "Paste tenant API key"}
              autoComplete="off"
              className="sb-input"
            />
          </div>
          <div className="flex flex-col gap-2 sm:flex-row md:justify-end">
            <button type="button" onClick={handleSaveApiKey} className="sb-button-primary">
              {apiKeySaved ? (
                <>
                  <CheckCircle2 className="h-4 w-4" />
                  Saved
                </>
              ) : (
                <>
                  <Key className="h-4 w-4" />
                  Save API key
                </>
              )}
            </button>
            <button
              type="button"
              onClick={handleClearApiKey}
              disabled={!hasBrowserApiKey && !browserApiKey}
              className="sb-button-secondary"
            >
              <Trash2 className="h-4 w-4" />
              Clear
            </button>
          </div>
        </div>
        <p className={`text-sm ${apiKeyError ? "text-amber-200" : "text-zinc-500"}`}>
          {apiKeyError
            ? apiKeyError
            : apiKeySaved
              ? "API key saved for this browser."
              : "Use Clear on shared machines after finishing a browser session."}
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
              disabled={pairingLoading || !hasBrowserApiKey}
              className="sb-button-primary"
            >
              {pairingLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Key className="h-4 w-4" />}
              {pairingLoading ? "Generating…" : "Generate pairing key"}
            </button>
            <p className={`text-sm ${pairingError ? "text-amber-200" : "text-zinc-500"}`}>
              {pairingError ?? (hasBrowserApiKey ? "Requires tenant admin access." : "Save an admin tenant API key above first.")}
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
