import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export type ToastType = "success" | "error" | "info";
const TOAST_LIMIT = 3;
const TOAST_TTL_MS: Record<ToastType, number> = {
  success: 3000,
  info: 3000,
  error: 6000,
};

interface Toast {
  id: string;
  message: string;
  type: ToastType;
  count: number;
}

interface ToastContextValue {
  toast: {
    success: (msg: string) => void;
    error: (msg: string) => void;
    info: (msg: string) => void;
  };
}

const ToastContext = createContext<ToastContextValue>(null!);

function ToastContainer({
  toasts,
  onDismiss,
}: {
  toasts: Toast[];
  onDismiss: (id: string) => void;
}) {
  return (
    <div className="fixed bottom-4 right-4 z-50 flex max-w-sm flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`flex min-w-72 max-w-sm items-start justify-between gap-3 rounded-2xl border px-4 py-3 text-sm shadow-xl backdrop-blur ${
            t.type === "success"
              ? "border-emerald-700/40 bg-emerald-950/90 text-emerald-50"
              : t.type === "error"
                ? "border-rose-700/40 bg-rose-950/92 text-rose-50"
                : "border-zinc-700 bg-zinc-900/95 text-zinc-100"
          }`}
        >
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <p className="text-[11px] font-semibold uppercase tracking-[0.22em] opacity-70">
                {t.type === "error" ? "Issue" : t.type}
              </p>
              {t.count > 1 ? (
                <span className="rounded-full border border-current/20 px-1.5 py-0.5 text-[10px] font-medium opacity-70">
                  ×{t.count}
                </span>
              ) : null}
            </div>
            <p className="mt-1 break-words leading-5">{t.message}</p>
          </div>
          <button
            onClick={() => onDismiss(t.id)}
            className="shrink-0 rounded-full p-1 text-base leading-none opacity-60 transition hover:bg-black/10 hover:opacity-100"
            aria-label="Dismiss"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timersRef = useRef<Map<string, number>>(new Map());

  const dismiss = useCallback((id: string) => {
    const timer = timersRef.current.get(id);
    if (timer) {
      window.clearTimeout(timer);
      timersRef.current.delete(id);
    }
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const scheduleDismiss = useCallback((id: string, type: ToastType) => {
    const existing = timersRef.current.get(id);
    if (existing) {
      window.clearTimeout(existing);
    }
    const timer = window.setTimeout(() => dismiss(id), TOAST_TTL_MS[type]);
    timersRef.current.set(id, timer);
  }, [dismiss]);

  const add = useCallback((message: string, type: ToastType) => {
    const normalizedMessage = message.trim();
    if (!normalizedMessage) {
      return;
    }

    let toastId: string = crypto.randomUUID();
    setToasts((prev) => {
      const existing = prev.find((toast) => toast.type === type && toast.message === normalizedMessage);
      if (existing) {
        toastId = existing.id;
        return prev.map((toast) =>
          toast.id === existing.id
            ? { ...toast, count: toast.count + 1 }
            : toast,
        );
      }

      return [...prev, { id: toastId, message: normalizedMessage, type, count: 1 }].slice(-TOAST_LIMIT);
    });
    scheduleDismiss(toastId, type);
  }, [scheduleDismiss]);

  useEffect(() => {
    return () => {
      for (const timer of timersRef.current.values()) {
        window.clearTimeout(timer);
      }
      timersRef.current.clear();
    };
  }, []);

  const toast = {
    success: (msg: string) => add(msg, "success"),
    error: (msg: string) => add(msg, "error"),
    info: (msg: string) => add(msg, "info"),
  };

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      {createPortal(
        <ToastContainer toasts={toasts} onDismiss={dismiss} />,
        document.body,
      )}
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext).toast;
