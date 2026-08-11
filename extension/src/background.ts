// Credentials and their API base URL live in device-local storage, never in
// `chrome.storage.sync`. See shared/credentials.ts.
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(["palaceApiBaseUrl"]).then((stored) => {
    if (typeof stored.palaceApiBaseUrl !== "string") {
      return chrome.storage.local.set({ palaceApiBaseUrl: "https://palaceoftruth.test" });
    }
    return undefined;
  });
});
