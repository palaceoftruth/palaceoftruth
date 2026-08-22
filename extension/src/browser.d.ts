type ChromeStorageArea = {
  get(keys?: string[] | Record<string, unknown> | string | null): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
  remove(keys: string | string[]): Promise<void>;
};

type ChromeTab = {
  id?: number;
  url?: string;
  title?: string;
};

type ChromeContextMenuInfo = {
  menuItemId: string;
  srcUrl?: string;
  pageUrl?: string;
};

declare const chrome: {
  storage: {
    /** Device-local. Credentials live here - see shared/credentials.ts. */
    local: ChromeStorageArea;
    /** Replicated to Google's servers. Never store credentials here. */
    sync: ChromeStorageArea;
  };
  tabs: {
    query(queryInfo: { active?: boolean; currentWindow?: boolean }): Promise<ChromeTab[]>;
  };
  scripting: {
    /**
     * Chrome resolves a promise an injected function returns before handing
     * back the result, so the awaited type is what a caller actually reads.
     */
    executeScript<T, Args extends unknown[] = []>(options: {
      target: { tabId: number };
      func: (...args: Args) => T;
      args?: Args;
    }): Promise<Array<{ result?: Awaited<T> }>>;
  };
  permissions: {
    request(permissions: { origins: string[] }): Promise<boolean>;
  };
  action: {
    setBadgeText(details: { text: string; tabId?: number }): Promise<void>;
    setBadgeBackgroundColor(details: { color: string; tabId?: number }): Promise<void>;
  };
  contextMenus: {
    create(properties: {
      id: string;
      title: string;
      contexts: string[];
    }): void;
    removeAll(): Promise<void>;
    onClicked: {
      addListener(
        callback: (info: ChromeContextMenuInfo, tab?: ChromeTab) => void,
      ): void;
    };
  };
  runtime: {
    getManifest(): { version: string };
    openOptionsPage(): Promise<void>;
    onInstalled: {
      addListener(callback: () => void): void;
    };
  };
};
