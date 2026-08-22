import { classifyCapture, labelForCaptureKind, type CaptureClassification } from "./shared/classifier.js";
import { getCredentials, type PalaceCredentials } from "./shared/credentials.js";
import { extractXPostImageCandidates, type BrowserImageCandidate } from "./shared/imageCandidates.js";
import { decodeGrabbedImage, grabImageBytesFromTab, type GrabbedImageBytes } from "./shared/imageBytes.js";
import {
  lookupWebSavesForUrl,
  submitCapture,
  uploadCaptureImage,
  type WebSave,
} from "./shared/palaceClient.js";

type CurrentTabContext = {
  imageCandidates: BrowserImageCandidate[];
  tabId: number | null;
  title: string;
  url: string | null;
  selectionText: string | null;
};

type GrabbedCandidate = {
  candidate: BrowserImageCandidate;
  grabbed: GrabbedImageBytes;
};

const kindLabel = document.querySelector<HTMLSpanElement>("#kindLabel");
const stateLabel = document.querySelector<HTMLSpanElement>("#stateLabel");
const pageTitle = document.querySelector<HTMLParagraphElement>("#pageTitle");
const reason = document.querySelector<HTMLParagraphElement>("#reason");
const tagsInput = document.querySelector<HTMLInputElement>("#tags");
const saveButton = document.querySelector<HTMLButtonElement>("#saveButton");
const settingsButton = document.querySelector<HTMLButtonElement>("#settingsButton");
const message = document.querySelector<HTMLParagraphElement>("#message");
const savedPanel = document.querySelector<HTMLElement>("#savedPanel");
const savedLabel = document.querySelector<HTMLSpanElement>("#savedLabel");
const relatedCount = document.querySelector<HTMLSpanElement>("#relatedCount");
const relatedList = document.querySelector<HTMLUListElement>("#relatedList");

let currentContext: CurrentTabContext | null = null;
let currentClassification: CaptureClassification | null = null;
let currentSavedWebSave: WebSave | null = null;

function setMessage(text: string, tone: "default" | "error" | "success" = "default"): void {
  if (!message) return;
  message.textContent = text;
  message.className = `message ${tone === "default" ? "" : tone}`.trim();
}

function setBusy(isBusy: boolean): void {
  if (saveButton) {
    saveButton.disabled = isBusy || currentClassification?.kind === "invalid";
    saveButton.textContent = isBusy ? "Saving..." : currentSavedWebSave ? "Saved in Palace" : "Save to Palace";
  }
  if (stateLabel) stateLabel.textContent = isBusy ? "Saving" : "Ready";
}

function setSavedState(save: WebSave | null): void {
  currentSavedWebSave = save;
  if (saveButton) {
    saveButton.textContent = save ? "Saved in Palace" : "Save to Palace";
  }
  if (stateLabel) {
    stateLabel.textContent = save ? "Saved" : "Ready";
  }
}

async function readCurrentTab(): Promise<CurrentTabContext> {
  if (typeof chrome === "undefined" || !chrome.tabs?.query || !chrome.scripting?.executeScript) {
    return {
      imageCandidates: [],
      tabId: null,
      title: "Example article",
      url: "https://example.com/article",
      selectionText: null,
    };
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const tabId = tab.id;
  const selectionResults =
    tabId !== undefined
      ? await chrome.scripting.executeScript({
          target: { tabId },
          func: () => window.getSelection()?.toString() ?? "",
        })
      : [];
  const imageCandidateResults =
    tabId !== undefined && tab.url
      ? await chrome.scripting
          .executeScript({
            target: { tabId },
            func: extractXPostImageCandidates,
            args: [tab.url],
          })
          .catch(() => [])
      : [];
  return {
    imageCandidates: imageCandidateResults[0]?.result ?? [],
    tabId: tabId ?? null,
    title: tab.title?.trim() || tab.url || "Current tab",
    url: tab.url ?? null,
    selectionText: selectionResults[0]?.result?.trim() || null,
  };
}

function renderContext(context: CurrentTabContext, classification: CaptureClassification): void {
  if (kindLabel) kindLabel.textContent = labelForCaptureKind(classification.kind);
  if (pageTitle) pageTitle.textContent = context.title;
  if (reason) reason.textContent = classification.reason;
  if (saveButton) saveButton.disabled = classification.kind === "invalid";
  if (classification.kind === "invalid") {
    setMessage(classification.reason, "error");
  }
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function titleForWebSave(save: WebSave): string {
  return save.source_title?.trim() || save.item.title?.trim() || save.source_domain || save.normalized_url;
}

function renderSavedPanel(saved: WebSave | null, related: WebSave[]): void {
  if (!savedPanel || !savedLabel || !relatedCount || !relatedList) return;
  savedPanel.hidden = false;
  savedLabel.textContent = saved ? "Already saved" : "Not saved yet";
  relatedCount.textContent = related.length ? `${related.length} related` : "";
  relatedList.replaceChildren();
  if (!related.length) {
    const empty = document.createElement("li");
    empty.className = "related-empty";
    empty.textContent = saved ? "No other active web saves from this page context." : "No related active web saves yet.";
    relatedList.append(empty);
    return;
  }
  related.forEach((save) => {
    const item = document.createElement("li");
    item.className = "related-item";

    const title = document.createElement("span");
    title.className = "related-title";
    title.textContent = titleForWebSave(save);

    const meta = document.createElement("span");
    meta.className = "related-meta";
    meta.textContent = [save.source_domain, formatDate(save.saved_at)].filter(Boolean).join(" · ");

    item.append(title, meta);
    relatedList.append(item);
  });
}

function renderSavedStatus(label: string, detail: string): void {
  if (!savedPanel || !savedLabel || !relatedCount || !relatedList) return;
  savedPanel.hidden = false;
  savedLabel.textContent = label;
  relatedCount.textContent = "";
  relatedList.replaceChildren();
  const item = document.createElement("li");
  item.className = "related-empty";
  item.textContent = detail;
  relatedList.append(item);
}

async function refreshWebSaveContext(): Promise<void> {
  if (!currentContext?.url || currentClassification?.kind === "invalid") return;
  const credentials = await getCredentials();
  if (!credentials) {
    if (savedPanel) savedPanel.hidden = true;
    return;
  }
  if (savedLabel) savedLabel.textContent = "Checking saved state";
  const result = await lookupWebSavesForUrl(credentials, currentContext.url);
  if (result.state === "ready") {
    setSavedState(result.saved);
    renderSavedPanel(result.saved, result.related);
    return;
  }
  renderSavedStatus("Unable to check saved state", "Try again after Palace is reachable.");
  setMessage(result.message, result.state === "auth_error" ? "error" : "default");
}

function parseTags(): string[] {
  return (tagsInput?.value ?? "")
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

/**
 * Reads the bytes of every image candidate the page offered. A candidate whose
 * bytes cannot be read is dropped here, not failed: the caller then sends its
 * URL instead and lets Palace try the download.
 */
async function grabImageCandidates(context: CurrentTabContext): Promise<GrabbedCandidate[]> {
  if (context.tabId === null || !context.imageCandidates.length) return [];
  const grabbedCandidates: GrabbedCandidate[] = [];
  for (const candidate of context.imageCandidates) {
    const grabbed = await grabImageBytesFromTab(context.tabId, candidate.url);
    if (grabbed) grabbedCandidates.push({ candidate, grabbed });
  }
  return grabbedCandidates;
}

async function uploadGrabbedCandidates(
  credentials: PalaceCredentials,
  context: CurrentTabContext,
  itemId: string,
  grabbedCandidates: GrabbedCandidate[],
): Promise<number> {
  let stored = 0;
  for (const [index, { candidate, grabbed }] of grabbedCandidates.entries()) {
    const result = await uploadCaptureImage(credentials, {
      bytes: decodeGrabbedImage(grabbed),
      mediaType: grabbed.mediaType,
      byteOrigin: grabbed.origin,
      itemId,
      imageUrl: candidate.url,
      sourceUrl: candidate.source_post_url ?? context.url,
      altText: candidate.alt_text ?? grabbed.altText ?? null,
      width: candidate.width ?? grabbed.width ?? null,
      height: candidate.height ?? grabbed.height ?? null,
      role: candidate.role ?? null,
      order: candidate.order ?? index,
    });
    if (result.state === "queued" || result.state === "duplicate") stored += 1;
  }
  return stored;
}

/** Uploads the image in the tab itself. Returns false to fall back to a URL save. */
async function saveImageBytesCapture(
  credentials: PalaceCredentials,
  context: CurrentTabContext,
  classification: CaptureClassification,
): Promise<boolean> {
  if (context.tabId === null || !classification.url) return false;
  const grabbed = await grabImageBytesFromTab(context.tabId, classification.url);
  if (!grabbed) return false;

  const result = await uploadCaptureImage(credentials, {
    bytes: decodeGrabbedImage(grabbed),
    mediaType: grabbed.mediaType,
    byteOrigin: grabbed.origin,
    imageUrl: classification.url,
    sourceUrl: context.url,
    pageTitle: context.title,
    altText: grabbed.altText ?? null,
    width: grabbed.width ?? null,
    height: grabbed.height ?? null,
    tags: parseTags(),
  });

  if (result.state === "queued") {
    const note = grabbed.origin === "canvas" ? " Re-encoded from the rendered image." : "";
    setMessage(`Uploaded the image file to Palace.${note}`, "success");
    return true;
  }
  if (result.state === "duplicate") {
    setMessage(result.message, "success");
    return true;
  }
  if (result.state === "auth_error") {
    setMessage(result.message, "error");
    if (typeof chrome !== "undefined" && chrome.runtime?.openOptionsPage) {
      await chrome.runtime.openOptionsPage();
    }
    return true;
  }
  setMessage(result.message, "error");
  return true;
}

async function saveUrlCapture(
  credentials: PalaceCredentials,
  context: CurrentTabContext,
  classification: CaptureClassification,
  notice = "",
): Promise<void> {
  // Bytes read from the page beat a URL Palace has to fetch, so the candidate
  // URLs are only sent for the images whose bytes could not be read.
  const grabbedCandidates =
    classification.kind === "social_post" ? await grabImageCandidates(context) : [];
  const grabbedUrls = new Set(grabbedCandidates.map(({ candidate }) => candidate.url));
  const remainingCandidates =
    classification.kind === "social_post"
      ? context.imageCandidates.filter((candidate) => !grabbedUrls.has(candidate.url))
      : [];

  const result = await submitCapture(credentials, {
    classification,
    imageCandidates: remainingCandidates,
    pageTitle: context.title,
    selectionText: context.selectionText,
    tags: parseTags(),
  });

  if (result.state === "queued") {
    const stored = result.itemId
      ? await uploadGrabbedCandidates(credentials, context, result.itemId, grabbedCandidates)
      : 0;
    const imageNote = stored ? ` Uploaded ${stored} image file${stored === 1 ? "" : "s"}.` : "";
    setMessage(
      `${notice}Queued ${labelForCaptureKind(result.kind).toLowerCase()} capture. Job ${result.jobId}.${imageNote}`,
      "success",
    );
    return;
  }
  if (result.state === "duplicate") {
    // The capture already exists, but its images may not: an earlier save can
    // have lost them to a download Palace was never able to make.
    if (result.itemId) {
      await uploadGrabbedCandidates(credentials, context, result.itemId, grabbedCandidates);
    }
    const savedUrl = classification.url ?? context.url ?? "";
    const savedKind = classification.kind === "invalid" ? "webpage" : classification.kind;
    setSavedState({
      id: result.webSaveId ?? "duplicate",
      item_id: result.itemId ?? "duplicate",
      original_url: savedUrl,
      normalized_url: savedUrl,
      source_title: context.title,
      source_domain: null,
      capture_kind: savedKind,
      user_tags: parseTags(),
      saved_at: new Date().toISOString(),
      archived_at: null,
      item: {
        id: result.itemId ?? "duplicate",
        title: context.title,
        source_type: "webpage",
        status: "ready",
        summary: null,
        tags: parseTags(),
      },
    });
    setMessage(`${notice}${result.message}`, "success");
    return;
  }
  if (result.state === "auth_error") {
    setMessage(result.message, "error");
    if (typeof chrome !== "undefined" && chrome.runtime?.openOptionsPage) {
      await chrome.runtime.openOptionsPage();
    }
    return;
  }
  setMessage(result.message, "error");
}

async function saveCapture(): Promise<void> {
  if (!currentContext || !currentClassification) return;
  const credentials = await getCredentials();
  if (!credentials) {
    setMessage("Create a scoped capture token in Settings.", "error");
    if (typeof chrome !== "undefined" && chrome.runtime?.openOptionsPage) {
      await chrome.runtime.openOptionsPage();
    }
    return;
  }

  const context = currentContext;
  const classification = currentClassification;
  setBusy(true);
  setMessage("");
  try {
    // The bytes are the point, but an unreadable image is still worth saving
    // by URL: Palace may be able to fetch what the page would not hand over.
    const uploaded =
      classification.kind === "image" &&
      (await saveImageBytesCapture(credentials, context, classification));
    if (uploaded) return;
    const notice =
      classification.kind === "image" ? "Could not read the image file, so the URL was saved. " : "";
    await saveUrlCapture(credentials, context, classification, notice);
  } finally {
    setBusy(false);
  }
}

async function init(): Promise<void> {
  try {
    currentContext = await readCurrentTab();
    currentClassification = classifyCapture({
      url: currentContext.url,
      selectionText: currentContext.selectionText,
    });
    renderContext(currentContext, currentClassification);
    void refreshWebSaveContext();
  } catch (error) {
    setMessage(error instanceof Error ? error.message : "Unable to inspect the current tab.", "error");
    if (saveButton) saveButton.disabled = true;
  }
}

saveButton?.addEventListener("click", () => {
  void saveCapture();
});

settingsButton?.addEventListener("click", () => {
  if (typeof chrome === "undefined") {
    setMessage("Load as an extension to open settings.");
    return;
  }
  if (chrome.runtime?.openOptionsPage) {
    void chrome.runtime.openOptionsPage();
  } else {
    setMessage("Load as an extension to open settings.");
  }
});

void init();
