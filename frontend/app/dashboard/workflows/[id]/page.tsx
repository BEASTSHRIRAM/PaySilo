"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, ChevronDown, Clock, Plus, RefreshCw, Save, Search, Trash2, TriangleAlert, X } from "lucide-react";
import ReactFlow, {
  Background, BackgroundVariant, Controls, MiniMap, ReactFlowProvider,
  addEdge, applyEdgeChanges, applyNodeChanges, useReactFlow,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange,
} from "reactflow";

import { SectionError, SectionLoading, PageNotice } from "@/components/dashboard/states";
import { FebShell } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { WorkflowNode } from "@/components/workflows/workflow-node";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/select";
import { ApiError, apiWorkflows, type WorkflowDetail, type WorkflowGraph, type WorkflowRunStep } from "@/lib/api";
import {
  ADD_NEXT_EVENT, GROUP_LABELS, PALETTE_GROUPS, WORKFLOW_NODE_DEFS,
  cleanNodeData, defaultDataFor, isKnownNodeType, kindChipClass, kindLabel, kindMiniMapColor,
  newNodeId, nodeDef, validateNodeData, type AddNextDetail, type ConfigFieldDef,
} from "@/lib/workflows";
import { cn } from "@/lib/utils";

// One custom component registered for every registry type, so the saved
// graph's `type` values round-trip unchanged. Unknown types saved by
// templates are rendered through a fallback registration at load time
// (see nodeTypesFor below).
const baseNodeTypes = Object.fromEntries(WORKFLOW_NODE_DEFS.map((d) => [d.type, WorkflowNode]));

type RFNode = Node<Record<string, unknown>>;
type RFEdge = Edge;

const textareaClass =
  "flex w-full rounded-2xl border-2 border-ink-600 bg-ink-900/80 px-4 py-3 text-[15px] font-semibold text-sand-100 placeholder:font-medium placeholder:text-muted-foreground/60 focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25";

const EDGE_LABEL_STYLE = { fill: "#244855", fontWeight: 800, fontSize: 11 };
const EDGE_LABEL_BG = { fill: "#FBE9D0", fillOpacity: 0.95 };

function branchEdgeProps(sourceHandle?: string | null) {
  return sourceHandle === "true" || sourceHandle === "false"
    ? { label: sourceHandle, labelStyle: EDGE_LABEL_STYLE, labelBgStyle: EDGE_LABEL_BG, labelBgPadding: [6, 3] as [number, number], labelBgBorderRadius: 999 }
    : {};
}

/** Graph JSON -> React Flow projection. __editable marks the + stub; stripped on save. Missing branches simply have no edge — rendering tolerates that. */
function graphToFlow(graph: WorkflowGraph | undefined | null): { nodes: RFNode[]; edges: RFEdge[] } {
  const nodes: RFNode[] = (graph?.nodes ?? []).map((n, i) => ({
    id: n.id, type: n.type,
    position: n.position ?? { x: 240, y: 60 + i * 140 },
    data: { ...(n.data ?? {}), __editable: true },
  }));
  const edges: RFEdge[] = (graph?.edges ?? []).map((e, i) => ({
    id: e.id || `edge_${e.source}_${e.target}_${i}`,
    source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? undefined,
    type: "default", style: { stroke: "#244855", strokeWidth: 1.8 },
    ...branchEdgeProps(e.sourceHandle),
  }));
  return { nodes, edges };
}

function flowToGraph(nodes: RFNode[], edges: RFEdge[]): WorkflowGraph {
  return {
    nodes: nodes.map((n) => ({ id: n.id, type: n.type ?? "", position: { x: Math.round(n.position.x), y: Math.round(n.position.y) }, data: cleanNodeData(n.data) })),
    edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? null })),
  };
}

function snapshotOf(name: string, description: string, enabled: boolean, nodes: RFNode[], edges: RFEdge[]): string {
  return JSON.stringify({ name, description, enabled, graph: flowToGraph(nodes, edges) });
}

const strVal = (data: Record<string, unknown>, key: string) =>
  typeof data[key] === "string" ? (data[key] as string) : data[key] == null ? "" : String(data[key]);

// ------------------------------------------------------------------ registry-driven field
function RegistryField({ field, value, error, onChange }: { field: ConfigFieldDef; value: string; error?: string; onChange: (v: string) => void }) {
  return (
    <label className="grid gap-1.5">
      <span className="text-sm font-extrabold text-sage-400">{field.label}{field.required ? " *" : ""}</span>
      {field.kind === "select" ? (
        <NativeSelect value={value || field.defaultValue || ""} onChange={(e) => onChange(e.target.value)} aria-label={field.label}>
          {(field.options ?? []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </NativeSelect>
      ) : field.kind === "textarea" ? (
        <textarea className={textareaClass} rows={3} value={value} onChange={(e) => onChange(e.target.value)} placeholder={field.placeholder} aria-label={field.label} />
      ) : (
        <Input type={field.kind === "number" ? "number" : "text"} value={value} onChange={(e) => onChange(e.target.value)} placeholder={field.placeholder} aria-label={field.label} />
      )}
      {field.help && <span className="text-xs font-semibold text-sage-500">{field.help}</span>}
      {error && <span role="alert" className="text-xs font-bold text-coral-300">{error}</span>}
    </label>
  );
}

interface PickerState { sourceId: string | null; sourceHandle: string | null; triggersOnly: boolean }

function EditorInner() {
  const params = useParams<{ id: string }>();
  const workflowId = params.id;
  const { session } = useSession(`/dashboard/workflows/${workflowId}`);
  const reactFlow = useReactFlow();
  const wrapperRef = useRef<HTMLDivElement>(null);

  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [nodes, setNodes] = useState<RFNode[]>([]);
  const [edges, setEdges] = useState<RFEdge[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveErrors, setSaveErrors] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState<string>("");
  const [savedAt, setSavedAt] = useState<string | null>(null);

  const [paletteQuery, setPaletteQuery] = useState("");
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});
  const [picker, setPicker] = useState<PickerState | null>(null);

  // Last run output per node (drawer section, #13) — latest run only.
  const [lastStepsByNode, setLastStepsByNode] = useState<Record<string, WorkflowRunStep>>({});

  const currentSnapshot = useMemo(() => (detail ? snapshotOf(name, description, enabled, nodes, edges) : ""), [detail, name, description, enabled, nodes, edges]);
  const dirty = !!detail && savedSnapshot !== "" && currentSnapshot !== savedSnapshot;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const wf = await apiWorkflows.get(workflowId);
      setDetail(wf); setName(wf.name); setDescription(wf.description ?? ""); setEnabled(wf.enabled);
      const { nodes: n, edges: e } = graphToFlow(wf.graph);
      setNodes(n); setEdges(e);
      setSavedSnapshot(snapshotOf(wf.name, wf.description ?? "", wf.enabled, n, e));
      setSavedAt(wf.updated_at ?? null);
      setError(null);
      // Best-effort: latest run steps for the drawer's "Last run output".
      try {
        const runs = await apiWorkflows.runs({ workflow_id: workflowId });
        if (runs.length > 0) {
          const latest = [...runs].sort((a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime())[0];
          const runDetail = await apiWorkflows.run(latest.id);
          const byNode: Record<string, WorkflowRunStep> = {};
          for (const s of runDetail.steps) if (!byNode[s.node_id]) byNode[s.node_id] = s;
          setLastStepsByNode(byNode);
        }
      } catch { /* last-run output is optional in the editor */ }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setError(e instanceof ApiError ? e.message : errMsg(e));
    }
    setLoading(false);
  }, [workflowId]);

  useEffect(() => { void load(); }, [load]);

  const nodeTypes = useMemo(() => {
    const extra: Record<string, typeof WorkflowNode> = {};
    for (const n of nodes) if (n.type && !isKnownNodeType(n.type)) extra[n.type] = WorkflowNode;
    return { ...baseNodeTypes, ...extra };
  }, [nodes]);

  const selectedNode = useMemo(() => nodes.find((n) => n.id === selectedNodeId) ?? null, [nodes, selectedNodeId]);
  const selectedEdge = useMemo(() => edges.find((e) => e.id === selectedEdgeId) ?? null, [edges, selectedEdgeId]);
  const selectedDef = selectedNode ? nodeDef(selectedNode.type ?? "") : null;
  const selectedErrors = selectedNode ? validateNodeData(selectedNode.type ?? "", cleanNodeData(selectedNode.data)) : {};

  const onNodesChange = useCallback((changes: NodeChange[]) => setNodes((cur) => applyNodeChanges(changes, cur)), []);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => setEdges((cur) => applyEdgeChanges(changes, cur)), []);
  const onConnect = useCallback((conn: Connection) => {
    if (!conn.source || !conn.target || conn.source === conn.target) return;
    setEdges((cur) => addEdge({ ...conn, id: `edge_${conn.source}_${conn.target}_${conn.sourceHandle ?? "out"}_${Date.now().toString(36)}`, type: "default", style: { stroke: "#244855", strokeWidth: 1.8 }, ...branchEdgeProps(conn.sourceHandle) }, cur));
  }, []);

  const addNodeAt = useCallback((type: string, position: { x: number; y: number }): string => {
    const id = newNodeId(type);
    setNodes((cur) => [...cur, { id, type, position, data: { ...defaultDataFor(type), __editable: true } }]);
    return id;
  }, []);

  function addNode(type: string, position?: { x: number; y: number }) {
    const id = addNodeAt(type, position ?? { x: 260 + (nodes.length % 3) * 48, y: 90 + nodes.length * 36 });
    setSelectedNodeId(id); setSelectedEdgeId(null); setDrawerOpen(true); setNotice(null);
  }

  /** Append a node pre-connected from a source node (+ stub / picker). */
  function addConnected(type: string, sourceId: string, sourceHandle: string | null) {
    const source = nodes.find((n) => n.id === sourceId);
    if (!source) return;
    const siblings = edges.filter((e) => e.source === sourceId && (e.sourceHandle ?? null) === sourceHandle).length;
    const pos = { x: source.position.x + siblings * 250, y: source.position.y + 190 };
    const id = addNodeAt(type, pos);
    const edge: RFEdge = {
      id: `edge_${sourceId}_${id}_${sourceHandle ?? "out"}_${Date.now().toString(36)}`,
      source: sourceId, target: id, sourceHandle: sourceHandle ?? undefined,
      type: "default", style: { stroke: "#244855", strokeWidth: 1.8 }, ...branchEdgeProps(sourceHandle),
    };
    setEdges((cur) => [...cur, edge]);
    setSelectedNodeId(id); setSelectedEdgeId(null); setDrawerOpen(true);
  }

  // "+" stub events from node cards.
  useEffect(() => {
    const handler = (e: Event) => {
      const d = (e as CustomEvent<AddNextDetail>).detail;
      if (d) setPicker({ sourceId: d.sourceId, sourceHandle: d.sourceHandle, triggersOnly: false });
    };
    window.addEventListener(ADD_NEXT_EVENT, handler);
    return () => window.removeEventListener(ADD_NEXT_EVENT, handler);
  }, []);

  function onDragStart(event: React.DragEvent, type: string) {
    event.dataTransfer.setData("application/reactflow", type);
    event.dataTransfer.setData("text/plain", type);
    event.dataTransfer.effectAllowed = "move";
  }
  function onDrop(event: React.DragEvent) {
    event.preventDefault();
    const type = event.dataTransfer.getData("application/reactflow") || event.dataTransfer.getData("text/plain");
    if (!type || !isKnownNodeType(type)) return;
    addNode(type, reactFlow.screenToFlowPosition({ x: event.clientX, y: event.clientY }));
  }

  function updateSelectedData(patch: Record<string, unknown>) {
    if (!selectedNodeId) return;
    setNodes((cur) => cur.map((n) => (n.id === selectedNodeId ? { ...n, data: { ...(n.data ?? {}), ...patch } } : n)));
  }
  function deleteSelectedNode() {
    if (!selectedNodeId) return;
    setNodes((cur) => cur.filter((n) => n.id !== selectedNodeId));
    setEdges((cur) => cur.filter((e) => e.source !== selectedNodeId && e.target !== selectedNodeId));
    setSelectedNodeId(null); setDrawerOpen(false);
  }
  function deleteSelectedEdge() {
    if (!selectedEdgeId) return;
    setEdges((cur) => cur.filter((e) => e.id !== selectedEdgeId));
    setSelectedEdgeId(null); setDrawerOpen(false);
  }

  const save = useCallback(async () => {
    if (!detail || saving) return;
    // Inline (registry) validation across the graph before asking the server.
    const localErrors: string[] = [];
    for (const n of nodes) {
      const errs = validateNodeData(n.type ?? "", cleanNodeData(n.data));
      for (const [k, msg] of Object.entries(errs)) localErrors.push(`${nodeDef(n.type ?? "").label}: ${msg} (${k})`);
    }
    if (!nodes.some((n) => nodeDef(n.type ?? "").kind === "trigger")) localErrors.push("Add a trigger node — every workflow starts with exactly one trigger.");
    setSaving(true); setSaveErrors([]); setNotice(null);
    if (localErrors.length > 0) { setSaveErrors(localErrors); setSaving(false); return; }
    try {
      const graph = flowToGraph(nodes, edges);
      const updated = await apiWorkflows.update(workflowId, { name: name.trim(), description: description.trim(), enabled, graph });
      setDetail(updated); setName(updated.name); setDescription(updated.description ?? ""); setEnabled(updated.enabled);
      const { nodes: n, edges: e } = graphToFlow(updated.graph ?? graph);
      if (updated.graph) { setNodes(n); setEdges(e); }
      setSavedSnapshot(snapshotOf(updated.name, updated.description ?? "", updated.enabled, updated.graph ? n : nodes, updated.graph ? e : edges));
      setSavedAt(updated.updated_at ?? new Date().toISOString());
      setNotice("Workflow saved. The engine will run exactly this graph.");
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.validationErrors.length > 0) setSaveErrors(e.validationErrors);
        else if (Object.keys(e.fields).length > 0) setSaveErrors(Object.values(e.fields));
        else setSaveErrors([e.code ? `${e.message} (${e.code})` : e.message]);
      } else setSaveErrors([errMsg(e)]);
    }
    setSaving(false);
  }, [detail, saving, nodes, edges, workflowId, name, description, enabled]);

  // Keyboard: Cmd/Ctrl+S saves, Esc closes drawer / picker.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); void save(); }
      if (e.key === "Escape") { setDrawerOpen(false); setPicker(null); setSelectedNodeId(null); setSelectedEdgeId(null); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [save]);

  async function toggleEnabled(next: boolean) {
    // Workflow-level enable only (no per-node disable in v1). Staged as a
    // dirty change and persisted by Save, so the top bar state is honest.
    setEnabled(next);
  }

  const pickerDefs = useMemo(() => {
    if (!picker) return [];
    let defs = WORKFLOW_NODE_DEFS;
    if (picker.triggersOnly) defs = defs.filter((d) => d.kind === "trigger");
    else if (picker.sourceId) defs = defs.filter((d) => d.kind !== "trigger");
    return defs;
  }, [picker]);

  const paletteDefs = useMemo(() => {
    const q = paletteQuery.trim().toLowerCase();
    return WORKFLOW_NODE_DEFS.filter((d) => !q || d.label.toLowerCase().includes(q) || d.type.toLowerCase().includes(q) || d.description.toLowerCase().includes(q));
  }, [paletteQuery]);

  const lastStep = selectedNode ? lastStepsByNode[selectedNode.id] : undefined;
  const savedLabel = dirty ? "Unsaved changes" : savedAt ? `Saved · ${new Date(savedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "Saved";

  return (
    <FebShell
      session={session}
      title="Workflow editor"
      subtitle="Build on the canvas — the saved graph JSON is the only truth and the canvas is its projection. There are no approve, purchase, or pay nodes by design; use a Wait for approval gate instead."
      backHref="/dashboard/workflows"
      backLabel="Workflows"
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={dirty ? "sand" : "sage"}>{savedLabel}</Badge>
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={loading ? "animate-spin" : undefined} /> Reload
          </Button>
          <Button size="sm" onClick={() => void save()} disabled={saving || loading || !detail}>
            <Save /> {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      }
    >
      {loading && <SectionLoading label="Loading workflow…" />}
      {error && <SectionError message={error} onRetry={() => void load()} />}
      {notice && <PageNotice message={notice} />}
      {saveErrors.length > 0 && (
        <div role="alert" className="grid gap-1.5 rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3">
          <p className="flex items-center gap-2 text-sm font-extrabold text-coral-300"><TriangleAlert className="size-4" /> This graph was not saved</p>
          {saveErrors.map((msg, i) => <p key={i} className="text-sm font-bold text-coral-300">{msg}</p>)}
        </div>
      )}

      {detail && !loading && (
        <>
          <Card className="grid gap-4 p-5">
            <div className="grid gap-4 md:grid-cols-2">
              <label className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">Name</span>
                <Input value={name} onChange={(e) => setName(e.target.value)} aria-label="Workflow name" />
              </label>
              <label className="grid gap-1.5">
                <span className="text-sm font-extrabold text-sage-400">Description</span>
                <Input value={description} onChange={(e) => setDescription(e.target.value)} aria-label="Workflow description" placeholder="What this automation does" />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2.5 text-sm font-bold text-sand-100">
                <input type="checkbox" checked={enabled} onChange={(e) => void toggleEnabled(e.target.checked)} className="size-5 shrink-0 accent-coral-500" aria-label="Workflow enabled" />
                Enabled — the engine runs this workflow on its trigger (applied on Save)
              </label>
              <Badge tone={enabled ? "sage" : "neutral"}>{enabled ? "Enabled" : "Disabled"}</Badge>
              <span className="flex items-center gap-1.5 text-xs font-semibold text-sage-400">
                <Clock className="size-3.5" /> {savedLabel} · {nodes.length} nodes · {edges.length} edges · Press Ctrl/Cmd+S to save
              </span>
            </div>
            {!enabled && <p className="text-sm font-semibold text-sage-400">This workflow is disabled. Its nodes still render normally in the editor — disabling is workflow-level only; there is no per-node disable in this version.</p>}
          </Card>

          <div className="grid gap-4 xl:grid-cols-[230px_minmax(0,1fr)]">
            {/* Palette: search pinned, collapsible categories */}
            <Card className="grid content-start gap-3 p-4">
              <p className="text-sm font-extrabold text-sand-100">Palette</p>
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-sage-400" />
                <Input value={paletteQuery} onChange={(e) => setPaletteQuery(e.target.value)} placeholder="Search nodes" aria-label="Search palette" className="pl-9" />
              </div>
              <p className="-mt-1 text-xs font-semibold text-sage-400">Click to add, drag onto the canvas, or use the + on a node to append the next step pre-connected.</p>
              {PALETTE_GROUPS.map((group) => {
                const defs = paletteDefs.filter((d) => d.group === group);
                if (defs.length === 0) return null;
                const collapsed = !!collapsedGroups[group];
                return (
                  <div key={group} className="grid gap-2">
                    <button type="button" onClick={() => setCollapsedGroups((c) => ({ ...c, [group]: !c[group] }))} aria-expanded={!collapsed} className="flex items-center justify-between text-left">
                      <span className="text-[11px] font-extrabold uppercase tracking-widest text-sage-500">{GROUP_LABELS[group]}</span>
                      <ChevronDown className={cn("size-4 text-sage-400 transition-transform", collapsed && "-rotate-90")} />
                    </button>
                    {!collapsed && defs.map((def) => (
                      <button key={def.type} type="button" draggable onDragStart={(e) => onDragStart(e, def.type)} onClick={() => addNode(def.type)}
                        className="grid gap-1 rounded-2xl border-2 border-ink-600 bg-ink-900/60 px-3 py-2.5 text-left transition-colors hover:border-sage-500 hover:bg-ink-800"
                        title={`${def.label} — click to add or drag onto the canvas`}>
                        <span className="flex items-center gap-2">
                          <span className={cn("inline-flex rounded-full px-2 py-0.5 text-[10px] font-extrabold uppercase tracking-wide", kindChipClass(def.kind))}>{kindLabel(def.kind)}</span>
                          <span className="text-sm font-extrabold text-sand-100">{def.label}</span>
                        </span>
                        <span className="text-xs font-medium leading-snug text-sage-400">{def.description}</span>
                      </button>
                    ))}
                  </div>
                );
              })}
              {paletteDefs.length === 0 && <p className="text-sm font-semibold text-sage-400">No nodes match that search.</p>}
            </Card>

            {/* Canvas + drawer */}
            <Card className="relative overflow-hidden p-0">
              <div ref={wrapperRef} className="paysilo-flow relative h-[640px] w-full" onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }} onDrop={onDrop} data-testid="workflow-canvas">
                <ReactFlow
                  nodes={nodes} edges={edges} nodeTypes={nodeTypes}
                  onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect}
                  onNodeClick={(_, node) => { setSelectedNodeId(node.id); setSelectedEdgeId(null); setDrawerOpen(true); }}
                  onNodeDoubleClick={(_, node) => { setSelectedNodeId(node.id); setSelectedEdgeId(null); setDrawerOpen(true); }}
                  onEdgeClick={(_, edge) => { setSelectedEdgeId(edge.id); setSelectedNodeId(null); setDrawerOpen(true); }}
                  onPaneClick={() => { setSelectedNodeId(null); setSelectedEdgeId(null); setDrawerOpen(false); }}
                  deleteKeyCode={["Delete", "Backspace"]}
                  snapToGrid snapGrid={[20, 20]}
                  fitView fitViewOptions={{ padding: 0.25 }}
                >
                  <Background variant={BackgroundVariant.Dots} gap={20} size={1.6} color="#D9B98A" />
                  <Controls showInteractive={false} position="bottom-left" />
                  <MiniMap position="bottom-right" nodeColor={(n) => kindMiniMapColor(nodeDef(n.type ?? "").kind)} maskColor="rgba(251,233,208,0.6)" pannable zoomable />
                </ReactFlow>

                {nodes.length === 0 && (
                  <div className="absolute inset-0 z-10 grid place-items-center bg-[#FFFAF3]/70 p-6">
                    <button type="button" onClick={() => setPicker({ sourceId: null, sourceHandle: null, triggersOnly: true })}
                      className="grid w-full max-w-sm gap-2 rounded-3xl border-2 border-dashed border-[#874F41]/50 bg-white/80 px-6 py-8 text-center hover:border-[#E64833]">
                      <Plus className="mx-auto size-7 text-[#E64833]" />
                      <span className="font-display text-lg font-semibold text-[#11262E]">Add first step</span>
                      <span className="text-sm font-semibold text-[#3D6E7E]">Start with a trigger — every workflow begins with exactly one.</span>
                    </button>
                  </div>
                )}

                {/* Config drawer — overlays the right edge, canvas stays visible */}
                {drawerOpen && (selectedNode || selectedEdge) && (
                  <aside aria-label="Node configuration" className="absolute inset-y-0 right-0 z-20 grid w-[320px] max-w-[92%] content-start gap-4 overflow-y-auto border-l-2 border-ink-600 bg-ink-900 p-4 shadow-2xl">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-sm font-extrabold text-sand-100">Configuration</p>
                      <button type="button" onClick={() => setDrawerOpen(false)} aria-label="Close configuration" className="rounded-full p-1.5 text-sage-300 hover:bg-ink-700 hover:text-sand-100"><X className="size-5" /></button>
                    </div>

                    {selectedEdge && !selectedNode && (
                      <div className="grid gap-3">
                        <p className="text-sm font-semibold text-sage-300">
                          Edge from <span className="font-bold text-sand-100">{nodeDef(nodes.find((n) => n.id === selectedEdge.source)?.type ?? "").label}</span> to{" "}
                          <span className="font-bold text-sand-100">{nodeDef(nodes.find((n) => n.id === selectedEdge.target)?.type ?? "").label}</span>
                          {selectedEdge.sourceHandle ? <> via <span className="font-bold text-sand-100">{selectedEdge.sourceHandle}</span></> : null}.
                        </p>
                        <Button variant="outline" size="sm" onClick={deleteSelectedEdge}><Trash2 /> Delete edge</Button>
                      </div>
                    )}

                    {selectedNode && selectedDef && (
                      <div className="grid gap-4">
                        <div className="grid gap-1">
                          <span className={cn("inline-flex w-fit rounded-full px-2 py-0.5 text-[10px] font-extrabold uppercase tracking-wide", kindChipClass(selectedDef.kind))}>{kindLabel(selectedDef.kind)}</span>
                          <p className="font-display text-base font-semibold text-sand-100">{selectedDef.label}</p>
                          <p className="font-mono text-xs font-semibold text-sage-400">{selectedDef.type}</p>
                          <p className="text-sm font-medium leading-relaxed text-sage-300">{selectedDef.description}</p>
                        </div>

                        {selectedDef.fields.map((f) => (
                          <RegistryField key={f.key} field={f} value={strVal(cleanNodeData(selectedNode.data), f.key)} error={selectedErrors[f.key]} onChange={(v) => updateSelectedData({ [f.key]: v })} />
                        ))}
                        {selectedDef.fields.length === 0 && <p className="text-sm font-semibold text-sage-400">This step needs no configuration — its essence is shown on the card.</p>}
                        {selectedDef.type === "gate.wait_for_approval" && (
                          <p className="rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-3 py-2.5 text-xs font-bold text-sand-200">The run pauses here and shows as Waiting for human approval until a human decides. Nothing is approved automatically.</p>
                        )}
                        {selectedDef.type === "condition.field_check" && (
                          <p className="text-xs font-semibold text-sage-400">Connect the true handle to the next step when the check passes, and the false handle for the other path. A branch with no edge simply ends there.</p>
                        )}
                        {(selectedDef.type === "trigger.schedule") && (
                          <p className="text-xs font-semibold text-sage-400">Monthly schedules use the day of month; weekly schedules use the weekday. Fan out runs the workflow once per employer, or once for the whole organisation.</p>
                        )}

                        {lastStep && (
                          <div className="grid gap-1.5 rounded-2xl border-2 border-ink-600 bg-ink-950/50 p-3">
                            <p className="text-xs font-extrabold uppercase tracking-wider text-sage-400">Last run output</p>
                            <p className="text-sm font-bold text-sand-100">Status: {lastStep.status}</p>
                            {lastStep.detail && <p className="text-sm font-semibold text-sage-300">{lastStep.status.toUpperCase() === "WAITING" ? "Waiting for human approval" : lastStep.detail}</p>}
                            <p className="text-xs font-semibold text-sage-500">{new Date(lastStep.created_at).toLocaleString()}</p>
                          </div>
                        )}

                        <Button variant="outline" size="sm" onClick={deleteSelectedNode}><Trash2 /> Delete node</Button>
                      </div>
                    )}
                  </aside>
                )}
              </div>
              <p className="border-t-2 border-ink-700 bg-ink-900/50 px-4 py-2.5 text-xs font-semibold text-sage-400">
                Drag from a handle to connect, or click the + on a node to append the next step pre-connected. Condition nodes branch on true / false. Double-click a node to configure it; Esc closes the drawer; Delete removes the selection.
              </p>
            </Card>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={() => void save()} disabled={saving}><Save /> {saving ? "Saving…" : "Save workflow"}</Button>
            <Button variant="outline" size="sm" asChild><Link href="/dashboard/workflows/brief">Morning Brief</Link></Button>
            <Button variant="ghost" size="sm" asChild><Link href="/dashboard/workflows"><ArrowLeft /> Back to workflows</Link></Button>
            <span className="text-sm font-semibold text-sage-400">{savedLabel}</span>
          </div>

          {/* Node picker — opened by the + stub, the empty-state tile, pre-connected */}
          {picker && (
            <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/70 p-4" role="dialog" aria-modal="true" aria-label="Add a step">
              <Card className="grid max-h-[85vh] w-full max-w-lg gap-4 overflow-y-auto p-6">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h3 className="font-display text-xl font-semibold text-sand-100">{picker.triggersOnly ? "Add first step — choose a trigger" : "Add next step"}</h3>
                    <p className="mt-0.5 text-sm font-semibold text-sage-400">
                      {picker.sourceId ? <>The new step is wired from <span className="text-sand-100">{nodeDef(nodes.find((n) => n.id === picker.sourceId)?.type ?? "").label}</span>{picker.sourceHandle ? <> via the <span className="text-sand-100">{picker.sourceHandle}</span> branch</> : null}.</> : "Choose the trigger that starts this workflow."}
                    </p>
                  </div>
                  <button type="button" onClick={() => setPicker(null)} aria-label="Close picker" className="rounded-full p-1.5 text-sage-300 hover:bg-ink-700"><X className="size-5" /></button>
                </div>
                {PALETTE_GROUPS.map((group) => {
                  const defs = pickerDefs.filter((d) => d.group === group);
                  if (defs.length === 0) return null;
                  return (
                    <div key={group} className="grid gap-2">
                      <p className="text-[11px] font-extrabold uppercase tracking-widest text-sage-500">{GROUP_LABELS[group]}</p>
                      {defs.map((def) => (
                        <button key={def.type} type="button"
                          onClick={() => {
                            if (picker.sourceId) addConnected(def.type, picker.sourceId, picker.sourceHandle);
                            else addNode(def.type, { x: 260, y: 80 });
                            setPicker(null);
                          }}
                          className="grid gap-1 rounded-2xl border-2 border-ink-600 bg-ink-900/60 px-3 py-2.5 text-left hover:border-sage-500 hover:bg-ink-800">
                          <span className="flex items-center gap-2">
                            <span className={cn("inline-flex rounded-full px-2 py-0.5 text-[10px] font-extrabold uppercase tracking-wide", kindChipClass(def.kind))}>{kindLabel(def.kind)}</span>
                            <span className="text-sm font-extrabold text-sand-100">{def.label}</span>
                          </span>
                          <span className="text-xs font-medium text-sage-400">{def.description}</span>
                        </button>
                      ))}
                    </div>
                  );
                })}
              </Card>
            </div>
          )}
        </>
      )}
    </FebShell>
  );
}

export default function WorkflowEditorPage() {
  return (
    <ReactFlowProvider>
      <EditorInner />
    </ReactFlowProvider>
  );
}
