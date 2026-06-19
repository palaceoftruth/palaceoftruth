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

declare const chrome: {
  storage: {
    sync: ChromeStorageArea;
  };
  tabs: {
    query(queryInfo: { active?: boolean; currentWindow?: boolean }): Promise<ChromeTab[]>;
  };
  scripting: {
    executeScript<T, Args extends unknown[] = []>(options: {
      target: { tabId: number };
      func: (...args: Args) => T;
      args?: Args;
    }): Promise<Array<{ result?: T }>>;
  };
  runtime: {
    getManifest(): { version: string };
    openOptionsPage(): Promise<void>;
    onInstalled: {
      addListener(callback: () => void): void;
    };
  };
};
