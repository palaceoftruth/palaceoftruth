import { getCredentials } from "./shared/credentials.js";
import { grabImageBytesFromTab, decodeGrabbedImage } from "./shared/imageBytes.js";
import { uploadCaptureImage } from "./shared/palaceClient.js";

const SAVE_IMAGE_MENU_ID = "palace-save-image";

// Credentials and their API base URL live in device-local storage, never in
// `chrome.storage.sync`. See shared/credentials.ts.
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(["palaceApiBaseUrl"]).then((stored) => {
    if (typeof stored.palaceApiBaseUrl !== "string") {
      return chrome.storage.local.set({ palaceApiBaseUrl: "https://palaceoftruth.test" });
    }
    return undefined;
  });

  // removeAll first: onInstalled also fires on update, and create() on an id
  // that already exists throws.
  const menus = chrome.contextMenus;
  if (menus) {
    void menus.removeAll().then(() => {
      menus.create({
        id: SAVE_IMAGE_MENU_ID,
        title: "Save image to Palace",
        contexts: ["image"],
      });
    });
  }
});

/**
 * The popup is closed during a context-menu save, so the badge is the only
 * place feedback can go. It clears itself so a later click starts clean.
 */
async function flashBadge(text: string, color: string): Promise<void> {
  if (!chrome.action?.setBadgeText) return;
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
  setTimeout(() => {
    chrome.action.setBadgeText({ text: "" });
  }, 4000);
}

async function saveImageFromContextMenu(srcUrl: string, pageUrl: string | null, tabId: number): Promise<void> {
  const credentials = await getCredentials();
  if (!credentials) {
    await flashBadge("KEY", "#b45309");
    await chrome.runtime.openOptionsPage?.();
    return;
  }

  await flashBadge("...", "#334155");
  // Read the bytes in the tab: the image may be behind the session, and a URL
  // alone would leave Palace with nothing it can fetch.
  const grabbed = await grabImageBytesFromTab(tabId, srcUrl);
  if (!grabbed) {
    await flashBadge("ERR", "#b91c1c");
    return;
  }

  const result = await uploadCaptureImage(credentials, {
    bytes: decodeGrabbedImage(grabbed),
    mediaType: grabbed.mediaType,
    byteOrigin: "context_menu",
    imageUrl: srcUrl,
    sourceUrl: pageUrl,
    altText: grabbed.altText ?? null,
    width: grabbed.width ?? null,
    height: grabbed.height ?? null,
  });

  if (result.state === "queued" || result.state === "duplicate") {
    await flashBadge("OK", "#15803d");
    return;
  }
  await flashBadge("ERR", "#b91c1c");
  if (result.state === "auth_error") await chrome.runtime.openOptionsPage?.();
}

chrome.contextMenus?.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== SAVE_IMAGE_MENU_ID) return;
  if (!info.srcUrl || typeof tab?.id !== "number") return;
  void saveImageFromContextMenu(info.srcUrl, info.pageUrl ?? null, tab.id);
});
