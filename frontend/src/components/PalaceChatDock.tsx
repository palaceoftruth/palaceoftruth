import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import {
  ChevronDown,
  LoaderCircle,
  MessageSquareText,
  Send,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";

import { streamPalaceChat, type PalaceChatMeta } from "../api/client";
import ArtifactCitation from "./ArtifactCitation";
import { useToast } from "../context/ToastContext";
import type { ChatMessage, ChatSource } from "../api/types";

interface ConversationTurn {
  id: string;
  query: string;
  answer: string;
  sources: ChatSource[];
  meta: PalaceChatMeta | null;
  streaming: boolean;
  errored: boolean;
}

interface PalaceChatDockProps {
  /** When provided, the dock seeds the input with this query on mount. */
  initialQuery?: string;
  /** When true, the dock renders expanded on mount instead of collapsed. */
  defaultOpen?: boolean;
}

const STARTER_PROMPTS = [
  "What does the Palace know about this workspace?",
  "What's in the latest Palace run?",
  "Summarize the diary entries from the last week.",
  "Which items need a closer look before the next run?",
];

function genId() {
  return Math.random().toString(36).slice(2, 10);
}

function routeConfidenceLabel(confidence: PalaceChatMeta["route_confidence"]): string {
  switch (confidence) {
    case "high":
      return "High confidence";
    case "low":
      return "Low confidence (expanded)";
    default:
      return "Tenant-wide";
  }
}

export default function PalaceChatDock({
  initialQuery,
  defaultOpen = false,
}: PalaceChatDockProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [input, setInput] = useState(initialQuery ?? "");
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const toast = useToast();

  useEffect(() => {
    if (open) {
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  useEffect(() => {
    if (turns.length === 0) return;
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [turns]);

  const sendQuery = useCallback(async () => {
    const text = input.trim();
    if (!text || loading) return;

    const turnId = genId();
    setTurns((prev) => [
      ...prev,
      { id: turnId, query: text, answer: "", sources: [], meta: null, streaming: true, errored: false },
    ]);
    setInput("");
    setLoading(true);

    let answer = "";

    await streamPalaceChat(
      text,
      history,
      (token) => {
        answer += token;
        setTurns((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.id === turnId) {
            next[next.length - 1] = { ...last, answer };
          }
          return next;
        });
      },
      () => {
        setTurns((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.id === turnId) {
            next[next.length - 1] = { ...last, streaming: false };
          }
          return next;
        });
        setHistory((prev) => [...prev, { role: "user", content: text }, { role: "assistant", content: answer }]);
        setLoading(false);
      },
      (err) => {
        toast.error(err.message);
        setTurns((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.id === turnId) {
            next[next.length - 1] = { ...last, streaming: false, errored: true, answer: answer || err.message };
          }
          return next;
        });
        setLoading(false);
      },
      (sources) => {
        setTurns((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.id === turnId) {
            next[next.length - 1] = { ...last, sources };
          }
          return next;
        });
      },
      (meta) => {
        setTurns((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.id === turnId) {
            next[next.length - 1] = { ...last, meta };
          }
          return next;
        });
      },
    );
  }, [history, input, loading, toast]);

  const clearConversation = useCallback(() => {
    setTurns([]);
    setHistory([]);
    setInput("");
  }, []);

  const primePrompt = useCallback((prompt: string) => {
    setInput(prompt);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="sb-button-primary fixed bottom-6 right-6 z-40 px-4 py-3 shadow-[0_18px_60px_rgba(2,6,23,0.55)]"
        aria-label="Open Palace quick chat"
      >
        <Sparkles className="h-4 w-4" />
        Quick Palace chat
      </button>
    );
  }

  return (
    <div className="fixed bottom-6 right-6 z-40 flex w-[min(420px,calc(100vw-3rem))] flex-col overflow-hidden rounded-[24px] border border-zinc-800/80 bg-zinc-950/90 shadow-[0_24px_80px_rgba(2,6,23,0.6)] backdrop-blur-xl">
      <header className="flex items-center justify-between gap-2 border-b border-zinc-800/80 bg-zinc-950/85 px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="rounded-full border border-sky-700/50 bg-sky-950/40 p-1.5">
            <MessageSquareText className="h-3.5 w-3.5 text-sky-200" />
          </div>
          <div>
            <p className="text-xs font-semibold text-zinc-100">Quick Palace chat</p>
            <p className="text-[10px] uppercase tracking-[0.18em] text-zinc-500">Tenant-wide retrieval</p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={clearConversation}
            disabled={turns.length === 0 && !input}
            className="sb-button-ghost px-2 py-1 text-xs text-zinc-500 hover:text-rose-200"
            aria-label="Clear conversation"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="sb-button-ghost px-2 py-1 text-zinc-400 hover:text-white"
            aria-label="Minimize Palace chat"
          >
            <ChevronDown className="h-4 w-4" />
          </button>
        </div>
      </header>

      <div ref={scrollRef} className="max-h-[55vh] min-h-[180px] flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {turns.length === 0 ? (
          <div className="space-y-2 rounded-[18px] border border-dashed border-zinc-800/80 bg-zinc-900/40 px-3 py-3 text-xs text-zinc-400">
            <p className="text-zinc-300">Ask anything about the tenant — rooms, items, runs, diary entries.</p>
            <div className="flex flex-wrap gap-1.5">
              {STARTER_PROMPTS.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  onClick={() => primePrompt(prompt)}
                  className="sb-chip sb-chip-inactive cursor-pointer text-[11px]"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {turns.map((turn) => (
          <div key={turn.id} className="space-y-2">
            <div className="flex justify-end">
              <div className="max-w-[88%] rounded-[18px] rounded-br-md border border-sky-700/40 bg-sky-950/50 px-3 py-2 text-xs text-sky-50">
                {turn.query}
              </div>
            </div>
            <div className="flex justify-start">
              <div className="max-w-[92%] space-y-2 rounded-[18px] rounded-bl-md border border-zinc-800 bg-zinc-900/70 px-3 py-2 text-xs text-zinc-100">
                {turn.meta ? (
                  <p className="text-[10px] uppercase tracking-[0.18em] text-zinc-500">
                    {routeConfidenceLabel(turn.meta.route_confidence)}
                    {turn.meta.selected_wing ? ` · ${turn.meta.selected_wing}` : ""}
                    {turn.meta.result_count ? ` · ${turn.meta.result_count} source${turn.meta.result_count === 1 ? "" : "s"}` : ""}
                  </p>
                ) : null}
                {turn.answer ? (
                  <div className="prose prose-xs prose-invert max-w-none">
                    <ReactMarkdown>{turn.answer}</ReactMarkdown>
                    {turn.streaming ? <span className="animate-pulse">▋</span> : null}
                  </div>
                ) : turn.streaming ? (
                  <LoaderCircle className="h-3.5 w-3.5 animate-spin text-zinc-400" />
                ) : turn.errored ? (
                  <p className="text-rose-300">The Palace could not answer that.</p>
                ) : null}

                {turn.meta?.completeness_warning ? (
                  <p className="rounded-md border border-amber-700/40 bg-amber-950/30 px-2 py-1 text-[10px] text-amber-200">
                    {turn.meta.completeness_warning}
                  </p>
                ) : null}

                {turn.sources.length ? (
                  <details className="border-t border-zinc-700/80 pt-1.5">
                    <summary className="cursor-pointer text-[10px] uppercase tracking-[0.16em] text-zinc-400 transition hover:text-zinc-200">
                      {turn.sources.length} source{turn.sources.length === 1 ? "" : "s"}
                    </summary>
                    <div className="mt-1.5 space-y-1.5">
                      {turn.sources.map((src) => (
                        <div
                          key={src.item_id}
                          className="rounded-md bg-zinc-950/70 px-2 py-1 text-[11px] text-zinc-400"
                        >
                          <p className="mb-0.5 font-medium text-zinc-300">{src.title}</p>
                          <p className="line-clamp-2 italic">{src.chunk_text}</p>
                          <ArtifactCitation citation={src.artifact_citation} compact />
                        </div>
                      ))}
                    </div>
                  </details>
                ) : null}
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="border-t border-zinc-800/80 bg-zinc-950/85 p-3">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void sendQuery();
              }
            }}
            placeholder="Ask the Palace anything..."
            rows={2}
            disabled={loading}
            className="sb-textarea min-h-[60px] flex-1 resize-none text-xs"
          />
          <button
            type="button"
            onClick={() => void sendQuery()}
            disabled={!input.trim() || loading}
            className="sb-button-primary shrink-0 px-3 py-2"
            aria-label="Send Palace chat query"
          >
            <Send className="h-3.5 w-3.5" />
          </button>
        </div>
        <p className="mt-1.5 flex items-center justify-between text-[10px] text-zinc-500">
          <span>Retrieves across the whole tenant. Cite-grounded answers only.</span>
          {loading ? <LoaderCircle className="h-3 w-3 animate-spin" /> : null}
        </p>
      </div>
    </div>
  );
}
