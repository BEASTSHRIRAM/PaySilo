"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy, Download, FileJson, TriangleAlert, Upload, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { apiWorkflows, ApiError, type GraphIssue, type WorkflowGraph } from "@/lib/api";

export interface WorkflowEnvelope { name: string; description: string; graph: WorkflowGraph }

const textareaClass =
  "h-full min-h-[240px] w-full resize-none rounded-2xl border-2 border-ink-600 bg-ink-950/80 p-3 font-mono text-xs font-semibold leading-relaxed text-sand-100 focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25";

function slug(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "workflow";
}

/**
 * View / edit the workflow as JSON (like n8n): copy, download, paste or upload. Import is validated by the server
 * against the node registry (types, required config, edge rules, single trigger, size limits). A valid import
 * replaces the canvas after a confirm and is NOT saved until the person clicks Save.
 */
export function JsonDrawer({
  open, onClose, current, hasContent, onImport,
}: {
  open: boolean;
  onClose: () => void;
  current: WorkflowEnvelope;
  hasContent: boolean;
  onImport: (env: WorkflowEnvelope) => void;
}) {
  const [text, setText] = useState("");
  const [issues, setIssues] = useState<GraphIssue[]>([]);
  const [syntaxError, setSyntaxError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [copied, setCopied] = useState(false);
  const [pending, setPending] = useState<WorkflowEnvelope | null>(null);
  const [okNote, setOkNote] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setText(JSON.stringify(current, null, 2));
      setIssues([]); setSyntaxError(null); setPending(null); setOkNote(null); setCopied(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  async function copy() {
    try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1800); }
    catch { setSyntaxError("Copy was blocked by the browser. Select the text and copy it manually."); }
  }
  function download() {
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${slug(current.name)}.workflow.json`; a.click();
    URL.revokeObjectURL(url);
  }
  async function upload(file: File | undefined) {
    if (!file) return;
    if (file.size > 400_000) { setSyntaxError("That file is larger than 400 KB: too big to be a workflow."); return; }
    setText(await file.text());
    setIssues([]); setSyntaxError(null); setPending(null); setOkNote(null);
  }

  async function check() {
    setSyntaxError(null); setIssues([]); setPending(null); setOkNote(null);
    let parsed: unknown;
    try { parsed = JSON.parse(text); }
    catch (e) { setSyntaxError(`That isn't valid JSON: ${(e as Error).message}`); return; }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) { setSyntaxError("Expected a JSON object with nodes and edges (or {name, description, graph})."); return; }
    const obj = parsed as Record<string, unknown>;
    const graph = (obj.graph && typeof obj.graph === "object" ? obj.graph : obj) as WorkflowGraph;
    setChecking(true);
    try {
      const res = await apiWorkflows.validate(graph);
      if (!res.valid || !res.graph) { setIssues(res.errors); setChecking(false); return; }
      const env: WorkflowEnvelope = {
        name: typeof obj.name === "string" && obj.name.trim() ? obj.name.trim() : current.name,
        description: typeof obj.description === "string" ? obj.description : current.description,
        graph: res.graph,
      };
      setOkNote(`Valid: ${res.node_count} nodes, trigger ${res.trigger_type}.`);
      if (hasContent) setPending(env); else onImport(env);
    } catch (e) {
      setSyntaxError(e instanceof ApiError ? e.message : "Could not validate right now. Try again.");
    }
    setChecking(false);
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-ink-950/60" role="dialog" aria-modal="true" aria-label="Workflow JSON">
      <aside className="grid h-full w-full max-w-[560px] grid-rows-[auto_minmax(0,1fr)_auto] gap-3 border-l-2 border-ink-600 bg-ink-900 p-4 shadow-2xl">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="flex items-center gap-2 font-display text-lg font-semibold text-sand-100"><FileJson className="size-5 text-sage-400" /> Workflow JSON</p>
            <p className="mt-0.5 text-xs font-semibold text-sage-400">Copy it, edit it, or paste / upload another workflow. Nothing changes until you apply it, and nothing is saved until you press Save.</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close JSON" className="rounded-full p-1.5 text-sage-300 hover:bg-ink-700 hover:text-sand-100"><X className="size-5" /></button>
        </div>

        <textarea className={textareaClass} value={text} spellCheck={false} aria-label="Workflow JSON"
          onChange={(e) => { setText(e.target.value); setIssues([]); setSyntaxError(null); setPending(null); setOkNote(null); }} />

        <div className="grid gap-3">
          {syntaxError && <p role="alert" className="flex items-start gap-2 rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-3 py-2 text-sm font-bold text-coral-300"><TriangleAlert className="mt-0.5 size-4 shrink-0" /> {syntaxError}</p>}
          {issues.length > 0 && (
            <div role="alert" className="grid max-h-40 gap-1.5 overflow-y-auto rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-3 py-2" data-testid="json-errors">
              <p className="flex items-center gap-2 text-sm font-extrabold text-coral-300"><TriangleAlert className="size-4" /> {issues.length} problem{issues.length === 1 ? "" : "s"}: nothing was imported</p>
              {issues.map((e, i) => (
                <p key={i} className="text-xs font-bold text-coral-300">
                  {e.node_id ? <span className="mr-1 rounded bg-coral-500/20 px-1.5 py-0.5 font-mono">{e.node_id}{e.field ? `.${e.field}` : ""}</span> : e.edge_id ? <span className="mr-1 rounded bg-coral-500/20 px-1.5 py-0.5 font-mono">edge {e.edge_id}</span> : null}
                  {e.message}
                </p>
              ))}
            </div>
          )}
          {okNote && !pending && <p className="flex items-center gap-2 text-sm font-bold text-mint-400"><Check className="size-4" /> {okNote}</p>}
          {pending && (
            <div className="grid gap-2 rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-3 py-3" data-testid="json-confirm">
              <p className="text-sm font-bold text-sand-100">{okNote} Replace the current canvas with this workflow?</p>
              <p className="text-xs font-semibold text-sand-200">Your current canvas edits are replaced. It is not saved until you press Save, and Reload brings back the saved version.</p>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={() => { onImport(pending); setPending(null); }}>Replace canvas</Button>
                <Button size="sm" variant="ghost" onClick={() => setPending(null)}>Keep current</Button>
              </div>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={() => void copy()}>{copied ? <Check /> : <Copy />} {copied ? "Copied" : "Copy"}</Button>
            <Button size="sm" variant="outline" onClick={download}><Download /> Download .json</Button>
            <Button size="sm" variant="outline" onClick={() => fileRef.current?.click()}><Upload /> Upload</Button>
            <input ref={fileRef} type="file" accept="application/json,.json" className="hidden" onChange={(e) => { void upload(e.target.files?.[0]); e.target.value = ""; }} />
            <Button size="sm" onClick={() => void check()} disabled={checking || !text.trim()}>{checking ? "Checking…" : "Import to canvas"}</Button>
          </div>
        </div>
      </aside>
    </div>
  );
}
