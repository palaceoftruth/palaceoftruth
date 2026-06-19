chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.sync.get(["palaceApiBaseUrl"]).then((stored) => {
    if (typeof stored.palaceApiBaseUrl !== "string") {
      return chrome.storage.sync.set({ palaceApiBaseUrl: "https://palaceoftruth.test" });
    }
    return undefined;
  });
});
