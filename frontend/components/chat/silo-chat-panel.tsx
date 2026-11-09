"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import {
  Check, Loader2, MessageCircle, PauseCircle, Plus, Send, WifiOff, Wrench, X,
} from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, ApiError, streamChat, type ChatApproval, type ChatContext } from "@/lib/api";
import { MarkdownText } from "@/lib/markdown";
import { cn } from "@/lib/utils";

// ------------------------------------------------------------------ types
interface ToolCall { id: string; name: string; label: string; status: "running" | "done" | "error" }
interface ApprovalCard extends ChatApproval { state: "pending" | "busy" | "approve" | "hold"; error?: string }
interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  text: string;
  tools: ToolCall[];
  approvals: ApprovalCard[];
  streaming?: boolean;
  error?: boolean;
}
interface ThreadSummary { id: string; title: string }

let nextId = 1;
const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

// The chat history endpoints' payloads are owned by the backend; normalise
// defensively (array or wrapped) without trusting any single shape.
function asRecord(v: unknown): Record<string, unknown> | null {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : null;
}
function asList(v: unknown): unknown[] {
  if (Array.isArray(v)) return v;
  const rec = asRecord(v);
  if (rec) {
    for (const key of ["threads", "messages", "items", "results"]) {
      if (Array.isArray(rec[key])) return rec[key] as unknown[];
    }
  }
  return [];
}
function normaliseThreads(raw: unknown): ThreadSummary[] {
  return asList(raw).flatMap((item) => {
    const rec = asRecord(item);
    if (!rec) return [];
    const id = rec.id ?? rec.thread_id;
    if (typeof id !== "string" || !id) return [];
    const title = rec.title ?? rec.name ?? rec.preview;
    return [{ id, title: typeof title === "string" && title ? title : "Conversation" }];
  });
}
function normaliseMessages(raw: unknown): ChatMessage[] {
  return asList(raw).flatMap((item) => {
    const rec = asRecord(item);
    if (!rec) return [];
    const roleRaw = rec.role ?? rec.sender;
    const role = roleRaw === "user" || roleRaw === "human" ? "user" : "assistant";
    const textRaw = rec.content ?? rec.text ?? rec.message;
    if (typeof textRaw !== "string" || !textRaw) return [];
    return [{ id: nextId++, role: role as "user" | "assistant", text: textRaw, tools: [], approvals: [] }];
  });
}

// ------------------------------------------------------------------ panel
export function SiloChatPanel() {
  const pathname = usePathname() ?? "/dashboard";
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [offline, setOffline] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [contextName, setContextName] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Context from the route: /dashboard/employers/{id} or /dashboard/runs/{id}
  const segments = pathname.split("/").filter(Boolean);
  const employerId = segments[1] === "employers" && segments[2] && segments[2] !== "new" ? segments[2] : undefined;
  const runId = segments[1] === "runs" && segments[2] ? segments[2] : undefined;
  const context: ChatContext = { page: pathname, ...(employerId ? { employer_id: employerId } : {}), ...(runId ? { run_id: runId } : {}) };

  useEffect(() => {
    let alive = true;
    setContextName(null);
    (async () => {
      try {
        if (employerId) {
          const emp = await api.employer(employerId);
          if (alive) setContextName(emp.name);
        } else if (runId) {
          const run = await api.run(runId);
          if (alive) setContextName(`${run.employer_name} · run`);
        }
      } catch {
        // The chip simply falls back to a generic label.
      }
    })();
    return () => {
      alive = false;
    };
  }, [employerId, runId]);

  const loadHistory = useCallback(async () => {
    try {
      const list = normaliseThreads(await api.chatThreads());
      setThreads(list);
      setOffline(false);
      if (list.length > 0) {
        const latest = list[0];
        setThreadId(latest.id);
        const history = normaliseMessages(await api.chatMessages(latest.id));
        setMessages(history);
      }
    } catch {
      setOffline(true);
    }
    setHistoryLoaded(true);
  }, []);

  useEffect(() => {
    if (open && !historyLoaded) void loadHistory();
  }, [open, historyLoaded, loadHistory]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, open]);

  async function openThread(id: string) {
    setThreadId(id);
    try {
      setMessages(normaliseMessages(await api.chatMessages(id)));
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }

  function newChat() {
    abortRef.current?.abort();
    setThreadId(null);
    setMessages([]);
    setSending(false);
  }

  function patchMessage(id: number, patch: (m: ChatMessage) => ChatMessage) {
    setMessages((prev) => prev.map((m) => (m.id === id ? patch(m) : m)));
  }

  async function send(ev?: React.FormEvent) {
    ev?.preventDefault();
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setOffline(false);
    const userMsg: ChatMessage = { id: nextId++, role: "user", text, tools: [], approvals: [] };
    const assistantId = nextId++;
    const assistantMsg: ChatMessage = { id: assistantId, role: "assistant", text: "", tools: [], approvals: [], streaming: true };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setSending(true);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamChat(
        { ...(threadId ? { thread_id: threadId } : {}), message: text, context },
        (event) => {
          if (event.type === "token") {
            patchMessage(assistantId, (m) => ({ ...m, text: m.text + event.text }));
          } else if (event.type === "tool") {
            patchMessage(assistantId, (m) => {
              const status = event.status === "error" ? "error" : event.status === "running" || event.status === "started" ? "running" : "done";
              const existing = m.tools.find((t) => t.name === event.name && t.label === event.label);
              if (existing) {
                return { ...m, tools: m.tools.map((t) => (t === existing ? { ...t, status } : t)) };
              }
              return { ...m, tools: [...m.tools, { id: `${event.name}-${m.tools.length}`, name: event.name, label: event.label, status }] };
            });
          } else if (event.type === "approval") {
            patchMessage(assistantId, (m) => ({
              ...m,
              approvals: [...m.approvals, { ...event.approval, state: "pending" }],
            }));
          } else if (event.type === "done") {
            if (event.thread_id) {
              setThreadId(event.thread_id);
              setThreads((prev) =>
                prev.some((t) => t.id === event.thread_id) ? prev : [{ id: event.thread_id, title: text.slice(0, 60) }, ...prev],
              );
            }
          } else if (event.type === "error") {
            patchMessage(assistantId, (m) => ({ ...m, error: true, text: m.text || event.message }));
          }
        },
        controller.signal,
      );
      patchMessage(assistantId, (m) => ({
        ...m,
        streaming: false,
        tools: m.tools.map((t) => (t.status === "running" ? { ...t, status: "done" as const } : t)),
        text: m.text || (m.tools.length === 0 && m.approvals.length === 0 ? "Done." : m.text),
      }));
    } catch (err) {
      if ((err as { name?: string })?.name === "AbortError") return;
      const unreachable = err instanceof ApiError && (err.status === 0 || err.status === 404);
      if (unreachable) setOffline(true);
      patchMessage(assistantId, (m) => ({
        ...m,
        streaming: false,
        error: true,
        text: m.text || (unreachable ? "Silo chat is not available right now." : errMsg(err)),
      }));
    } finally {
      setSending(false);
    }
  }

  async function decideApproval(msgId: number, card: ApprovalCard, decision: "approve" | "hold") {
    patchMessage(msgId, (m) => ({
      ...m,
      approvals: m.approvals.map((a) => (a === card ? { ...a, state: "busy" as const, error: undefined } : a)),
    }));
    try {
      // The human clicks; the decision goes through the same endpoint as the run panel.
      await api.decide(card.run_id, decision);
      patchMessage(msgId, (m) => ({
        ...m,
        approvals: m.approvals.map((a) => (a.run_id === card.run_id ? { ...a, state: decision } : a)),
      }));
    } catch (err) {
      patchMessage(msgId, (m) => ({
        ...m,
        approvals: m.approvals.map((a) =>
          a.run_id === card.run_id ? { ...a, state: "pending" as const, error: errMsg(err) } : a,
        ),
      }));
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Open Silo chat"
        className="fixed bottom-5 right-5 z-50 flex items-center gap-2 rounded-full border-2 border-sage-600/60 bg-ink-800 py-2 pl-2 pr-4 shadow-2xl transition-transform hover:scale-[1.03]"
      >
        <span className="grid size-10 place-items-center overflow-hidden rounded-full bg-sand-200">
          <LottieAnimation name="silo-wave" className="size-10" label="Silo" />
        </span>
        <span className="flex items-center gap-1.5 font-extrabold text-sand-100">
          <MessageCircle className="size-4 text-sage-300" /> Ask Silo
        </span>
      </button>
    );
  }

  return (
    <aside
      aria-label="Silo chat"
      className="fixed inset-0 z-50 flex flex-col border-ink-700 bg-ink-900 shadow-2xl sm:inset-y-0 sm:left-auto sm:right-0 sm:w-[400px] sm:max-w-full sm:border-l-2"
    >
      <header className="flex items-center gap-3 border-b-2 border-ink-700 px-4 py-3">
        <span className="grid size-11 shrink-0 place-items-center overflow-hidden rounded-full bg-sand-200">
          <LottieAnimation name="silo-idle" className="size-11" label="Silo" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-display text-lg font-semibold leading-tight text-sand-100">Silo</p>
          <p className="truncate text-xs font-semibold text-sage-400">Your payroll agent for this workspace</p>
        </div>
        <Button type="button" variant="ghost" size="sm" onClick={newChat}>
          <Plus /> New chat
        </Button>
        <button type="button" onClick={() => setOpen(false)} aria-label="Close Silo chat" className="rounded-full p-2 text-sage-300 hover:bg-ink-700 hover:text-sand-100">
          <X className="size-5" />
        </button>
      </header>

      <div className="flex flex-wrap items-center gap-2 border-b-2 border-ink-700 px-4 py-2.5">
        <span className="text-xs font-extrabold uppercase tracking-widest text-sage-500">Looking at:</span>
        <Badge tone="sage">{contextName ?? (employerId || runId ? "This page" : "All employers")}</Badge>
        {threads.length > 1 && (
          <label className="ml-auto flex items-center gap-1 text-xs font-bold text-sage-400">
            History
            <select
              value={threadId ?? ""}
              onChange={(e) => void openThread(e.target.value)}
              className="max-w-36 rounded-lg border border-ink-600 bg-ink-800 px-1.5 py-1 text-xs font-semibold text-sand-100"
              aria-label="Open a previous conversation"
            >
              {threads.map((t) => (
                <option key={t.id} value={t.id}>{t.title}</option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {offline ? (
          <div className="flex flex-col items-center gap-2 py-10 text-center" role="status">
            <WifiOff className="size-8 text-sage-500" />
            <p className="font-bold text-sand-100">Silo chat is offline</p>
            <p className="max-w-64 text-sm text-sage-400">
              The chat service is not reachable yet. The rest of the dashboard works as normal.
            </p>
            <Button type="button" variant="outline" size="sm" onClick={() => { setHistoryLoaded(false); void loadHistory(); }}>
              Try again
            </Button>
          </div>
        ) : messages.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <LottieAnimation name="silo-wave" className="size-24" label="Silo waving" />
            <p className="font-bold text-sand-100">Hi, I am Silo.</p>
            <p className="max-w-64 text-sm text-sage-400">
              Ask me to check a balance, explain a run, or start payroll — for example, “Run payroll for {contextName ?? "an employer"}”.
            </p>
          </div>
        ) : (
          <ul className="grid gap-4">
            {messages.map((m) => (
              <li key={m.id} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                <div
                  className={cn(
                    "grid max-w-[92%] gap-2 rounded-3xl px-4 py-3 text-[15px]",
                    m.role === "user"
                      ? "bg-coral-500 text-sand-50"
                      : m.error
                        ? "border-2 border-coral-600/60 bg-coral-500/10 text-coral-200"
                        : "bg-ink-800 text-sand-200",
                  )}
                >
                  {m.role === "user" ? (
                    <p className="leading-relaxed">{m.text}</p>
                  ) : (
                    <>
                      {m.text && <MarkdownText text={m.text} />}
                      {m.streaming && !m.text && (
                        <p className="flex items-center gap-2 font-semibold text-sage-400">
                          <Loader2 className="size-4 animate-spin" /> Silo is thinking…
                        </p>
                      )}
                      {m.tools.map((t) => (
                        <div key={t.id} className="flex items-center gap-2 rounded-2xl bg-ink-950/50 px-3 py-2 text-sm font-semibold text-sage-200">
                          {t.status === "running" ? (
                            <Loader2 className="size-4 shrink-0 animate-spin text-sage-300" />
                          ) : t.status === "error" ? (
                            <X className="size-4 shrink-0 text-coral-400" />
                          ) : (
                            <Check className="size-4 shrink-0 text-mint-400" />
                          )}
                          <Wrench className="size-3.5 shrink-0 text-sage-500" />
                          <span className="min-w-0 truncate">{t.label}</span>
                        </div>
                      ))}
                      {m.approvals.map((card, ci) => (
                        <div key={ci} className="grid gap-2 rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-3 py-3">
                          <p className="text-sm font-bold text-sand-100">Approval needed · {card.employer_name}</p>
                          {card.summary && <p className="text-sm text-sand-200">{card.summary}</p>}
                          {card.state === "approve" || card.state === "hold" ? (
                            <p className="flex items-center gap-2 text-sm font-bold text-sage-200">
                              {card.state === "approve" ? <Check className="size-4 text-mint-400" /> : <PauseCircle className="size-4 text-sand-300" />}
                              You chose to {card.state === "approve" ? "approve & sign" : "hold"} this run.
                            </p>
                          ) : (
                            <div className="flex flex-wrap gap-2">
                              <Button type="button" variant="success" size="sm" disabled={card.state === "busy"} onClick={() => void decideApproval(m.id, card, "approve")}>
                                {card.state === "busy" ? <Loader2 className="animate-spin" /> : <Check />} Approve &amp; sign
                              </Button>
                              <Button type="button" variant="outline" size="sm" disabled={card.state === "busy"} onClick={() => void decideApproval(m.id, card, "hold")}>
                                <PauseCircle /> Hold
                              </Button>
                            </div>
                          )}
                          {card.error && <p className="text-sm font-bold text-coral-300">{card.error}</p>}
                        </div>
                      ))}
                      {m.streaming && m.text && <Loader2 className="size-4 animate-spin text-sage-400" aria-label="Streaming" />}
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <form onSubmit={(e) => void send(e)} className="flex items-end gap-2 border-t-2 border-ink-700 px-4 py-3">
        <label htmlFor="silo-chat-input" className="sr-only">Message Silo</label>
        <textarea
          id="silo-chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          rows={1}
          placeholder="Ask Silo…"
          className="max-h-28 min-h-11 flex-1 resize-none rounded-2xl border-2 border-ink-600 bg-ink-950/60 px-3.5 py-2.5 text-[15px] font-semibold text-sand-100 placeholder:font-medium placeholder:text-muted-foreground/60 focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25"
        />
        <Button type="submit" size="icon" disabled={!input.trim() || sending} aria-label="Send message">
          {sending ? <Loader2 className="animate-spin" /> : <Send />}
        </Button>
      </form>
    </aside>
  );
}
