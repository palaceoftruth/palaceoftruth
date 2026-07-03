import { useState } from "react";
import { CheckCircle2, Key, SlidersHorizontal } from "lucide-react";

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
  const [saved, setSaved] = useState(false);
  const [storageError, setStorageError] = useState(false);

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

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Configuration"
        title="Settings"
        description="Manage local browsing defaults and confirm how the frontend is talking to the backend in this environment."
        meta={
          <>
            <span className="sb-chip sb-chip-inactive">Read-only environment</span>
            <span className="sb-chip sb-chip-inactive">Browser-local preferences</span>
            <span className="sb-chip sb-chip-inactive">No browser API key</span>
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
              Browser requests no longer receive the shared backend API key from the frontend proxy.
            </p>
            <p className="mt-2 text-sm text-zinc-500">
              Agent and service integrations should use MCP OAuth or server-side credentials.
            </p>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="sb-panel-muted p-4">
            <p className="text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">Credential source</p>
            <p className="mt-2 text-sm text-zinc-200">MCP OAuth or backend service key</p>
          </div>
          <div className="sb-panel-muted p-4">
            <p className="text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">Browser key state</p>
            <p className="mt-2 text-sm text-emerald-200">No shared secret exposed</p>
          </div>
        </div>
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
