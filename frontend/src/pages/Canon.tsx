import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { FolderUp, Loader2, Plus, Search, X } from "lucide-react";

import { api, ApiError } from "../api/client";
import type {
  CanonDigest,
  CanonDriftList,
  CanonImportCommitResponse,
  CanonImportPlan,
  CanonPage,
  CanonRevision,
  CanonSearchResponse,
  CanonSection,
} from "../api/types";
import PageHeader from "../components/PageHeader";
import {
  BODY_MAX_CHARS,
  BODY_WARN_CHARS,
  SECTION_LABELS,
  SECTION_ORDER,
  SUMMARY_MAX_CHARS,
  bodyLengthDelta,
  bodyLengthState,
  formToPayload,
  driftStateLabel,
  formatBytes,
  isDriftActionable,
  importActionLabel,
  importPreflight,
  isImportableName,
  tallyImportActions,
  groupPagesBySection,
} from "../lib/canon";


interface CanonForm {
  slug: string;
  title: string;
  summary: string;
  section: CanonSection;
  roomId: string;
  priority: string;
  pinnedInDigest: boolean;
  body: string;
}

function emptyForm(): CanonForm {
  return {
    slug: "",
    title: "",
    summary: "",
    section: "identity",
    roomId: "",
    priority: "100",
    pinnedInDigest: false,
    body: "",
  };
}

function formFromPage(page: CanonPage): CanonForm {
  return {
    slug: page.slug,
    title: page.title,
    summary: page.summary,
    section: page.section,
    roomId: page.room_id ?? "",
    priority: String(page.priority),
    pinnedInDigest: page.pinned_in_digest,
    body: page.body ?? "",
  };
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

function formatDate(value: string | null): string {
  if (!value) return "unknown time";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

const INPUT_CLASS =
  "w-full rounded-2xl border border-zinc-700 bg-zinc-950/80 px-4 py-2.5 text-sm text-zinc-100 placeholder:text-zinc-500 outline-none transition duration-200 ease-out focus:border-zinc-500 disabled:cursor-not-allowed disabled:text-zinc-500";

const LABEL_CLASS = "text-[11px] font-medium uppercase tracking-[0.2em] text-zinc-500";

export default function Canon() {
  const [pages, setPages] = useState<CanonPage[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [digest, setDigest] = useState<CanonDigest | null>(null);

  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<CanonForm>(emptyForm);
  const [detail, setDetail] = useState<CanonPage | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailToken, setDetailToken] = useState(0);

  const [revisions, setRevisions] = useState<CanonRevision[]>([]);
  const [openBodies, setOpenBodies] = useState<number[]>([]);

  const [preview, setPreview] = useState(false);
  const [writing, setWriting] = useState<string | null>(null);
  const [writeError, setWriteError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<CanonSearchResponse | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  // Folder import. Preview is a read of the folder against the current pages;
  // nothing is written until the operator commits the plan it returned.
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const [importFiles, setImportFiles] = useState<File[]>([]);
  const [importPlan, setImportPlan] = useState<CanonImportPlan | null>(null);
  const [importDecisions, setImportDecisions] = useState<Record<string, string>>({});
  const [importBusy, setImportBusy] = useState<"preview" | "commit" | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [importDone, setImportDone] = useState<CanonImportCommitResponse | null>(null);
  // Drift: the machine proposes, a person ratifies. Loaded separately from the
  // page list so a drift read failure cannot blank the page.
  const [drift, setDrift] = useState<CanonDriftList | null>(null);
  const [driftBusy, setDriftBusy] = useState(false);
  const [driftError, setDriftError] = useState<string | null>(null);
  const importCheck = useMemo(() => importPreflight(importFiles), [importFiles]);
  const importTally = useMemo(
    () =>
      tallyImportActions(
        importPlan?.files ?? [],
        importDecisions,
        (importPlan?.files ?? []).map((file) => file.slug),
      ),
    [importPlan, importDecisions],
  );

  const editorOpen = creating || selectedSlug !== null;

  const loadList = async () => {
    setListLoading(true);
    setListError(null);
    try {
      const response = await api.listCanonPages({ limit: 200 });
      setPages(response.pages);
    } catch (err) {
      setListError(errorMessage(err));
    } finally {
      setListLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await api.listCanonPages({ limit: 200 });
        if (!cancelled) setPages(response.pages);
      } catch (err) {
        if (!cancelled) setListError(errorMessage(err));
      } finally {
        if (!cancelled) setListLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await api.getCanonDigest();
        if (!cancelled) setDigest(response);
      } catch {
        // The digest is a header ornament; a failure here must not blank the page.
        if (!cancelled) setDigest(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedSlug) return;
    const slug = selectedSlug;
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    void (async () => {
      try {
        const [page, revisionResponse] = await Promise.all([
          api.getCanonPage(slug),
          api.listCanonRevisions(slug),
        ]);
        if (cancelled) return;
        setDetail(page);
        setForm(formFromPage(page));
        setRevisions(revisionResponse.revisions);
        setOpenBodies([]);
      } catch (err) {
        if (!cancelled) setDetailError(errorMessage(err));
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedSlug, detailToken]);

  const startNewPage = () => {
    setCreating(true);
    setSelectedSlug(null);
    setDetail(null);
    setRevisions([]);
    setDetailError(null);
    setWriteError(null);
    setPreview(false);
    setOpenBodies([]);
    setForm(emptyForm());
  };

  const selectPage = (slug: string) => {
    setCreating(false);
    setWriteError(null);
    setPreview(false);
    setSelectedSlug(slug);
  };

  const refresh = async () => {
    await loadList();
    setDetailToken((current) => current + 1);
  };

  const submitWrite = async (label: string, action: () => Promise<unknown>) => {
    setWriting(label);
    setWriteError(null);
    try {
      await action();
      await refresh();
    } catch (err) {
      setWriteError(errorMessage(err));
    } finally {
      setWriting(null);
    }
  };

  const saveDraft = () => {
    const title = form.title.trim();
    const summary = form.summary.trim();
    const slug = form.slug.trim();
    const body = form.body;
    if (!title || !summary || !body.trim() || (creating && !slug)) {
      setWriteError("Slug, title, summary and body are all required to save a draft.");
      return;
    }
    // One place turns editor strings into the API payload, so a blank room or a
    // blank priority cannot reach the API as "" or NaN from one call site only.
    const { slug: payloadSlug, ...payload } = formToPayload({ ...form, slug });
    void payloadSlug;
    if (creating) {
      void submitWrite("save", async () => {
        const response = await api.createCanonPage({ ...payload, slug });
        setCreating(false);
        setSelectedSlug(response.page.slug);
      });
      return;
    }
    if (!selectedSlug) return;
    const slugToUpdate = selectedSlug;
    void submitWrite("save", async () => {
      await api.updateCanonPage(slugToUpdate, payload);
    });
  };

  const runSearch = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) {
      setSearch(null);
      return;
    }
    setSearching(true);
    setSearchError(null);
    void (async () => {
      try {
        setSearch(await api.searchCanon(trimmed));
      } catch (err) {
        setSearch(null);
        setSearchError(errorMessage(err));
      } finally {
        setSearching(false);
      }
    })();
  };

  const clearSearch = () => {
    setQuery("");
    setSearch(null);
    setSearchError(null);
  };

  const toggleBody = (revision: number) => {
    setOpenBodies((current) =>
      current.includes(revision) ? current.filter((value) => value !== revision) : [...current, revision],
    );
  };

  const groups = groupPagesBySection(pages).map((group) => ({
    section: group.section,
    items: group.pages,
  }));

  const orderedRevisions = [...revisions].sort((a, b) => b.revision - a.revision);
  const bodyChars = form.body.length;
  const bodyState = bodyLengthState(bodyChars);
  const bodyOverWarn = bodyState !== "ok";


  useEffect(() => {
    let alive = true;
    api
      .getCanonDrift()
      .then((result) => {
        if (alive) {
          setDrift(result);
        }
      })
      .catch(() => {
        if (alive) {
          setDrift(null);
        }
      });
    return () => {
      alive = false;
    };
  }, []);

  async function runDriftDetection() {
    setDriftBusy(true);
    setDriftError(null);
    try {
      await api.detectCanonDrift();
      setDrift(await api.getCanonDrift());
    } catch (error) {
      setDriftError(error instanceof ApiError ? error.message : "Detection did not run.");
    } finally {
      setDriftBusy(false);
    }
  }

  async function reloadCanon() {
    const [list, nextDigest] = await Promise.all([
      api.listCanonPages({ limit: 100 }),
      api.getCanonDigest().catch(() => null),
    ]);
    setPages(list.pages);
    if (nextDigest) {
      setDigest(nextDigest);
    }
  }

  function chooseImportFiles(fileList: FileList | null) {
    const files = Array.from(fileList ?? []).filter((file) => isImportableName(file.name));
    setImportFiles(files);
    setImportPlan(null);
    setImportDecisions({});
    setImportDone(null);
    setImportError(null);
  }

  async function previewImport() {
    if (importFiles.length === 0) {
      return;
    }
    setImportBusy("preview");
    setImportError(null);
    setImportDone(null);
    try {
      const plan = await api.previewCanonImport(importFiles);
      setImportPlan(plan);
      // A conflict starts unanswered, so the operator has to choose. Everything
      // else starts on its planned action.
      const next: Record<string, string> = {};
      for (const file of plan.files) {
        if (file.action === "conflict") {
          next[file.slug] = "skip";
        }
      }
      setImportDecisions(next);
    } catch (error) {
      setImportError(error instanceof ApiError ? error.message : "Could not read that folder.");
    } finally {
      setImportBusy(null);
    }
  }

  async function commitImport() {
    if (!importPlan) {
      return;
    }
    setImportBusy("commit");
    setImportError(null);
    try {
      const result = await api.commitCanonImport(
        importPlan.plan_id,
        importDecisions,
        importFiles,
        false,
      );
      setImportDone(result);
      setImportPlan(null);
      setImportFiles([]);
      await reloadCanon();
    } catch (error) {
      setImportError(error instanceof ApiError ? error.message : "The import did not complete.");
    } finally {
      setImportBusy(null);
    }
  }

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Canon"
        title="Canon"
        description="Canon is authored and authoritative: a person writes it, and it only changes when a person edits, publishes, or ratifies it. Snapshots are derived from captured material and can be regenerated at any time. The two are never merged."
        meta={
          digest ? (
            <>
              <span className="sb-chip sb-chip-inactive">{digest.page_count} pages</span>
              <span className="sb-chip sb-chip-inactive">{digest.char_count} characters</span>
              {digest.truncated ? <span className="sb-chip sb-chip-inactive">Truncated</span> : null}
            </>
          ) : undefined
        }
        actions={
          <form onSubmit={runSearch} className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
            <label className="sr-only" htmlFor="canon-search">
              Search canon
            </label>
            <input
              id="canon-search"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search canon"
              className={`${INPUT_CLASS} sm:w-64`}
            />
            <button type="submit" className="sb-button-secondary" disabled={searching}>
              {searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              Search
            </button>
          </form>
        }
      />


      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="sb-kicker">Drift</p>
            <p className="mt-1 max-w-2xl text-sm text-zinc-400">
              Canon never regenerates. When a room moves past what a page confirmed, Palace says so
              and asks. Detection only proposes. A person ratifies.
            </p>
          </div>
          <button
            type="button"
            className="sb-button-secondary"
            onClick={runDriftDetection}
            disabled={driftBusy}
          >
            {driftBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
            Check for drift
          </button>
        </div>

        {driftError ? <p className="text-sm text-rose-300">{driftError}</p> : null}

        {drift && Object.keys(drift.counts).length > 0 ? (
          <div className="flex flex-wrap items-center gap-2">
            {Object.entries(drift.counts).map(([state, count]) => (
              <span key={state} className="sb-chip sb-chip-inactive">
                {driftStateLabel(state)}: {count}
              </span>
            ))}
          </div>
        ) : null}

        {drift && drift.pages.filter((page) => isDriftActionable(page.state)).length > 0 ? (
          <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800">
            {drift.pages
              .filter((page) => isDriftActionable(page.state))
              .map((page) => (
                <li
                  key={page.slug}
                  className="flex flex-wrap items-center justify-between gap-2 px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm text-zinc-200">{page.title}</p>
                    <p className="truncate text-xs text-zinc-500">
                      canon:{page.slug} in {page.room_slug}: {page.reason}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="sb-chip sb-chip-inactive">{driftStateLabel(page.state)}</span>
                    <button
                      type="button"
                      className="sb-button-secondary"
                      onClick={() => {
                        setCreating(false);
                        setSelectedSlug(page.slug);
                      }}
                    >
                      Open page
                    </button>
                  </div>
                </li>
              ))}
          </ul>
        ) : drift ? (
          <p className="text-sm text-zinc-400">No page has drifted from its room.</p>
        ) : null}
      </section>

      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="sb-kicker">Import a folder</p>
            <p className="mt-1 max-w-2xl text-sm text-zinc-400">
              Markdown files become canon pages as drafts. Nothing is written until you commit
              the plan, and re-importing an unchanged folder writes nothing at all.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={importInputRef}
              type="file"
              multiple
              // Folder selection is a non-standard attribute React does not type.
              {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
              className="hidden"
              onChange={(event) => chooseImportFiles(event.target.files)}
            />
            <button
              type="button"
              className="sb-button-secondary"
              onClick={() => importInputRef.current?.click()}
              disabled={importBusy !== null}
            >
              <FolderUp className="h-4 w-4" />
              Choose folder
            </button>
            <button
              type="button"
              className="sb-button-primary"
              onClick={previewImport}
              disabled={!importCheck.ok || importBusy !== null}
            >
              {importBusy === "preview" ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Search className="h-4 w-4" />
              )}
              Preview
            </button>
          </div>
        </div>

        {importFiles.length > 0 ? (
          <p className="text-sm text-zinc-400">
            {importCheck.fileCount} {importCheck.fileCount === 1 ? "file" : "files"},{" "}
            {formatBytes(importCheck.totalBytes)} selected.
          </p>
        ) : null}

        {importCheck.problems.length > 0 && importFiles.length > 0 ? (
          <ul className="space-y-1 text-sm text-amber-300">
            {importCheck.problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        ) : null}

        {importError ? <p className="text-sm text-rose-300">{importError}</p> : null}

        {importDone ? (
          <p className="text-sm text-emerald-300">
            Imported {importDone.applied} {importDone.applied === 1 ? "page" : "pages"} as drafts.{" "}
            {importDone.skipped > 0 ? `${importDone.skipped} skipped. ` : ""}
            Review and publish when they read right.
          </p>
        ) : null}

        {importPlan ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="sb-chip sb-chip-inactive">{importTally.create} new</span>
              <span className="sb-chip sb-chip-inactive">{importTally.update} updated</span>
              <span className="sb-chip sb-chip-inactive">{importTally.unchanged} unchanged</span>
              <span className="sb-chip sb-chip-inactive">{importTally.conflict} conflicted</span>
              <span className="sb-chip sb-chip-inactive">
                {importTally.writable} will be written
              </span>
            </div>

            <ul className="max-h-96 divide-y divide-zinc-800 overflow-auto rounded-lg border border-zinc-800">
              {importPlan.files.map((file) => (
                <li
                  key={file.path}
                  className="flex flex-wrap items-center justify-between gap-2 px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm text-zinc-200">{file.title}</p>
                    <p className="truncate text-xs text-zinc-500">
                      {file.path} to {file.slug}: {file.reason}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="sb-chip sb-chip-inactive">{importActionLabel(file.action)}</span>
                    {file.action === "conflict" ? (
                      <select
                        aria-label={`Decision for ${file.slug}`}
                        className={INPUT_CLASS}
                        value={importDecisions[file.slug] ?? "skip"}
                        onChange={(event) =>
                          setImportDecisions((current) => ({
                            ...current,
                            [file.slug]: event.target.value,
                          }))
                        }
                      >
                        <option value="skip">Skip</option>
                        <option value="apply">Overwrite</option>
                        <option value="rename">Import as a new page</option>
                      </select>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="sb-button-primary"
                onClick={commitImport}
                disabled={importBusy !== null || importTally.writable === 0}
              >
                {importBusy === "commit" ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <FolderUp className="h-4 w-4" />
                )}
                Import {importTally.writable} {importTally.writable === 1 ? "page" : "pages"}
              </button>
              <button
                type="button"
                className="sb-button-secondary"
                onClick={() => {
                  setImportPlan(null);
                  setImportFiles([]);
                  setImportDecisions({});
                }}
                disabled={importBusy !== null}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : null}
      </section>

      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="sb-kicker">Pages</p>
            <p className="mt-1 text-sm text-zinc-400">
              {pages.length} {pages.length === 1 ? "page" : "pages"} across {groups.length}{" "}
              {groups.length === 1 ? "section" : "sections"}
            </p>
          </div>
          <button type="button" className="sb-button-primary" onClick={startNewPage}>
            <Plus className="h-4 w-4" />
            New page
          </button>
        </div>

        {searchError ? (
          <div role="alert" className="rounded-2xl border border-zinc-700 bg-zinc-950/80 px-4 py-3 text-sm text-zinc-100">
            {searchError}
          </div>
        ) : null}

        {search ? (
          <div className="space-y-3 rounded-2xl border border-zinc-800 bg-zinc-950/60 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className={LABEL_CLASS}>
                Search results for “{search.query}” · {search.total}
              </p>
              <button type="button" className="sb-button-ghost" onClick={clearSearch}>
                <X className="h-4 w-4" />
                Clear search
              </button>
            </div>
            {search.hits.length === 0 ? (
              <p className="text-sm text-zinc-400">No canon pages matched that query.</p>
            ) : (
              <ul className="space-y-2">
                {search.hits.map((hit) => (
                  <li key={hit.slug}>
                    <button
                      type="button"
                      onClick={() => selectPage(hit.slug)}
                      className="w-full rounded-2xl border border-zinc-800 bg-zinc-950/70 px-4 py-3 text-left transition duration-200 ease-out hover:border-zinc-600"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs text-zinc-300">{hit.slug}</span>
                        <span className="sb-chip sb-chip-inactive">{SECTION_LABELS[hit.section]}</span>
                        <span className="sb-chip sb-chip-inactive">score {hit.score.toFixed(2)}</span>
                        {hit.truncated ? <span className="sb-chip sb-chip-inactive">Snippet cut</span> : null}
                      </div>
                      <p className="mt-2 text-sm leading-6 text-zinc-300">{hit.snippet}</p>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : null}

        {listError ? (
          <div role="alert" className="rounded-2xl border border-zinc-700 bg-zinc-950/80 px-4 py-3 text-sm text-zinc-100">
            {listError}
          </div>
        ) : null}

        {listLoading ? (
          <div className="flex min-h-40 items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-950/60">
            <Loader2 className="h-5 w-5 animate-spin text-zinc-400" />
          </div>
        ) : null}

        {!listLoading && !listError && pages.length === 0 ? (
          <div className="rounded-2xl border border-zinc-800 bg-zinc-950/60 px-4 py-6">
            <p className="text-sm leading-6 text-zinc-300">
              No canon pages yet. Write the first one to state what this tenant is for.
            </p>
          </div>
        ) : null}

        {!listLoading && !listError && pages.length > 0 ? (
          <div className="space-y-6">
            {groups.map((group) => (
              <div key={group.section} className="space-y-2">
                <h2 className="sb-kicker">{SECTION_LABELS[group.section]}</h2>
                <ul className="divide-y divide-zinc-800 rounded-2xl border border-zinc-800 bg-zinc-950/60">
                  {group.items.map((page) => {
                    const isSelected = page.slug === selectedSlug && !creating;
                    return (
                      <li key={page.slug}>
                        <button
                          type="button"
                          onClick={() => selectPage(page.slug)}
                          aria-pressed={isSelected}
                          className={`flex w-full flex-wrap items-center gap-3 px-4 py-3 text-left transition duration-200 ease-out hover:bg-zinc-900/60 ${
                            isSelected ? "bg-zinc-900/70" : ""
                          }`}
                        >
                          <span className="min-w-0 flex-1 break-words text-sm font-medium text-zinc-100">
                            {page.title}
                          </span>
                          <span className="font-mono text-xs text-zinc-500">{page.slug}</span>
                          <span className="sb-chip sb-chip-inactive">{page.status}</span>
                          <span className="text-xs text-zinc-400">rev {page.revision_count}</span>
                          <span className="text-xs text-zinc-400">{page.char_count} characters</span>
                          {page.pinned_in_digest ? (
                            <span className="sb-chip sb-chip-inactive">Pinned</span>
                          ) : null}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </div>
        ) : null}
      </section>

      {editorOpen ? (
        <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,24rem)]">
          <div className="sb-panel sb-panel-padding space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="sb-kicker">{creating ? "New canon page" : "Edit canon page"}</p>
                <p className="mt-1 text-sm text-zinc-400">
                  {creating
                    ? "A new page starts as a draft and is not visible to agents until it is published."
                    : `Revision ${detail?.revision_count ?? 0} · status ${detail?.status ?? "draft"}`}
                </p>
              </div>
              {!creating && detail ? (
                <span className="sb-chip sb-chip-inactive">{detail.status}</span>
              ) : null}
            </div>

            {detailLoading ? (
              <div className="flex min-h-24 items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-950/60">
                <Loader2 className="h-5 w-5 animate-spin text-zinc-400" />
              </div>
            ) : null}

            {detailError ? (
              <div role="alert" className="rounded-2xl border border-zinc-700 bg-zinc-950/80 px-4 py-3 text-sm text-zinc-100">
                {detailError}
              </div>
            ) : null}

            {writeError ? (
              <div role="alert" className="rounded-2xl border border-zinc-700 bg-zinc-950/80 px-4 py-3 text-sm text-zinc-100">
                {writeError}
              </div>
            ) : null}

            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-1">
                <label className={LABEL_CLASS} htmlFor="canon-slug">
                  Slug
                </label>
                <input
                  id="canon-slug"
                  type="text"
                  value={form.slug}
                  disabled={!creating}
                  onChange={(event) => setForm({ ...form, slug: event.target.value })}
                  placeholder="what-this-page-covers"
                  className={`${INPUT_CLASS} font-mono`}
                />
              </div>
              <div className="space-y-1">
                <label className={LABEL_CLASS} htmlFor="canon-title">
                  Title
                </label>
                <input
                  id="canon-title"
                  type="text"
                  value={form.title}
                  onChange={(event) => setForm({ ...form, title: event.target.value })}
                  className={INPUT_CLASS}
                />
              </div>
              <div className="space-y-1 md:col-span-2">
                <label className={LABEL_CLASS} htmlFor="canon-summary">
                  Summary
                </label>
                <input
                  id="canon-summary"
                  type="text"
                  value={form.summary}
                  maxLength={SUMMARY_MAX_CHARS}
                  onChange={(event) => setForm({ ...form, summary: event.target.value })}
                  className={INPUT_CLASS}
                />
                <p className="text-xs text-zinc-500">
                  {form.summary.length}/{SUMMARY_MAX_CHARS} characters
                </p>
              </div>
              <div className="space-y-1">
                <label className={LABEL_CLASS} htmlFor="canon-section">
                  Section
                </label>
                <select
                  id="canon-section"
                  value={form.section}
                  onChange={(event) => {
                    const next = event.target.value as CanonSection;
                    setForm({ ...form, section: next });
                  }}
                  className={INPUT_CLASS}
                >
                  {SECTION_ORDER.map((section) => (
                    <option key={section} value={section}>
                      {SECTION_LABELS[section]}
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-1">
                <label className={LABEL_CLASS} htmlFor="canon-room">
                  Room ID (optional)
                </label>
                <input
                  id="canon-room"
                  type="text"
                  value={form.roomId}
                  onChange={(event) => setForm({ ...form, roomId: event.target.value })}
                  placeholder="Leave blank for tenant-wide canon"
                  className={`${INPUT_CLASS} font-mono`}
                />
              </div>
              <div className="space-y-1">
                <label className={LABEL_CLASS} htmlFor="canon-priority">
                  Priority
                </label>
                <input
                  id="canon-priority"
                  type="number"
                  min={0}
                  max={10000}
                  value={form.priority}
                  onChange={(event) => setForm({ ...form, priority: event.target.value })}
                  className={INPUT_CLASS}
                />
              </div>
              <div className="flex items-end">
                <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
                  <input
                    type="checkbox"
                    checked={form.pinnedInDigest}
                    onChange={(event) => setForm({ ...form, pinnedInDigest: event.target.checked })}
                    className="h-4 w-4 rounded border-zinc-700 bg-zinc-950 text-zinc-100"
                  />
                  Pinned in digest
                </label>
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <label className={LABEL_CLASS} htmlFor="canon-body">
                  Body (markdown)
                </label>
                <button
                  type="button"
                  className="sb-button-ghost"
                  onClick={() => setPreview((current) => !current)}
                  aria-pressed={preview}
                >
                  {preview ? "Edit body" : "Preview"}
                </button>
              </div>
              {preview ? (
                <div className="min-h-64 space-y-3 rounded-2xl border border-zinc-800 bg-zinc-950/60 px-4 py-4">
                  {form.body.trim() ? (
                    form.body
                      .split(/\n\s*\n/)
                      .map((block) => block.trim())
                      .filter((block) => block.length > 0)
                      .map((block, index) => {
                        const lines = block.split("\n");
                        const heading = /^(#{1,6})\s+(.*)$/.exec(lines[0] ?? "");
                        if (heading) {
                          const level = heading[1].length;
                          const text = [heading[2], ...lines.slice(1)].join(" ").trim();
                          const Tag = level <= 2 ? "h2" : "h3";
                          return (
                            <Tag
                              key={`block-${index}`}
                              className={
                                level <= 1
                                  ? "text-xl font-semibold text-zinc-50"
                                  : level === 2
                                    ? "text-lg font-semibold text-zinc-50"
                                    : "text-sm font-semibold uppercase tracking-[0.2em] text-zinc-400"
                              }
                            >
                              {text}
                            </Tag>
                          );
                        }
                        return (
                          <p key={`block-${index}`} className="whitespace-pre-line text-sm leading-6 text-zinc-300">
                            {block}
                          </p>
                        );
                      })
                  ) : (
                    <p className="text-sm text-zinc-500">Nothing to preview yet.</p>
                  )}
                </div>
              ) : (
                <textarea
                  id="canon-body"
                  value={form.body}
                  onChange={(event) => setForm({ ...form, body: event.target.value })}
                  rows={16}
                  spellCheck={false}
                  className="w-full resize-y rounded-2xl border border-zinc-700 bg-zinc-950/80 px-4 py-3 font-mono text-xs leading-5 text-zinc-100 outline-none transition duration-200 ease-out placeholder:text-zinc-500 focus:border-zinc-500"
                  placeholder={"# Heading\n\nWhat this tenant is for, in plain sentences."}
                />
              )}
              <p className={`text-xs ${bodyOverWarn ? "font-semibold text-zinc-100" : "text-zinc-500"}`}>
                {bodyChars} characters · hard cap {BODY_MAX_CHARS}
                {bodyOverWarn ? ` · past ${BODY_WARN_CHARS}, trim before saving` : ""}
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="sb-button-primary"
                onClick={saveDraft}
                disabled={writing !== null}
              >
                {writing === "save" ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                Save draft
              </button>
              {!creating && detail?.status === "draft" && selectedSlug ? (
                <button
                  type="button"
                  className="sb-button-secondary"
                  disabled={writing !== null}
                  onClick={() =>
                    void submitWrite("publish", () => api.publishCanonPage(selectedSlug))
                  }
                >
                  {writing === "publish" ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                  Publish
                </button>
              ) : null}
              {!creating && detail?.status === "published" && selectedSlug ? (
                <button
                  type="button"
                  className="sb-button-secondary"
                  disabled={writing !== null}
                  onClick={() =>
                    void submitWrite("ratify", () => api.ratifyCanonPage(selectedSlug))
                  }
                >
                  {writing === "ratify" ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                  Ratify
                </button>
              ) : null}
              {!creating && detail && detail.status !== "archived" && selectedSlug ? (
                <button
                  type="button"
                  className="sb-button-secondary"
                  disabled={writing !== null}
                  onClick={() =>
                    void submitWrite("archive", () => api.archiveCanonPage(selectedSlug))
                  }
                >
                  {writing === "archive" ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                  Archive
                </button>
              ) : null}
              <button
                type="button"
                className="sb-button-ghost"
                onClick={() => {
                  setCreating(false);
                  setSelectedSlug(null);
                  setDetail(null);
                  setRevisions([]);
                  setWriteError(null);
                  setDetailError(null);
                  setPreview(false);
                  setForm(emptyForm());
                }}
              >
                Close
              </button>
            </div>
          </div>

          <div className="sb-panel sb-panel-padding space-y-3">
            <div>
              <p className="sb-kicker">Revision history</p>
              <p className="mt-1 text-sm text-zinc-400">
                {creating
                  ? "Save the page to start its history."
                  : `${revisions.length} recorded ${revisions.length === 1 ? "revision" : "revisions"}, newest first`}
              </p>
            </div>

            {!creating && orderedRevisions.length === 0 ? (
              <p className="text-sm text-zinc-400">No revisions recorded for this page yet.</p>
            ) : null}

            <ol className="space-y-3">
              {orderedRevisions.map((revision, index) => {
                const older = orderedRevisions[index + 1];
                const isOpen = openBodies.includes(revision.revision);
                return (
                  <li
                    key={revision.revision}
                    className="space-y-2 rounded-2xl border border-zinc-800 bg-zinc-950/60 px-4 py-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium text-zinc-100">rev {revision.revision}</span>
                      <span className="text-xs text-zinc-500">{formatDate(revision.created_at)}</span>
                    </div>
                    <p className="text-xs text-zinc-400">{revision.created_by_subject ?? "unknown author"}</p>
                    <p className="text-sm leading-6 text-zinc-300">{revision.summary}</p>
                    {revision.change_note ? (
                      <p className="text-xs leading-5 text-zinc-400">{revision.change_note}</p>
                    ) : null}
                    <p className="text-xs text-zinc-500">
                      {bodyLengthDelta(revision, older)} · length delta, not a diff
                    </p>
                    <button
                      type="button"
                      className="sb-button-ghost"
                      onClick={() => toggleBody(revision.revision)}
                      aria-expanded={isOpen}
                    >
                      {isOpen ? "Hide body" : "Show body"}
                    </button>
                    {isOpen ? (
                      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-2xl border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs leading-5 text-zinc-300">
                        {revision.body_md}
                      </pre>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          </div>
        </section>
      ) : null}
    </div>
  );
}
