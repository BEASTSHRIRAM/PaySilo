"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  ArrowLeft, ChevronDown, FileJson, Info, LayoutGrid, Plus, RefreshCw, Save, Search, Trash2, TriangleAlert, X,
} from "lucide-react";
import ReactFlow, {
  Background, BackgroundVariant, Controls, MarkerType, MiniMap, ReactFlowProvider,
  applyEdgeChanges, applyNodeChanges, useReactFlow,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange, type OnConnectStartParams,
} from "reactflow";

import { AppBar } from "@/components/dashboard/app-bar";
import { SectionError, SectionLoading } from "@/components/dashboard/states";
import { useSession, errMsg } from "@/components/feb/use-session";
import { JsonDrawer, type WorkflowEnvelope } from "@/components/workflows/json-drawer";
import { ScheduleFields, type EmployerOption } from "@/components/workflows/schedule-fields";
import { WorkflowNode } from "@/components/workflows/workflow-node";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/select";
import { api, ApiError, apiWorkflows, type WorkflowDetail, type WorkflowGraph, type WorkflowRunStep } from "@/lib/api";
import { layoutGraph, layoutPositions, needsLayout, COL_W, ROW_H } from "@/lib/workflow-layout";
import {
  ADD_NEXT_EVENT, GROUP_LABELS, PALETTE_GROUPS, WORKFLOW_NODE_DEFS,
  cleanNodeData, connectionProblem, defaultDataFor, isKnownNodeType, kindChipClass, kindLabel, kindMiniMapColor,
  newNodeId, nodeDef, validateNodeData, wouldCreateCycle, type AddNextDetail, type ConfigFieldDef, type WorkflowNodeDef,
} from "@/lib/workflows";
import { cn } from "@/lib/utils";

// One custom component registered for every registry type, so the saved graph's `type` values round-trip unchanged.
const baseNodeTypes = Object.fromEntries(WORKFLOW_NODE_DEFS.map((d) => [d.type, WorkflowNode]));
const PAYROLL_ACTIONS = ["action.prepare_payroll_run", "action.revalidate_held_run"];

type RFNode = Node<Record<string, unknown>>;
type RFEdge = Edge;

const textareaClass =
  "flex w-full rounded-2xl border-2 border-ink-600 bg-ink-900/80 px-4 py-3 text-[15px] font-semibold text-sand-100 placeholder:font-medium placeholder:text-muted-foreground/60 focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25";

const EDGE_LABEL_STYLE = { fill: "#244855", fontWeight: 800, fontSize: 11 };
const EDGE_LABEL_BG = { fill: "#FBE9D0", fillOpacity: 0.95 };

function edgeProps(sourceHandle?: string | null) {
  const base = {
    type: "default" as const, interactionWidth: 28,
    style: { stroke: "#244855", strokeWidth: 1.8 },
    markerEnd: { type: MarkerType.ArrowClosed, color: "#244855", width: 18, height: 18 },
  };
  return sourceHandle === "true" || sourceHandle === "false"
    ? { ...base, label: sourceHandle === "true" ? "Yes" : "No", labelStyle: EDGE_LABEL_STYLE, labelBgStyle: EDGE_LABEL_BG, labelBgPadding: [6, 3] as [number, number], labelBgBorderRadius: 999 }
    : base;
}

/** Graph JSON -> React Flow projection. Older (vertical) saved graphs are tidied left to right. */
function graphToFlow(input: WorkflowGraph | undefined | null): { nodes: RFNode[]; edges: RFEdge[] } {
  const graph = input && needsLayout(input) ? layoutGraph(input) : input;
  const nodes: RFNode[] = (graph?.nodes ?? []).map((n, i) => ({
    id: n.id, type: n.type,
    position: n.position ?? { x: 40 + i * COL_W, y: 60 },
    data: { ...(n.data ?? {}), __editable: true },
  }));
  const edges: RFEdge[] = (graph?.edges ?? []).map((e, i) => ({
    id: e.id || `edge_${e.source}_${e.target}_${i}`,
    source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? undefined,
    ...edgeProps(e.sourceHandle),
  }));
  return { nodes, edges };
}

function flowToGraph(nodes: RFNode[], edges: RFEdge[]): WorkflowGraph {
  const kinds = new Map(nodes.map((n) => [n.id, nodeDef(n.type ?? "").kind]));
  return {
    layout: "lr",
    nodes: nodes.map((n) => ({ id: n.id, type: n.type ?? "", position: { x: Math.round(n.position.x), y: Math.round(n.position.y) }, data: cleanNodeData(n.data) })),
    // Only a condition's outputs carry a handle (true / false); every other node has a single output.
    edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target, sourceHandle: kinds.get(e.source) === "condition" ? (e.sourceHandle ?? null) : null })),
  };
}

function snapshotOf(name: string, description: string, enabled: boolean, nodes: RFNode[], edges: RFEdge[]): string {
  return JSON.stringify({ name, description, enabled, graph: flowToGraph(nodes, edges) });
}

const strVal = (data: Record<string, unknown>, key: string) =>
  typeof data[key] === "string" ? (data[key] as string) : data[key] == null ? "" : String(data[key]);

function useMedia(query: string): boolean {
  const [match, setMatch] = useState(false);
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setMatch(m.matches);
    on();
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [query]);
  return match;
}

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

function PaletteItem({ def, onAdd, onDragStart }: { def: WorkflowNodeDef; onAdd: () => void; onDragStart?: (e: React.DragEvent) => void }) {
  return (
    <button type="button" draggable={!!onDragStart} onDragStart={onDragStart} onClick={onAdd}
      className="grid gap-0.5 rounded-xl border-2 border-ink-600 bg-ink-900/60 px-3 py-2 text-left transition-colors hover:border-sage-500 hover:bg-ink-800"
      title={`${def.label}: ${def.description}`}>
      <span className="flex items-center gap-2">
        <span className={cn("inline-flex rounded-full px-1.5 py-0.5 text-[9px] font-extrabold uppercase tracking-wide", kindChipClass(def.kind))}>{kindLabel(def.kind)}</span>
        <span className="truncate text-sm font-extrabold text-sand-100">{def.label}</span>
      </span>
      <span className="line-clamp-2 text-[11px] font-medium leading-snug text-sage-400">{def.description}</span>
    </button>
  );
}

interface PickerState { sourceId: string | null; sourceHandle: string | null; triggersOnly: boolean }

function EditorInner() {
  const params = useParams<{ id: string }>();
  const workflowId = params.id;
  const { session } = useSession(`/dashboard/workflows/${workflowId}`);
  const reactFlow = useReactFlow();
  const isLg = useMedia("(min-width: 1024px)");
  const isXl = useMedia("(min-width: 1280px)");

  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [nodes, setNodes] = useState<RFNode[]>([]);
  const [edges, setEdges] = useState<RFEdge[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [sheetInspector, setSheetInspector] = useState(false);
  const [sheetPalette, setSheetPalette] = useState(false);
  const [jsonOpen, setJsonOpen] = useState(false);
  const [employers, setEmployers] = useState<EmployerOption[]>([]);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveErrors, setSaveErrors] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState<string>("");
  const [savedAt, setSavedAt] = useState<string | null>(null);

  const [paletteQuery, setPaletteQuery] = useState("");
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});
  const [picker, setPicker] = useState<PickerState | null>(null);
  const [lastStepsByNode, setLastStepsByNode] = useState<Record<string, WorkflowRunStep>>({});

  const connectStart = useRef<OnConnectStartParams | null>(null);
  const connectMade = useRef(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 9000);
  }, []);
  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current); }, []);

  const currentSnapshot = useMemo(() => (detail ? snapshotOf(name, description, enabled, nodes, edges) : ""), [detail, name, description, enabled, nodes, edges]);
  const dirty = !!detail && savedSnapshot !== "" && currentSnapshot !== savedSnapshot;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const wf = await apiWorkflows.get(workflowId);
      setDetail(wf); setName(wf.name); setDescription(wf.description ?? ""); setEnabled(wf.enabled);
      const { nodes: n, edges: e } = graphToFlow(wf.graph);
      setNodes(n); setEdges(e);
      setSelectedNodeId(null); setSelectedEdgeId(null);
      setSavedSnapshot(snapshotOf(wf.name, wf.description ?? "", wf.enabled, n, e));
      setSavedAt(wf.updated_at ?? null);
      setError(null);
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
  useEffect(() => {
    api.overview().then((o) => setEmployers(o.employers.map((e) => ({ id: e.id, name: e.name })))).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!loading && nodes.length > 0) { const t = setTimeout(() => reactFlow.fitView({ padding: 0.2, maxZoom: 1 }), 60); return () => clearTimeout(t); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, detail?.id]);

  const nodeTypes = useMemo(() => {
    const extra: Record<string, typeof WorkflowNode> = {};
    for (const n of nodes) if (n.type && !isKnownNodeType(n.type)) extra[n.type] = WorkflowNode;
    return { ...baseNodeTypes, ...extra };
  }, [nodes]);

  // Derived display data: which outputs are connected (hides their "+") and the scoped employer's name.
  const displayNodes = useMemo(() => {
    const out = new Map<string, string[]>();
    for (const e of edges) {
      const kind = nodeDef(nodes.find((n) => n.id === e.source)?.type ?? "").kind;
      out.set(e.source, [...(out.get(e.source) ?? []), kind === "condition" ? (e.sourceHandle ?? "true") : "out"]);
    }
    return nodes.map((n) => {
      const extra: Record<string, unknown> = { __out: out.get(n.id) ?? [] };
      if (n.type === "trigger.schedule" && n.data?.employer_id) extra.__employerName = employers.find((e) => e.id === n.data.employer_id)?.name;
      return { ...n, data: { ...n.data, ...extra } };
    });
  }, [nodes, edges, employers]);

  const selectedNode = useMemo(() => nodes.find((n) => n.id === selectedNodeId) ?? null, [nodes, selectedNodeId]);
  const selectedEdge = useMemo(() => edges.find((e) => e.id === selectedEdgeId) ?? null, [edges, selectedEdgeId]);
  const selectedDef = selectedNode ? nodeDef(selectedNode.type ?? "") : null;
  const selectedErrors = selectedNode ? validateNodeData(selectedNode.type ?? "", cleanNodeData(selectedNode.data)) : {};
  const hasPayrollAction = nodes.some((n) => PAYROLL_ACTIONS.includes(n.type ?? ""));
  const triggerNode = nodes.find((n) => nodeDef(n.type ?? "").kind === "trigger");

  const onNodesChange = useCallback((changes: NodeChange[]) => setNodes((cur) => applyNodeChanges(changes, cur)), []);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges((cur) => applyEdgeChanges(changes, cur));
    for (const c of changes) if (c.type === "remove") setSelectedEdgeId((id) => (id === c.id ? null : id));
  }, []);

  /** Validate then add an edge; every refusal is a short friendly toast that says what to do instead. */
  const tryConnect = useCallback((sourceId: string, targetId: string, sourceHandle: string | null | undefined): boolean => {
    const source = nodes.find((n) => n.id === sourceId);
    const target = nodes.find((n) => n.id === targetId);
    if (!source || !target) return false;
    if (sourceId === targetId) { showToast("A step can't connect to itself. Drag to a different step's left-hand circle."); return false; }
    const problem = connectionProblem(source.type ?? "", target.type ?? "", sourceHandle);
    if (problem) { showToast(problem); return false; }
    const isCondition = nodeDef(source.type ?? "").kind === "condition";
    const handle = isCondition ? (sourceHandle ?? "true") : null;
    if (edges.some((e) => e.source === sourceId && e.target === targetId && (isCondition ? (e.sourceHandle ?? "true") === handle : true))) {
      showToast("These two steps are already connected. Click the line and press Delete if you want to remove it.");
      return false;
    }
    if (wouldCreateCycle(edges, sourceId, targetId)) {
      showToast("That would make the steps loop back on themselves. A workflow only runs forward: connect to a step further to the right.");
      return false;
    }
    setEdges((cur) => [...cur, { id: `edge_${sourceId}_${targetId}_${handle ?? "out"}_${Date.now().toString(36)}`, source: sourceId, target: targetId, sourceHandle: handle ?? undefined, ...edgeProps(handle) }]);
    setToast(null);
    return true;
  }, [nodes, edges, showToast]);

  const onConnect = useCallback((conn: Connection) => {
    connectMade.current = true;
    if (conn.source && conn.target) tryConnect(conn.source, conn.target, conn.sourceHandle);
  }, [tryConnect]);

  const onConnectStart = useCallback((_: unknown, params: OnConnectStartParams) => {
    connectStart.current = params; connectMade.current = false;
  }, []);
  const onConnectEnd = useCallback((event: MouseEvent | TouchEvent) => {
    const start = connectStart.current;
    connectStart.current = null;
    if (!start?.nodeId || connectMade.current) return;
    const el = (event.target as HTMLElement | null)?.closest?.(".react-flow__node") as HTMLElement | null;
    const targetId = el?.getAttribute("data-id");
    if (targetId && start.handleType === "source") {
      const source = nodes.find((n) => n.id === start.nodeId);
      const target = nodes.find((n) => n.id === targetId);
      const problem = source && target ? connectionProblem(source.type ?? "", target.type ?? "", start.handleId) : null;
      showToast(problem ?? "To connect, drop on the small circle on the LEFT edge of the next step.");
    } else if (!targetId && start.handleType === "source") {
      showToast("Drop the line on the circle on the left edge of another step to connect them.");
    }
  }, [nodes, showToast]);

  const addNodeAt = useCallback((type: string, position: { x: number; y: number }): string => {
    const id = newNodeId(type);
    setNodes((cur) => [...cur, { id, type, position, data: { ...defaultDataFor(type), __editable: true } }]);
    return id;
  }, []);

  const triggerGuard = useCallback((type: string): boolean => {
    if (nodeDef(type).kind === "trigger" && triggerNode) {
      showToast(`This workflow already has a trigger (${nodeDef(triggerNode.type ?? "").label}). A workflow starts from exactly one trigger: remove it first, or build the second one as its own workflow.`);
      return true;
    }
    return false;
  }, [triggerNode, showToast]);

  function openInspector() { if (!isXl) setSheetInspector(true); }

  /** Palette click: appended right of the rightmost step, auto-connected from the selected step when that is allowed. */
  function addNode(type: string, position?: { x: number; y: number }) {
    if (triggerGuard(type)) return;
    const rightmost = nodes.reduce((m, n) => Math.max(m, n.position.x), -COL_W + 40);
    const sel = selectedNode && !position ? selectedNode : null;
    const def = nodeDef(type);
    if (sel && def.kind !== "trigger" && !connectionProblem(sel.type ?? "", type)) {
      const handle = nodeDef(sel.type ?? "").kind === "condition" ? (edges.some((e) => e.source === sel.id && (e.sourceHandle ?? "true") === "true") ? "false" : "true") : null;
      addConnected(type, sel.id, handle);
    } else {
      const pos = position ?? { x: rightmost + COL_W, y: 60 + (nodes.length ? 0 : 0) };
      const id = addNodeAt(type, pos);
      setSelectedNodeId(id); setSelectedEdgeId(null); openInspector(); setNotice(null);
    }
    setSheetPalette(false);
  }

  /** Append a node pre-connected from a source node's output (the + button / picker). */
  function addConnected(type: string, sourceId: string, sourceHandle: string | null) {
    const source = nodes.find((n) => n.id === sourceId);
    if (!source) return;
    if (triggerGuard(type)) return;
    const siblings = edges.filter((e) => e.source === sourceId).length;
    const pos = { x: source.position.x + COL_W, y: source.position.y + siblings * ROW_H };
    const id = addNodeAt(type, pos);
    const isCondition = nodeDef(source.type ?? "").kind === "condition";
    const handle = isCondition ? (sourceHandle ?? "true") : null;
    setEdges((cur) => [...cur, { id: `edge_${sourceId}_${id}_${handle ?? "out"}_${Date.now().toString(36)}`, source: sourceId, target: id, sourceHandle: handle ?? undefined, ...edgeProps(handle) }]);
    setSelectedNodeId(id); setSelectedEdgeId(null); openInspector();
  }

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
    setNodes((cur) => cur.map((n) => {
      if (n.id !== selectedNodeId) return n;
      const next = { ...(n.data ?? {}), ...patch };
      for (const [k, v] of Object.entries(patch)) if (v === undefined) delete next[k];
      return { ...n, data: next };
    }));
  }
  function deleteSelectedNode() {
    if (!selectedNodeId) return;
    setNodes((cur) => cur.filter((n) => n.id !== selectedNodeId));
    setEdges((cur) => cur.filter((e) => e.source !== selectedNodeId && e.target !== selectedNodeId));
    setSelectedNodeId(null); setSheetInspector(false);
  }
  function deleteSelectedEdge() {
    if (!selectedEdgeId) return;
    setEdges((cur) => cur.filter((e) => e.id !== selectedEdgeId));
    setSelectedEdgeId(null); setSheetInspector(false);
  }
  function tidyLayout() {
    const pos = layoutPositions(flowToGraph(nodes, edges));
    setNodes((cur) => cur.map((n) => ({ ...n, position: pos[n.id] ?? n.position })));
    setTimeout(() => reactFlow.fitView({ padding: 0.2, maxZoom: 1, duration: 300 }), 50);
  }

  function importEnvelope(env: WorkflowEnvelope) {
    const { nodes: n, edges: e } = graphToFlow(env.graph);
    setNodes(n); setEdges(e);
    setName(env.name); setDescription(env.description ?? "");
    setSelectedNodeId(null); setSelectedEdgeId(null); setSheetInspector(false); setJsonOpen(false);
    setSaveErrors([]);
    setNotice("Imported into the canvas. It is NOT saved yet: review it, then press Save (Reload discards it).");
    setTimeout(() => reactFlow.fitView({ padding: 0.2, maxZoom: 1, duration: 300 }), 80);
  }

  const save = useCallback(async () => {
    if (!detail || saving) return;
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

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); void save(); }
      if (e.key === "Escape") { setSheetInspector(false); setSheetPalette(false); setPicker(null); setSelectedNodeId(null); setSelectedEdgeId(null); setToast(null); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [save]);

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
  const envelope: WorkflowEnvelope = { name, description, graph: flowToGraph(nodes, edges) };

  // ---------------------------------------------------------------- palette content (column or bottom sheet)
  const paletteBody = (
    <>
      <div className="relative shrink-0">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-sage-400" />
        <Input value={paletteQuery} onChange={(e) => setPaletteQuery(e.target.value)} placeholder="Search steps" aria-label="Search palette" className="h-10 pl-9" />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pr-1" data-testid="palette-scroll">
        <div className="grid gap-3 pb-2">
          {PALETTE_GROUPS.map((group) => {
            const defs = paletteDefs.filter((d) => d.group === group);
            if (defs.length === 0) return null;
            const collapsed = !!collapsedGroups[group];
            return (
              <div key={group} className="grid gap-1.5">
                <button type="button" onClick={() => setCollapsedGroups((c) => ({ ...c, [group]: !c[group] }))} aria-expanded={!collapsed} className="flex items-center justify-between text-left">
                  <span className="text-[11px] font-extrabold uppercase tracking-widest text-sage-500">{GROUP_LABELS[group]}</span>
                  <ChevronDown className={cn("size-4 text-sage-400 transition-transform", collapsed && "-rotate-90")} />
                </button>
                {!collapsed && defs.map((def) => (
                  <PaletteItem key={def.type} def={def} onAdd={() => addNode(def.type)} onDragStart={isLg ? (e) => onDragStart(e, def.type) : undefined} />
                ))}
              </div>
            );
          })}
          {paletteDefs.length === 0 && <p className="text-sm font-semibold text-sage-400">No steps match that search.</p>}
        </div>
      </div>
    </>
  );

  // ---------------------------------------------------------------- inspector content (column or bottom sheet)
  const inspectorBody = (
    <div className="grid content-start gap-4">
      {!selectedNode && !selectedEdge && (
        <div className="grid gap-2 text-sm font-semibold text-sage-400">
          <p className="font-extrabold text-sand-100">Nothing selected</p>
          <p>Click a step to configure it, or click a line to remove the connection.</p>
          <p>{nodes.length} steps · {edges.length} connections</p>
        </div>
      )}
      {selectedEdge && !selectedNode && (
        <div className="grid gap-3">
          <p className="text-sm font-semibold text-sage-300">
            Connection from <span className="font-bold text-sand-100">{nodeDef(nodes.find((n) => n.id === selectedEdge.source)?.type ?? "").label}</span> to{" "}
            <span className="font-bold text-sand-100">{nodeDef(nodes.find((n) => n.id === selectedEdge.target)?.type ?? "").label}</span>
            {selectedEdge.sourceHandle ? <> on the <span className="font-bold text-sand-100">{selectedEdge.sourceHandle === "true" ? "Yes" : "No"}</span> output</> : null}.
          </p>
          <Button variant="outline" size="sm" onClick={deleteSelectedEdge}><Trash2 /> Delete connection</Button>
          <p className="text-xs font-semibold text-sage-500">Tip: with the line selected you can also press Delete or Backspace.</p>
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

          {selectedDef.type === "trigger.schedule" ? (
            <ScheduleFields data={selectedNode.data ?? {}} onChange={updateSelectedData} employers={employers} requireEmployer={hasPayrollAction} />
          ) : (
            <>
              {selectedDef.fields.map((f) => (
                <RegistryField key={f.key} field={f} value={strVal(cleanNodeData(selectedNode.data), f.key)} error={selectedErrors[f.key]} onChange={(v) => updateSelectedData({ [f.key]: v })} />
              ))}
              {selectedDef.fields.length === 0 && <p className="text-sm font-semibold text-sage-400">This step needs no configuration: its essence is shown on the card.</p>}
            </>
          )}
          {selectedDef.type === "gate.wait_for_approval" && (
            <p className="rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-3 py-2.5 text-xs font-bold text-sand-200">The run pauses here and shows as Waiting until a human decides on the real screen. Nothing is approved automatically.</p>
          )}
          {selectedDef.type === "action.prepare_payroll_run" && (
            <p className="rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-3 py-2.5 text-xs font-bold text-sand-200">Prepares the employer&apos;s run (priced and reserved) and stops. It never approves or pays: add a Wait for approval step after it.</p>
          )}
          {selectedDef.type === "condition.field_check" && (
            <p className="text-xs font-semibold text-sage-400">Drag the <b>Yes</b> output to the step that follows when the check passes and the <b>No</b> output to the other path. An output with no line simply ends there.</p>
          )}
          {lastStep && (
            <div className="grid gap-1.5 rounded-2xl border-2 border-ink-600 bg-ink-950/50 p-3">
              <p className="text-xs font-extrabold uppercase tracking-wider text-sage-400">Last run output</p>
              <p className="text-sm font-bold text-sand-100">Status: {lastStep.status}</p>
              {lastStep.detail && <p className="text-sm font-semibold text-sage-300">{lastStep.status.toUpperCase() === "WAITING" ? "Waiting for human approval" : lastStep.detail}</p>}
              <p className="text-xs font-semibold text-sage-500">{new Date(lastStep.created_at).toLocaleString()}</p>
            </div>
          )}
          <Button variant="outline" size="sm" onClick={deleteSelectedNode}><Trash2 /> Delete step</Button>
        </div>
      )}
    </div>
  );

  const inspectorOpenOnSheet = !isXl && sheetInspector && (selectedNode || selectedEdge);

  return (
    <div>
      {session ? <AppBar session={session} /> : <div className="h-6" />}
      <div className="mx-auto flex h-[calc(100dvh-14.5rem)] min-h-[420px] max-w-[1600px] flex-col gap-2.5 px-3 sm:px-5 lg:h-[calc(100dvh-8.5rem)]" data-testid="editor-shell">
        {/* ---- compact header row */}
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Link href="/dashboard/workflows" aria-label="Back to workflows" title="Workflows" className="grid size-10 shrink-0 place-items-center rounded-xl text-sage-300 hover:bg-ink-700 hover:text-sand-100"><ArrowLeft className="size-5" /></Link>
          <Input value={name} onChange={(e) => setName(e.target.value)} aria-label="Workflow name" placeholder="Workflow name" className="h-10 min-w-0 flex-1 basis-40 sm:max-w-xs" disabled={!detail} />
          <Input value={description} onChange={(e) => setDescription(e.target.value)} aria-label="Workflow description" placeholder="What this automation does" className="hidden h-10 min-w-0 flex-[2] md:block" disabled={!detail} />
          <label className="flex shrink-0 cursor-pointer items-center gap-2 rounded-xl border-2 border-ink-600 bg-ink-850/60 px-3 py-1.5 text-sm font-bold text-sand-100">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="size-5 shrink-0 accent-coral-500" aria-label="Workflow enabled" disabled={!detail} />
            {enabled ? "Enabled" : "Disabled"}
          </label>
          <Badge tone={dirty ? "sand" : "sage"} className="hidden sm:inline-flex">{savedLabel}</Badge>
          {detail?.drafted_by_agent && <Badge tone="warning" className="hidden lg:inline-flex" title="Silo drafted this. Review every step before you enable it.">Drafted by Silo</Badge>}
          <Button variant="outline" size="sm" onClick={() => setJsonOpen(true)} disabled={!detail} aria-label="View or edit JSON"><FileJson /> <span className="hidden sm:inline">JSON</span></Button>
          <Button variant="outline" size="sm" onClick={tidyLayout} disabled={!detail || nodes.length === 0} aria-label="Tidy layout left to right" title="Tidy layout (left to right)"><LayoutGrid /></Button>
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading} aria-label="Reload from saved" title="Reload from saved"><RefreshCw className={loading ? "animate-spin" : undefined} /></Button>
          <Button size="sm" onClick={() => void save()} disabled={saving || loading || !detail}><Save /> {saving ? "Saving…" : "Save"}</Button>
        </div>

        {(notice || saveErrors.length > 0 || detail?.drafted_by_agent && !enabled) && (
          <div className="grid shrink-0 gap-1.5">
            {notice && <p role="status" className="rounded-xl border-2 border-sage-600/60 bg-sage-500/10 px-3 py-1.5 text-sm font-bold text-sage-300">{notice}</p>}
            {detail?.drafted_by_agent && !enabled && !notice && <p className="rounded-xl border-2 border-sand-400/40 bg-sand-300/10 px-3 py-1.5 text-sm font-bold text-sand-200">Silo drafted this workflow and left it disabled. Review each step, adjust it, then switch it on and Save.</p>}
            {saveErrors.length > 0 && (
              <div role="alert" className="grid max-h-28 gap-1 overflow-y-auto rounded-xl border-2 border-coral-600/60 bg-coral-500/10 px-3 py-2">
                <p className="flex items-center gap-2 text-sm font-extrabold text-coral-300"><TriangleAlert className="size-4" /> This graph was not saved</p>
                {saveErrors.map((msg, i) => <p key={i} className="text-xs font-bold text-coral-300">{msg}</p>)}
              </div>
            )}
          </div>
        )}
        {loading && <SectionLoading label="Loading workflow…" />}
        {error && <SectionError message={error} onRetry={() => void load()} />}

        {detail && !loading && (
          <div className="grid min-h-0 flex-1 gap-2.5 lg:grid-cols-[250px_minmax(0,1fr)] xl:grid-cols-[250px_minmax(0,1fr)_320px]">
            {/* ---- palette column (lg+) */}
            {isLg && (
              <Card className="flex min-h-0 flex-col gap-2.5 p-3" data-testid="palette">
                <p className="shrink-0 text-sm font-extrabold text-sand-100">Add a step</p>
                {paletteBody}
              </Card>
            )}

            {/* ---- canvas */}
            <Card className="relative min-h-0 overflow-hidden p-0">
              <div className="paysilo-flow relative h-full w-full" onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }} onDrop={onDrop} data-testid="workflow-canvas">
                <ReactFlow
                  nodes={displayNodes} edges={edges} nodeTypes={nodeTypes}
                  onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
                  onConnect={onConnect} onConnectStart={onConnectStart} onConnectEnd={onConnectEnd}
                  connectionRadius={34}
                  onNodeClick={(_, node) => { setSelectedNodeId(node.id); setSelectedEdgeId(null); openInspector(); }}
                  onEdgeClick={(_, edge) => { setSelectedEdgeId(edge.id); setSelectedNodeId(null); openInspector(); }}
                  onPaneClick={() => { setSelectedNodeId(null); setSelectedEdgeId(null); setSheetInspector(false); }}
                  deleteKeyCode={["Delete", "Backspace"]}
                  snapToGrid snapGrid={[20, 20]}
                  fitView fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
                  minZoom={0.3}
                >
                  <Background variant={BackgroundVariant.Dots} gap={20} size={1.6} color="#D9B98A" />
                  <Controls showInteractive={false} position="bottom-left" />
                  {isLg && <MiniMap position="bottom-right" nodeColor={(n) => kindMiniMapColor(nodeDef(n.type ?? "").kind)} maskColor="rgba(251,233,208,0.6)" pannable zoomable />}
                </ReactFlow>

                {toast && (
                  <div role="status" aria-live="polite" data-testid="connect-toast"
                    className="absolute left-1/2 top-3 z-30 flex w-[min(92%,520px)] -translate-x-1/2 items-start gap-2.5 rounded-2xl border-2 border-[#874F41]/60 bg-[#FBE9D0] px-3.5 py-2.5 text-sm font-bold text-[#563129] shadow-lg">
                    <Info className="mt-0.5 size-4 shrink-0" />
                    <span className="min-w-0 flex-1">{toast}</span>
                    <button type="button" onClick={() => setToast(null)} aria-label="Dismiss" className="shrink-0 rounded-full p-0.5 hover:bg-[#874F41]/15"><X className="size-4" /></button>
                  </div>
                )}

                {nodes.length === 0 && (
                  <div className="absolute inset-0 z-10 grid place-items-center bg-[#FFFAF3]/70 p-6">
                    <button type="button" onClick={() => setPicker({ sourceId: null, sourceHandle: null, triggersOnly: true })}
                      className="grid w-full max-w-sm gap-2 rounded-3xl border-2 border-dashed border-[#874F41]/50 bg-white/80 px-6 py-8 text-center hover:border-[#E64833]">
                      <Plus className="mx-auto size-7 text-[#E64833]" />
                      <span className="font-display text-lg font-semibold text-[#11262E]">Add first step</span>
                      <span className="text-sm font-semibold text-[#3D6E7E]">Start with a trigger: every workflow begins with exactly one.</span>
                    </button>
                  </div>
                )}

                {!isLg && (
                  <div className="absolute bottom-3 left-1/2 z-20 -translate-x-1/2">
                    <Button size="sm" onClick={() => setSheetPalette(true)}><Plus /> Add step</Button>
                  </div>
                )}
              </div>
              <p className="pointer-events-none absolute inset-x-0 bottom-0 z-10 hidden truncate bg-[#FFFAF3]/85 px-4 py-1 text-[11px] font-semibold text-[#3D6E7E] sm:block">
                Flow runs left to right. Drag from a step&apos;s right circle to the next step&apos;s left circle. Click a line, then press Delete to remove it. Conditions branch Yes / No.
              </p>
            </Card>

            {/* ---- inspector column (xl+) */}
            {isXl && (
              <Card className="min-h-0 overflow-y-auto p-4" data-testid="inspector" aria-label="Step configuration">
                <p className="mb-3 text-sm font-extrabold text-sand-100">Configuration</p>
                {inspectorBody}
              </Card>
            )}
          </div>
        )}
      </div>

      {/* ---- bottom sheets on small screens */}
      {!isLg && sheetPalette && (
        <div className="fixed inset-0 z-50 flex items-end bg-ink-950/60" role="dialog" aria-modal="true" aria-label="Add a step" onClick={() => setSheetPalette(false)}>
          <div className="flex max-h-[78dvh] w-full flex-col gap-2.5 rounded-t-3xl border-t-2 border-ink-600 bg-ink-900 p-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between"><p className="font-display text-lg font-semibold text-sand-100">Add a step</p>
              <button type="button" onClick={() => setSheetPalette(false)} aria-label="Close" className="rounded-full p-1.5 text-sage-300 hover:bg-ink-700"><X className="size-5" /></button></div>
            {paletteBody}
          </div>
        </div>
      )}
      {inspectorOpenOnSheet && (
        <div className="fixed inset-x-0 bottom-0 z-40 flex max-h-[68dvh] flex-col gap-2 rounded-t-3xl border-t-2 border-ink-600 bg-ink-900 p-4 shadow-2xl" role="dialog" aria-label="Step configuration">
          <div className="flex shrink-0 items-center justify-between"><p className="text-sm font-extrabold text-sand-100">Configuration</p>
            <button type="button" onClick={() => setSheetInspector(false)} aria-label="Close configuration" className="rounded-full p-1.5 text-sage-300 hover:bg-ink-700"><X className="size-5" /></button></div>
          <div className="min-h-0 flex-1 overflow-y-auto pr-1">{inspectorBody}</div>
        </div>
      )}

      <JsonDrawer open={jsonOpen} onClose={() => setJsonOpen(false)} current={envelope} hasContent={nodes.length > 0} onImport={importEnvelope} />

      {/* Node picker: opened by the + button or the empty-state tile, pre-connected */}
      {picker && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/70 p-4" role="dialog" aria-modal="true" aria-label="Add a step">
          <Card className="grid max-h-[85vh] w-full max-w-lg gap-4 overflow-y-auto p-6">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="font-display text-xl font-semibold text-sand-100">{picker.triggersOnly ? "Add first step: choose a trigger" : "Add next step"}</h3>
                <p className="mt-0.5 text-sm font-semibold text-sage-400">
                  {picker.sourceId ? <>The new step is wired from <span className="text-sand-100">{nodeDef(nodes.find((n) => n.id === picker.sourceId)?.type ?? "").label}</span>{picker.sourceHandle ? <> via the <span className="text-sand-100">{picker.sourceHandle === "true" ? "Yes" : "No"}</span> output</> : null}.</> : "Choose the trigger that starts this workflow."}
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
                    <PaletteItem key={def.type} def={def} onAdd={() => {
                      if (picker.sourceId) addConnected(def.type, picker.sourceId, picker.sourceHandle);
                      else addNode(def.type, { x: 40, y: 60 });
                      setPicker(null);
                    }} />
                  ))}
                </div>
              );
            })}
          </Card>
        </div>
      )}
    </div>
  );
}

export default function WorkflowEditorPage() {
  return (
    <ReactFlowProvider>
      <EditorInner />
    </ReactFlowProvider>
  );
}
