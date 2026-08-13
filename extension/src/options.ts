import { clearCredentials, getCredentials, saveCredentials } from "./shared/credentials.js";
import { issueExtensionToken } from "./shared/palaceClient.js";

const form = document.querySelector<HTMLFormElement>("#settingsForm");
const apiBaseUrl = document.querySelector<HTMLInputElement>("#apiBaseUrl");
const apiKey = document.querySelector<HTMLInputElement>("#pairingApiKey");
const clearButton = document.querySelector<HTMLButtonElement>("#clearButton");
const message = document.querySelector<HTMLParagraphElement>("#message");

function setMessage(text: string, tone: "default" | "error" | "success" = "default"): void {
  if (!message) return;
  message.textContent = text;
  message.className = `message ${tone === "default" ? "" : tone}`.trim();
}

function palaceOriginPattern(value: string): string {
  const url = new URL(value);
  if (url.protocol !== "https:") {
    throw new Error("Palace URL must use HTTPS.");
  }
  return `${url.origin}/*`;
}

async function requestPalaceOrigin(value: string): Promise<void> {
  const origin = palaceOriginPattern(value);
  if (typeof chrome === "undefined" || !chrome.permissions?.request) return;
  const granted = await chrome.permissions.request({ origins: [origin] });
  if (!granted) throw new Error("Palace access was not granted.");
}

async function hydrate(): Promise<void> {
  const credentials = await getCredentials();
  if (apiBaseUrl) apiBaseUrl.value = credentials?.apiBaseUrl ?? "https://palaceoftruth.test";
  if (apiKey) apiKey.value = "";
  if (credentials?.accessToken) {
    const expires = credentials.expiresAt ? ` Expires ${new Date(credentials.expiresAt).toLocaleDateString()}.` : "";
    setMessage(`Capture token installed.${expires}`, "success");
  }
  if (typeof chrome === "undefined" || !chrome.storage?.sync) {
    setMessage("Settings render preview. Load as an extension to save credentials.");
  }
}

form?.addEventListener("submit", (event) => {
  event.preventDefault();
  const baseUrl = apiBaseUrl?.value.trim() ?? "";
  const key = apiKey?.value.trim() ?? "";
  if (!baseUrl || !key) {
    setMessage("Palace URL and one-time pairing key are required.", "error");
    return;
  }
  const version =
    typeof chrome !== "undefined" && chrome.runtime?.getManifest ? chrome.runtime.getManifest().version : "0.1.0";
  setMessage("Requesting scoped capture token...");
  void requestPalaceOrigin(baseUrl)
    .then(() => issueExtensionToken(baseUrl, key, version))
    .then((credentials) => saveCredentials(credentials))
    .then(() => {
      if (apiKey) apiKey.value = "";
      setMessage("Scoped capture token saved. The one-time pairing key was not stored.", "success");
    })
    .catch((error: unknown) => {
      setMessage(error instanceof Error ? error.message : "Unable to save settings.", "error");
    });
});

clearButton?.addEventListener("click", () => {
  void clearCredentials()
    .then(() => {
      if (apiKey) apiKey.value = "";
      setMessage("Stored capture token cleared.", "success");
    })
    .catch((error: unknown) => {
      setMessage(error instanceof Error ? error.message : "Unable to clear settings.", "error");
    });
});

void hydrate();
