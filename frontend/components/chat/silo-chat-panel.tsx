"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  ArrowUp, Loader2, MessageCircle, Plus, RotateCcw, Square, WifiOff, X,
} from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  api, ApiError, streamChat, type ChatContext,
} from "@/lib/api";
import { DECIDER_ROLES } from "@/lib/status";
import { MarkdownText } from "@/lib/markdown";
import { cn } from "@/lib/utils";
import { ApprovalCard, type ApprovalCardState } from "./approval-card";
import { summariseValue, type ThinkingStep, type ToolCardState } from "./chat-types";
import { ThinkingBlock } from "./thinking-block";
import { ToolCard } from "./tool-card";

// ------------------------------------------------------------------ types
interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  text: string;
  thinking: ThinkingStep[];
  tools: ToolCardState[];
  approvals: ApprovalCardState[];
  streaming?: boolean;
  error?: boolean;
}
interface ThreadSummary { id: string; title: string }

let nextId = 1;
const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

function humaniseTool(tool: string): string {
  const words = tool.replace(/[_-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Tool";
}

// ------------------------------------------------------------------ turn reducers
// One set of reducers drives both the live SSE stream and history replay,
// so a reloaded thread renders exactly the turn the user watched happen.
function applyThinking(steps: ThinkingStep[], step: string, detail: string | undefined, status: "start" | "done"): ThinkingStep[] {
  if (status === "done") {
    const idx = steps.findIndex((s) => s.status === "active" && s.step === step);
    if (idx >= 0) {
      return steps.map((s, i) => (i === idx ? { ...s, status: "done" as const, detail: detail ?? s.detail } : s));
    }
    if (steps.some((s) => s.step === step && s.status === "done")) return steps;
    return [...steps, { id: `think-${steps.length}-${step}`, step, detail, status: "done" }];
  }
  const active = steps.find((s) => s.status === "active" && s.step === step);
  if (active) return steps.map((s) => (s === active ? { ...s, detail: detail ?? s.detail } : s));
  // A new step starting implies the previous one finished.
  return [
    ...steps.map((s) => (s.status === "active" ? { ...s, status: "done" as const } : s)),
    { id: `think-${steps.length}-${step}`, step, detail, status: "active" as const },
  ];
}

function applyToolStart(tools: ToolCardState[], tool: string, label: string, input: unknown): ToolCardState[] {
  return [
    ...tools,
    {
      id: `tool-${tools.length}-${tool}`,
      tool,
      label: label || humaniseTool(tool),
      input,
      inputSummary: summariseValue(input),
      status: "running",
      summary: "",
      hasData: false,
    },
  ];
}

function applyToolResult(tools: ToolCardState[], tool: string, summary: string, data: unknown): ToolCardState[] {
  const hasData = data !== undefined && data !== null;
  const idx = tools.findIndex((t) => t.tool === tool && t.status === "running");
  if (idx < 0) {
    // A result with no start in this turn (e.g. a trimmed trace): still a
    // real event, so show the completed call rather than dropping it.
    return [
      ...tools,
      {
        id: `tool-${tools.length}-${tool}`,
        tool,
        label: humaniseTool(tool),
        input: null,
        inputSummary: "",
        status: "done",
        summary,
        data,
        hasData,
      },
    ];
  }
  return tools.map((t, i) => (i === idx ? { ...t, status: "done" as const, summary, data, hasData } : t));
}

function applyLegacyTool(tools: ToolCardState[], name: string, label: string, status: string): ToolCardState[] {
  const nextStatus = status === "error" ? "error" : status === "running" || status === "started" || status === "start" ? "running" : "done";
  const existing = tools.find((t) => t.tool === name && t.label === label && t.status === "running");
  if (existing) {
    return tools.map((t) => (t === existing ? { ...t, status: nextStatus as ToolCardState["status"] } : t));
  }
  if (nextStatus !== "running" && tools.some((t) => t.tool === name && t.label === label)) return tools;
  return [
    ...tools,
    {
      id: `tool-${tools.length}-${name}`,
      tool: name,
      label: label || humaniseTool(name),
      input: null,
      inputSummary: "",
      status: nextStatus as ToolCardState["status"],
      summary: "",
      hasData: false,
    },
  ];
}

function finishThinking(steps: ThinkingStep[]): ThinkingStep[] {
  return steps.map((s) => (s.status === "active" ? { ...s, status: "done" as const } : s));
}

// ------------------------------------------------------- history normalising
// The history payloads are owned by the backend; normalise defensively
// (array or wrapped) without trusting any single shape.
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
const strOf = (v: unknown) => (typeof v === "string" ? v : "");

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

/** Rebuild thinking steps + tool cards from one message's persisted trace. */
function traceToTurn(trace: unknown): { thinking: ThinkingStep[]; tools: ToolCardState[] } {
  let thinking: ThinkingStep[] = [];
  let tools: ToolCardState[] = [];
  if (!Array.isArray(trace)) return { thinking, tools };
  for (const item of trace) {
    const rec = asRecord(item);
    if (!rec) continue;
    const kind = strOf(rec.type) || strOf(rec.kind) || strOf(rec.event);
    if (kind === "thinking" || (!kind && typeof rec.step === "string")) {
      const step = strOf(rec.step) || strOf(rec.title) || strOf(rec.label);
      if (!step) continue;
      const raw = strOf(rec.status).toLowerCase();
      thinking = applyThinking(thinking, step, strOf(rec.detail) || undefined, raw === "start" || raw === "running" ? "start" : "done");
    } else if (kind === "tool_start" || kind === "tool_call") {
      const tool = strOf(rec.tool) || strOf(rec.name);
      if (tool) tools = applyToolStart(tools, tool, strOf(rec.label), rec.input ?? rec.args ?? null);
    } else if (kind === "tool_result" || kind === "tool_output") {
      const tool = strOf(rec.tool) || strOf(rec.name);
      if (tool) tools = applyToolResult(tools, tool, strOf(rec.summary) || strOf(rec.message), rec.data ?? rec.result ?? rec.output);
    } else if (kind === "tool") {
      const name = strOf(rec.name) || strOf(rec.tool);
      if (name) tools = applyLegacyTool(tools, name, strOf(rec.label) || name, strOf(rec.status) || "done");
    }
  }
  // A persisted turn is complete by definition: nothing stays "active".
  return { thinking: finishThinking(thinking), tools: tools.map((t) => (t.status === "running" ? { ...t, status: "done" as const } : t)) };
}

function normaliseApprovals(raw: unknown): ApprovalCardState[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item) => {
    const rec = asRecord(item);
    if (!rec || typeof rec.run_id !== "string") return [];
    return [{
      run_id: rec.run_id,
      employer_id: strOf(rec.employer_id),
      employer_name: strOf(rec.employer_name),
      summary: strOf(rec.summary),
    }];
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
    const { thinking, tools } = role === "assistant"
      ? traceToTurn(rec.trace ?? rec.events)
      : { thinking: [] as ThinkingStep[], tools: [] as ToolCardState[] };
    return [{
      id: nextId++,
      role: role as "user" | "assistant",
      text: textRaw,
      thinking,
      tools,
      approvals: role === "assistant" ? normaliseApprovals(rec.approvals) : [],
    }];
  });
}

/**
 * Runs still waiting on a decision are not part of any stored message, so a
 * reloaded thread would silently drop them. Re-surface each one as an
 * approval card (which fetches the run's live state) at the end of the thread.
 */
async function withPendingRuns(history: ChatMessage[]): Promise<ChatMessage[]> {
  try {
    const shown = new Set(history.flatMap((m) => m.approvals.map((a) => a.run_id)));
    const waiting = (await api.runs())
      .filter((r) => r.pending && r.resumable && !shown.has(r.id))
      .slice(0, 5);
    if (waiting.length === 0) return history;
    return [
      ...history,
      {
        id: nextId++,
        role: "assistant",
        text: waiting.length === 1 ? "One run is still waiting for a decision." : `${waiting.length} runs are still waiting for a decision.`,
        thinking: [],
        tools: [],
        approvals: waiting.map((r) => ({
          run_id: r.id,
          employer_id: r.employer_id,
          employer_name: r.employer_name,
          summary: r.pending?.message ?? "",
        })),
      },
    ];
  } catch {
    return history;
  }
}

// ------------------------------------------------------------------ panel
export function SiloChatPanel({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const pathname = usePathname() ?? "/dashboard";
  const router = useRouter();
  const [role, setRole] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const streamSeq = useRef(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasOpenRef = useRef(false);
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
  const stickRef = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const lastSentRef = useRef<string>("");

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

  const goLogin = useCallback(() => {
    router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [router, pathname]);

  // A failed load is "offline" only when the network itself failed.
  const handleLoadFailure = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 401) {
      goLogin();
      return;
    }
    if (err instanceof ApiError && err.status === 0) {
      setOffline(true);
      setLoadError(null);
    } else {
      setOffline(false);
      setLoadError(errMsg(err));
    }
  }, [goLogin]);

  useEffect(() => {
    let alive = true;
    api.me().then((me) => { if (alive) setRole(me.role); }).catch(() => { /* role stays unknown: no approve buttons */ });
    return () => { alive = false; };
  }, []);

  // Stop any in-flight stream when the panel unmounts.
  useEffect(() => () => {
    streamSeq.current += 1;
    abortRef.current?.abort();
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      const list = normaliseThreads(await api.chatThreads());
      setThreads(list);
      setOffline(false);
      setLoadError(null);
      if (list.length > 0) {
        const latest = list[0];
        setThreadId(latest.id);
        const history = normaliseMessages(await api.chatMessages(latest.id));
        setMessages(await withPendingRuns(history));
      } else {
        setMessages(await withPendingRuns([]));
      }
    } catch (err) {
      handleLoadFailure(err);
    }
    setHistoryLoaded(true);
  }, [handleLoadFailure]);

  useEffect(() => {
    if (open && !historyLoaded) void loadHistory();
  }, [open, historyLoaded, loadHistory]);

  // Keep the view pinned to the newest content only while the reader is
  // already at the bottom; scrolling up to re-read is never overridden.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages, open]);

  // Focus moves into the panel on open and back to the trigger on close.
  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    } else if (wasOpenRef.current) {
      triggerRef.current?.focus();
    }
    wasOpenRef.current = open;
  }, [open]);

  async function openThread(id: string) {
    streamSeq.current += 1;
    abortRef.current?.abort();
    setSending(false);
    setThreadId(id);
    stickRef.current = true;
    try {
      const history = normaliseMessages(await api.chatMessages(id));
      setMessages(await withPendingRuns(history));
      setOffline(false);
      setLoadError(null);
    } catch (err) {
      handleLoadFailure(err);
    }
  }

  function newChat() {
    streamSeq.current += 1;
    abortRef.current?.abort();
    setThreadId(null);
    setMessages([]);
    setSending(false);
    stickRef.current = true;
    inputRef.current?.focus();
  }

  function stop() {
    streamSeq.current += 1;
    abortRef.current?.abort();
    setSending(false);
    setMessages((prev) =>
      prev.map((m) =>
        m.streaming
          ? {
              ...m,
              streaming: false,
              thinking: finishThinking(m.thinking),
              tools: m.tools.map((t) => (t.status === "running" ? { ...t, status: "done" as const } : t)),
              text: m.text || "Stopped. Ask again when you are ready.",
            }
          : m,
      ),
    );
  }

  function patchMessage(id: number, patch: (m: ChatMessage) => ChatMessage) {
    setMessages((prev) => prev.map((m) => (m.id === id ? patch(m) : m)));
  }

  const dispatch = useCallback(
    async (text: string, activeThreadId: string | null) => {
      const userMsg: ChatMessage = { id: nextId++, role: "user", text, thinking: [], tools: [], approvals: [] };
      const assistantId = nextId++;
      const assistantMsg: ChatMessage = {
        id: assistantId, role: "assistant", text: "", thinking: [], tools: [], approvals: [], streaming: true,
      };
      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setSending(true);
      stickRef.current = true;
      lastSentRef.current = text;
      const controller = new AbortController();
      abortRef.current = controller;
      const seq = ++streamSeq.current;
      let finished = false;
      try {
        await streamChat(
          { ...(activeThreadId ? { thread_id: activeThreadId } : {}), message: text, context },
          (event) => {
            switch (event.type) {
              case "token":
                patchMessage(assistantId, (m) => ({ ...m, text: m.text + event.text }));
                break;
              case "thinking":
                patchMessage(assistantId, (m) => ({
                  ...m,
                  thinking: applyThinking(m.thinking, event.step, event.detail, event.status),
                }));
                break;
              case "tool_start":
                patchMessage(assistantId, (m) => ({
                  ...m,
                  tools: applyToolStart(m.tools, event.tool, event.label, event.input),
                }));
                break;
              case "tool_result":
                patchMessage(assistantId, (m) => ({
                  ...m,
                  tools: applyToolResult(m.tools, event.tool, event.summary, event.data),
                }));
                break;
              case "tool":
                patchMessage(assistantId, (m) => ({
                  ...m,
                  tools: applyLegacyTool(m.tools, event.name, event.label, event.status),
                }));
                break;
              case "approval":
                patchMessage(assistantId, (m) => ({
                  ...m,
                  approvals: [...m.approvals, event.approval],
                }));
                break;
              case "done":
                finished = true;
                if (event.thread_id && streamSeq.current === seq) {
                  setThreadId(event.thread_id);
                  setThreads((prev) =>
                    prev.some((t) => t.id === event.thread_id)
                      ? prev
                      : [{ id: event.thread_id, title: text.slice(0, 60) }, ...prev],
                  );
                }
                patchMessage(assistantId, (m) => ({ ...m, streaming: false, thinking: finishThinking(m.thinking) }));
                break;
              case "error":
                finished = true;
                patchMessage(assistantId, (m) => ({
                  ...m,
                  error: true,
                  streaming: false,
                  thinking: finishThinking(m.thinking),
                  text: m.text || event.message,
                }));
                break;
            }
          },
          controller.signal,
        );
        // The stream closed. Without a `done` the turn was cut off: say so
        // instead of presenting a half answer as complete.
        patchMessage(assistantId, (m) => ({
          ...m,
          streaming: false,
          thinking: finishThinking(m.thinking),
          tools: m.tools.map((t) => (t.status === "running" ? { ...t, status: (finished ? "done" : "error") as ToolCardState["status"] } : t)),
          ...(finished
            ? { text: m.text || (m.tools.length === 0 && m.approvals.length === 0 && !m.error ? "Done." : m.text) }
            : { error: true, text: m.text ? `${m.text}\n\n_The connection closed before Silo finished._` : "The connection closed before Silo finished." }),
        }));
      } catch (err) {
        if ((err as { name?: string })?.name === "AbortError") return;
        if (err instanceof ApiError && err.status === 401) {
          goLogin();
          return;
        }
        const network = err instanceof ApiError && err.status === 0;
        if (network) setOffline(true);
        patchMessage(assistantId, (m) => ({
          ...m,
          streaming: false,
          error: true,
          thinking: finishThinking(m.thinking),
          text: m.text || (network ? "Can't reach Silo chat right now." : err instanceof ApiError && err.status === 404 ? "Silo chat is not available on this server." : errMsg(err)),
        }));
      } finally {
        if (streamSeq.current === seq) setSending(false);
      }
    },
    // `context` is rebuilt from the pathname on every render; the values
    // it carries are stable per route, which is what the stream needs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pathname, employerId, runId, goLogin],
  );

  async function send(ev?: React.FormEvent) {
    ev?.preventDefault();
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    if (inputRef.current) inputRef.current.style.height = "auto";
    setOffline(false);
    await dispatch(text, threadId);
  }

  function retryLast(failedId: number) {
    const text = lastSentRef.current;
    if (!text || sending) return;
    setMessages((prev) => {
      const idx = prev.findIndex((m) => m.id === failedId);
      if (idx < 0) return prev;
      // Drop the failed turn (its assistant bubble and the user message
      // directly above it) — dispatch re-adds both cleanly.
      const dropUser = idx > 0 && prev[idx - 1].role === "user";
      return prev.filter((_, i) => i !== idx && !(dropUser && i === idx - 1));
    });
    setOffline(false);
    void dispatch(text, threadId);
  }

  const suggestions = [
    `Check the balance for ${contextName ?? "an employer"}`,
    "Explain the most recent payroll run",
    "Which runs are waiting for approval?",
  ];

  if (!open) {
    return (
      <button
        ref={triggerRef}
        type="button"
        onClick={() => onOpenChange(true)}
        aria-label="Open Silo chat"
        className="fixed bottom-4 right-4 z-40 flex items-center gap-2 rounded-full border-2 border-sage-600/60 bg-ink-800 py-1.5 pl-1.5 pr-3 shadow-2xl sm:bottom-5 sm:right-5 sm:py-2 sm:pl-2 sm:pr-4 transition-transform hover:scale-[1.03] focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-400/60"
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
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onOpenChange(false);
        }
      }}
      className="fixed inset-0 z-50 flex flex-col border-ink-700 bg-ink-900 shadow-2xl sm:inset-y-0 sm:left-auto sm:right-0 sm:w-[420px] sm:max-w-full sm:border-l-2 xl:shadow-none"
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
        <button
          type="button"
          onClick={() => onOpenChange(false)}
          aria-label="Close Silo chat"
          className="rounded-full p-2 text-sage-300 transition-colors hover:bg-ink-700 hover:text-sand-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sage-400/60"
        >
          <X className="size-5" />
        </button>
      </header>

      <div className="flex flex-wrap items-center gap-2 border-b-2 border-ink-700 px-4 py-2.5">
        <span className="text-xs font-bold text-sage-400">Looking at:</span>
        <Badge tone="sage">{contextName ?? (employerId || runId ? "This page" : "All employers")}</Badge>
        {threads.length > 1 && (
          <label className="ml-auto flex items-center gap-1 text-xs font-bold text-sage-400">
            History
            <select
              value={threadId ?? ""}
              onChange={(e) => void openThread(e.target.value)}
              className="max-w-36 rounded-lg border border-ink-600 bg-ink-800 px-1.5 py-1 text-xs font-semibold text-sand-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sage-400/60"
              aria-label="Open a previous conversation"
            >
              {threads.map((t) => (
                <option key={t.id} value={t.id}>{t.title}</option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
        }}
        className="min-h-0 flex-1 overflow-y-auto px-4 py-4 [scrollbar-color:theme(colors.ink.600)_transparent] [scrollbar-width:thin]"
      >
        {(offline || loadError) && messages.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-10 text-center" role="status">
            <WifiOff className="size-8 text-sage-500" />
            <p className="font-bold text-sand-100">{offline ? "Silo chat is offline" : "Could not load the chat"}</p>
            <p className="max-w-64 text-sm leading-relaxed text-sage-300">
              {offline
                ? "PaySilo's backend is not reachable. The rest of the dashboard may be affected too."
                : loadError}
            </p>
            <Button type="button" variant="outline" size="sm" onClick={() => { setHistoryLoaded(false); void loadHistory(); }}>
              Try again
            </Button>
          </div>
        ) : messages.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-8 text-center">
            <LottieAnimation name="silo-wave" className="size-24" label="Silo waving" />
            <div className="grid gap-1">
              <p className="font-display text-lg font-semibold text-sand-100">Hi, I am Silo.</p>
              <p className="max-w-72 text-sm leading-relaxed text-sage-300">
                Ask me to check a balance, explain a run, or start payroll. You will see how I work through it, step by step.
              </p>
            </div>
            <div className="grid w-full max-w-80 gap-2 pt-2 text-left">
              {suggestions.map((s) => (
                <button
                  key={s}
                  type="button"
                  disabled={sending}
                  onClick={() => void dispatch(s, threadId)}
                  className="rounded-2xl border border-ink-600 bg-ink-800/70 px-3.5 py-2.5 text-sm font-semibold text-sand-200 transition-colors hover:border-sage-600/70 hover:bg-ink-800 hover:text-sand-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sage-400/60 disabled:opacity-50"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <ul className="grid gap-5" role="log" aria-live="polite" aria-relevant="additions text" aria-label="Conversation with Silo">
            {messages.map((m) => (
              <li key={m.id} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                {m.role === "user" ? (
                  <div className="max-w-[88%] rounded-3xl rounded-br-lg bg-coral-500 px-4 py-3 text-[15px] text-sand-50">
                    <p className="leading-relaxed">{m.text}</p>
                  </div>
                ) : (
                  <div
                    className={cn(
                      "grid w-full max-w-[96%] gap-2.5 rounded-3xl rounded-bl-lg px-4 py-3.5 text-[15px]",
                      m.error
                        ? "border-2 border-coral-600/50 bg-coral-500/10 text-coral-200"
                        : "bg-ink-800 text-sand-200",
                    )}
                  >
                    <ThinkingBlock steps={m.thinking} streaming={Boolean(m.streaming)} />
                    {m.tools.map((t) => (
                      <ToolCard key={t.id} card={t} />
                    ))}
                    {m.text && <MarkdownText text={m.text} />}
                    {m.streaming && !m.text && m.thinking.length === 0 && m.tools.length === 0 && (
                      <p className="flex items-center gap-2 font-semibold text-sage-300" role="status">
                        <Loader2 className="size-4 animate-spin" /> Silo is thinking…
                      </p>
                    )}
                    {m.streaming && m.text && (
                      <span className="flex items-center gap-1.5 text-xs font-bold text-sage-400" role="status">
                        <span className="size-1.5 animate-pulse rounded-full bg-sage-400" aria-hidden />
                        Writing…
                      </span>
                    )}
                    {m.approvals.map((card, ci) => (
                      <ApprovalCard
                        key={`${card.run_id}-${ci}`}
                        card={card}
                        canDecide={role !== null && DECIDER_ROLES.includes(role)}
                      />
                    ))}
                    {m.error && !m.streaming && (
                      <button
                        type="button"
                        onClick={() => retryLast(m.id)}
                        disabled={sending}
                        className="inline-flex w-fit items-center gap-1.5 rounded-lg px-1 py-0.5 text-sm font-extrabold text-coral-300 transition-colors hover:text-coral-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-coral-400/60 disabled:opacity-50"
                      >
                        <RotateCcw className="size-3.5" aria-hidden /> Try again
                      </button>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {offline && messages.length > 0 && (
        <p className="flex items-center gap-2 border-t border-ink-700 px-4 py-2 text-xs font-bold text-sand-300" role="status">
          <WifiOff className="size-3.5 shrink-0 text-sage-400" aria-hidden />
          Chat connection dropped. Your messages above are saved in this thread.
        </p>
      )}

      <form onSubmit={(e) => void send(e)} className="flex items-end gap-2 border-t-2 border-ink-700 px-4 py-3">
        <label htmlFor="silo-chat-input" className="sr-only">Message Silo</label>
        <textarea
          ref={inputRef}
          id="silo-chat-input"
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            const el = e.currentTarget;
            el.style.height = "auto";
            el.style.height = `${Math.min(el.scrollHeight, 112)}px`;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
              e.preventDefault();
              void send();
            }
          }}
          rows={1}
          placeholder="Ask Silo…"
          className="max-h-28 min-h-11 flex-1 resize-none rounded-2xl border-2 border-ink-600 bg-ink-950/60 px-3.5 py-2.5 text-[15px] font-semibold text-sand-100 placeholder:font-medium placeholder:text-sage-400/80 focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25"
        />
        {sending ? (
          <Button type="button" size="icon" variant="outline" onClick={stop} aria-label="Stop Silo">
            <Square />
          </Button>
        ) : (
          <Button type="submit" size="icon" disabled={!input.trim()} aria-label="Send message">
            <ArrowUp />
          </Button>
        )}
      </form>
    </aside>
  );
}
