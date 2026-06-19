import { classifyCapture, labelForCaptureKind, type CaptureClassification } from "./shared/classifier.js";
import { getCredentials } from "./shared/credentials.js";
import { extractXPostImageCandidates, type BrowserImageCandidate } from "./shared/imageCandidates.js";
import { lookupWebSavesForUrl, submitCapture, type WebSave } from "./shared/palaceClient.js";

type CurrentTabContext = {
  imageCandidates: BrowserImageCandidate[];
  title: string;
  url: string | null;
  selectionText: string | null;
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

  setBusy(true);
  setMessage("");
  const result = await submitCapture(credentials, {
    classification: currentClassification,
    imageCandidates: currentClassification.kind === "social_post" ? currentContext.imageCandidates : [],
    pageTitle: currentContext.title,
    selectionText: currentContext.selectionText,
    tags: parseTags(),
  });
  setBusy(false);

  if (result.state === "queued") {
    setMessage(`Queued ${labelForCaptureKind(result.kind).toLowerCase()} capture. Job ${result.jobId}.`, "success");
    return;
  }
  if (result.state === "duplicate") {
    const savedUrl = currentClassification.url ?? currentContext.url ?? "";
    const savedKind = currentClassification.kind === "invalid" ? "webpage" : currentClassification.kind;
    setSavedState({
      id: result.webSaveId ?? "duplicate",
      item_id: result.itemId ?? "duplicate",
      original_url: savedUrl,
      normalized_url: savedUrl,
      source_title: currentContext.title,
      source_domain: null,
      capture_kind: savedKind,
      user_tags: parseTags(),
      saved_at: new Date().toISOString(),
      archived_at: null,
      item: {
        id: result.itemId ?? "duplicate",
        title: currentContext.title,
        source_type: "webpage",
        status: "ready",
        summary: null,
        tags: parseTags(),
      },
    });
    setMessage(result.message, "success");
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
