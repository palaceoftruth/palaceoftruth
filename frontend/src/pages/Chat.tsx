import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { LoaderCircle, MessageSquare, PencilLine, Plus, Send, Trash2 } from "lucide-react";

import { api, streamChat } from "../api/client";
import ArtifactCitation from "../components/ArtifactCitation";
import PageHeader from "../components/PageHeader";
import { useToast } from "../context/ToastContext";
import type { ChatMessage, ChatSource, ConversationSummary } from "../api/types";

interface DisplayMessage {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
  streaming?: boolean;
}

function buildConversationTitle(text: string) {
  const normalized = text.trim().replace(/\s+/g, " ");
  if (!normalized) {
    return "New conversation";
  }
  return normalized.length > 56 ? `${normalized.slice(0, 56).trimEnd()}...` : normalized;
}

export default function Chat() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingConversations, setLoadingConversations] = useState(true);
  const [loadingConversationBody, setLoadingConversationBody] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const starterPrompts = [
    "What changed in my library recently?",
    "What does Palace know about this workspace?",
    "Summarize the newest captures for me.",
    "What context should a fresh agent read first?",
  ];

  useEffect(() => {
    if (messages.length === 0) {
      return;
    }
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    let cancelled = false;

    async function loadConversations() {
      setLoadingConversations(true);
      try {
        const data = await api.listConversations();
        if (cancelled) {
          return;
        }
        setConversations(data);
        setActiveConversationId((current) => current ?? data[0]?.id ?? null);
      } catch (error) {
        if (!cancelled) {
          toast.error(error instanceof Error ? error.message : "Failed to load conversations");
        }
      } finally {
        if (!cancelled) {
          setLoadingConversations(false);
        }
      }
    }

    void loadConversations();

    return () => {
      cancelled = true;
    };
  }, [toast]);

  useEffect(() => {
    let cancelled = false;

    async function loadConversationMessages() {
      if (!activeConversationId) {
        setMessages([]);
        return;
      }

      setLoadingConversationBody(true);
      try {
        const conversation = await api.getConversation(activeConversationId);
        if (cancelled) {
          return;
        }
        setMessages(
          conversation.messages.map((message) => ({
            role: message.role,
            content: message.content,
          })),
        );
      } catch (error) {
        if (!cancelled) {
          toast.error(error instanceof Error ? error.message : "Failed to load conversation");
        }
      } finally {
        if (!cancelled) {
          setLoadingConversationBody(false);
        }
      }
    }

    void loadConversationMessages();

    return () => {
      cancelled = true;
    };
  }, [activeConversationId, toast]);

  const upsertConversation = (conversation: ConversationSummary) => {
    setConversations((prev) => {
      const next = prev.filter((item) => item.id !== conversation.id);
      return [conversation, ...next];
    });
  };

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || loading || loadingConversationBody) {
      return;
    }

    const priorMessages = messages;
    const previousConversationId = activeConversationId;
    let workingConversationId = activeConversationId;
    let workingConversation = conversations.find((conversation) => conversation.id === workingConversationId) ?? null;
    let createdConversation = false;

    if (!workingConversationId) {
      try {
        workingConversation = await api.createConversation({ title: buildConversationTitle(text) });
        workingConversationId = workingConversation.id;
        createdConversation = true;
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Failed to create conversation");
        return;
      }
    }

    const history: ChatMessage[] = [
      ...priorMessages.map((message) => ({ role: message.role, content: message.content })),
      { role: "user", content: text },
    ];

    setMessages((prev) => [
      ...prev,
      { role: "user", content: text },
      { role: "assistant", content: "", streaming: true },
    ]);
    setInput("");
    setLoading(true);

    let assistantContent = "";

    await streamChat(
      history,
      { conversationId: workingConversationId },
      (token) => {
        assistantContent += token;
        setMessages((prev) => {
          const next = [...prev];
          next[next.length - 1] = {
            role: "assistant",
            content: assistantContent,
            streaming: true,
          };
          return next;
        });
      },
      () => {
        setMessages((prev) => {
          const next = [...prev];
          next[next.length - 1] = {
            ...next[next.length - 1],
            streaming: false,
          };
          return next;
        });
        if (workingConversationId) {
          const now = new Date().toISOString();
          upsertConversation({
            id: workingConversationId,
            title: workingConversation?.title ?? buildConversationTitle(text),
            created_at: workingConversation?.created_at ?? now,
            updated_at: now,
          });
          if (createdConversation) {
            setActiveConversationId(workingConversationId);
          }
        }
        setLoading(false);
      },
      async (err) => {
        toast.error(err.message);
        setMessages((prev) => prev.slice(0, Math.max(prev.length - 2, 0)));
        if (createdConversation && workingConversationId) {
          try {
            await api.deleteConversation(workingConversationId);
          } catch {
            // Best-effort cleanup if the initial stream failed before any content persisted.
          }
          setConversations((prev) => prev.filter((conversation) => conversation.id !== workingConversationId));
          setActiveConversationId(previousConversationId);
        }
        setLoading(false);
      },
      (sources) => {
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.role === "assistant") {
            next[next.length - 1] = { ...last, sources };
          }
          return next;
        });
      },
    );
  };

  const primePrompt = (prompt: string) => {
    setInput(prompt);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const startConversation = () => {
    if (loading) {
      return;
    }
    setActiveConversationId(null);
    setMessages([]);
    setInput("");
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const removeConversation = async (conversationId: string) => {
    if (loading) {
      return;
    }

    try {
      await api.deleteConversation(conversationId);
      const remaining = conversations.filter((conversation) => conversation.id !== conversationId);
      setConversations(remaining);
      if (activeConversationId === conversationId) {
        setActiveConversationId(remaining[0]?.id ?? null);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to delete conversation");
    }
  };

  const activeConversationTitle =
    conversations.find((conversation) => conversation.id === activeConversationId)?.title ?? "New conversation";

  return (
    <div className="sb-page flex min-h-[calc(100vh-8rem)] flex-col">
      <PageHeader
        eyebrow="Conversation"
        title="Chat with the workspace"
        description="Ask for summaries, recover project context, or keep a durable thread that another operator can resume later."
      />

      <div className="flex flex-1 flex-col gap-4 xl:flex-row">
        <aside className="sb-panel flex w-full flex-col overflow-hidden xl:min-h-0 xl:w-[320px] xl:flex-none">
          <div className="flex items-center justify-between gap-3 border-b border-zinc-800/80 px-5 py-4 md:px-6">
            <div>
              <p className="sb-section-title">Threads</p>
              <p className="mt-1 text-xs text-zinc-500">Workspace-scoped conversation history.</p>
            </div>
            <button type="button" onClick={startConversation} className="sb-button-secondary px-3" disabled={loading}>
              <Plus className="h-4 w-4" />
            </button>
          </div>

          <div className="flex-1 space-y-2 overflow-y-auto px-3 py-3">
            <button
              type="button"
              onClick={startConversation}
              disabled={loading}
              className={`w-full rounded-[24px] border px-4 py-3 text-left transition ${
                activeConversationId === null
                  ? "border-sky-600/50 bg-sky-950/30 text-zinc-100"
                  : "border-zinc-800/80 bg-zinc-950/40 text-zinc-300 hover:border-zinc-700 hover:text-zinc-100"
              }`}
            >
              <div className="flex items-center gap-3">
                <div className="rounded-full border border-zinc-800 bg-zinc-950/80 p-2">
                  <PencilLine className="h-4 w-4" />
                </div>
                <div>
                  <p className="text-sm font-medium">New conversation</p>
                  <p className="mt-1 text-xs text-zinc-500">The first sent message creates a durable thread.</p>
                </div>
              </div>
            </button>

            {loadingConversations ? (
              <div className="flex items-center gap-2 rounded-[24px] border border-zinc-800/80 bg-zinc-950/30 px-4 py-3 text-sm text-zinc-400">
                <LoaderCircle className="h-4 w-4 animate-spin" />
                Loading saved conversations...
              </div>
            ) : null}

            {!loadingConversations && conversations.length === 0 ? (
              <div className="rounded-[24px] border border-dashed border-zinc-800/80 bg-zinc-950/20 px-4 py-5 text-sm text-zinc-500">
                No saved threads yet. Send a message to create the first one.
              </div>
            ) : null}

            {conversations.map((conversation) => {
              const isActive = conversation.id === activeConversationId;
              return (
                <div
                  key={conversation.id}
                  className={`rounded-[24px] border transition ${
                    isActive
                      ? "border-sky-600/50 bg-sky-950/30"
                      : "border-zinc-800/80 bg-zinc-950/30 hover:border-zinc-700"
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => setActiveConversationId(conversation.id)}
                    disabled={loading}
                    className="w-full px-4 py-3 text-left"
                  >
                    <p className={`line-clamp-2 text-sm font-medium ${isActive ? "text-sky-50" : "text-zinc-100"}`}>
                      {conversation.title}
                    </p>
                    <p className="mt-1 text-xs text-zinc-500">
                      Updated {new Date(conversation.updated_at).toLocaleString()}
                    </p>
                  </button>
                  <div className="flex items-center justify-end px-3 pb-3">
                    <button
                      type="button"
                      onClick={() => removeConversation(conversation.id)}
                      disabled={loading}
                      className="sb-button-ghost px-2 py-1 text-zinc-500 hover:text-rose-200"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </aside>

        <section className="sb-panel flex min-h-0 flex-1 flex-col overflow-hidden">
          <div className="flex items-center justify-between gap-3 border-b border-zinc-800/80 px-5 py-4 md:px-6">
            <div>
              <p className="sb-section-title">Thread</p>
              <h2 className="mt-1 text-lg font-semibold tracking-tight text-zinc-100">{activeConversationTitle}</h2>
            </div>
            <div className="inline-flex items-center rounded-full border border-zinc-800/80 bg-zinc-950/60 px-3 py-1 text-xs text-zinc-400">
              {activeConversationId ? "Persisted" : "Draft"}
            </div>
          </div>

          <div className="flex-1 space-y-4 overflow-y-auto px-5 py-5 md:px-6">
            {loadingConversationBody ? (
              <div className="flex min-h-[240px] items-center justify-center rounded-[28px] border border-zinc-800/80 bg-zinc-950/25 text-sm text-zinc-400">
                <div className="flex items-center gap-2">
                  <LoaderCircle className="h-4 w-4 animate-spin" />
                  Loading conversation history...
                </div>
              </div>
            ) : null}

            {!loadingConversationBody && messages.length === 0 ? (
              <div className="rounded-[28px] border border-zinc-800/80 bg-zinc-900/45 px-6 py-8 md:px-8 md:py-10">
                <div className="mx-auto max-w-2xl text-center">
                  <div className="inline-flex h-14 w-14 items-center justify-center rounded-3xl border border-zinc-800 bg-zinc-950/80">
                    <MessageSquare className="h-6 w-6 text-zinc-200" />
                  </div>
                  <h2 className="mt-5 text-2xl font-semibold tracking-tight text-zinc-100">
                    Start a conversation with your library.
                  </h2>
                  <p className="mt-3 text-sm leading-7 text-zinc-400">
                    Ask for summaries, recover project context, or check what a freshly moved agent should read first.
                  </p>
                  <div className="mt-6 flex flex-wrap justify-center gap-2">
                    {starterPrompts.map((prompt) => (
                      <button
                        key={prompt}
                        type="button"
                        onClick={() => primePrompt(prompt)}
                        className="sb-chip sb-chip-inactive cursor-pointer"
                      >
                        {prompt}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            ) : null}

            {!loadingConversationBody &&
              messages.map((message, index) => (
                <div key={index} className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}>
                  <div
                    className={`max-w-[88%] rounded-[24px] px-4 py-3 text-sm shadow-[0_10px_30px_rgba(2,6,23,0.18)] ${
                      message.role === "user"
                        ? "rounded-br-md border border-sky-700/40 bg-sky-950/50 text-sky-50"
                        : "rounded-bl-md border border-zinc-800 bg-zinc-900/70 text-zinc-100"
                    }`}
                  >
                    {message.role === "assistant" ? (
                      <div className="prose prose-sm prose-invert max-w-none">
                        <ReactMarkdown>{message.content}</ReactMarkdown>
                        {message.streaming ? <span className="animate-pulse">▋</span> : null}
                      </div>
                    ) : (
                      <p>{message.content}</p>
                    )}

                    {message.sources?.length ? (
                      <details className="mt-3 border-t border-zinc-700/80 pt-2">
                        <summary className="cursor-pointer text-xs text-zinc-400 transition hover:text-zinc-200">
                          {message.sources.length} source{message.sources.length !== 1 ? "s" : ""}
                        </summary>
                        <div className="mt-2 space-y-2">
                          {message.sources.map((source) => (
                            <div key={source.item_id} className="rounded-2xl bg-zinc-950/70 p-2 text-xs text-zinc-400">
                              <p className="mb-0.5 font-medium text-zinc-300">{source.title}</p>
                              <p className="line-clamp-2 italic">{source.chunk_text}</p>
                              <ArtifactCitation citation={source.artifact_citation} compact />
                            </div>
                          ))}
                        </div>
                      </details>
                    ) : null}
                  </div>
                </div>
              ))}

            {loading && messages[messages.length - 1]?.streaming === false ? (
              <div className="flex justify-start">
                <div className="rounded-[24px] rounded-bl-md border border-zinc-800 bg-zinc-900/70 px-4 py-3">
                  <div className="flex gap-1">
                    {[0, 1, 2].map((index) => (
                      <div
                        key={index}
                        className="h-1.5 w-1.5 animate-bounce rounded-full bg-zinc-500"
                        style={{ animationDelay: `${index * 150}ms` }}
                      />
                    ))}
                  </div>
                </div>
              </div>
            ) : null}

            <div ref={bottomRef} />
          </div>
        </section>
      </div>

      <section className="sb-panel-muted mt-4 p-3">
        <div className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && sendMessage()}
            placeholder="Ask about projects, people, recent captures, or leave a thread for the next operator..."
            disabled={loading || loadingConversationBody}
            className="sb-input min-w-0 flex-1 border-zinc-800 bg-zinc-950/90"
          />
          <button
            onClick={sendMessage}
            disabled={!input.trim() || loading || loadingConversationBody}
            className="sb-button-primary shrink-0"
          >
            <Send className="h-4 w-4" />
          </button>
        </div>
      </section>
    </div>
  );
}
