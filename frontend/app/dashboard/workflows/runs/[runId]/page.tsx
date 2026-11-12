"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Clock, RefreshCw, Sunrise, X } from "lucide-react";
import ReactFlow, {
  Background, BackgroundVariant, Controls, MiniMap, ReactFlowProvider,
  type Edge, type Node,
} from "reactflow";

import { SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { WorkflowNode } from "@/components/workflows/workflow-node";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  ApiError, apiWorkflows,
  type WorkflowDetail, type WorkflowGraph, type WorkflowRunDetail, type WorkflowRunStep,
} from "@/lib/api";
import { WORKFLOW_NODE_DEFS, isKnownNodeType, kindMiniMapColor, nodeDef } from "@/lib/workflows";
import { cn } from "@/lib/utils";

const baseNodeTypes = Object.fromEntries(WORKFLOW_NODE_DEFS.map((d) => [d.type, WorkflowNode]));
type RFNode = Node<Record<string, unknown>>;
type RFEdge = Edge;

const EDGE_LABEL_STYLE = { fill: "#244855", fontWeight: 800, fontSize: 11 };
const EDGE_LABEL_BG = { fill: "#FBE9D0", fillOpacity: 0.95 };

function stepBranch(step: WorkflowRunStep | undefined): string | null {
  if (!step) return null;
  const out = step.output;
  if (out && typeof out === "object") {
    const b = (out as Record<string, unknown>).branch;
    if (typeof b === "string" && b) return b;
  }
  return null;
}

/** Is this entity a payroll run? The WAITING banner deep-links payroll gates to the Morning Brief. */
function isPayrollEntity(run: WorkflowRunDetail): boolean {
  const k = `${run.entity_kind ?? ""} ${run.trigger_event ?? ""}`.toLowerCase();
  return k.includes("payroll") || k.includes("run");
}

function waitingDeepLink(run: WorkflowRunDetail): { href: string; label: string } | null {
  const kind = (run.entity_kind ?? "").toLowerCase();
  if (isPayrollEntity(run)) return { href: "/dashboard/workflows/brief", label: "Open the Payroll Morning Brief" };
  if (kind.includes("kit")) return { href: "/dashboard/onboarding", label: "Open onboarding" };
  if (kind.includes("trip")) return { href: "/dashboard/travel", label: "Open travel" };
  if (run.entity_id) return { href: `/dashboard/runs/${run.entity_id}`, label: "Open the payroll run" };
  return null;
}

const TERMINAL = ["COMPLETED", "FAILED"];

function RunCanvas() {
  const params = useParams<{ runId: string }>();
  const runId = params.runId;
  const { session } = useSession(`/dashboard/workflows/runs/${runId}`);

  const [run, setRun] = useState<WorkflowRunDetail | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (silent = false) => {
    try {
      const r = await apiWorkflows.run(runId);
      setRun(r);
      try {
        const wf = await apiWorkflows.get(r.workflow_id);
        setWorkflow(wf);
      } catch { /* canvas needs the graph; surface below if missing */ }
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      if (!silent) setError(e instanceof ApiError ? e.message : errMsg(e));
    }
    setLoading(false);
  }, [runId]);

  useEffect(() => { void load(); }, [load]);

  // Poll every ~5s while RUNNING / WAITING; stop on terminal states.
  const runStatus = run?.status?.toUpperCase() ?? "";
  useEffect(() => {
    if (!run || TERMINAL.includes(runStatus)) return;
    const t = setInterval(() => { void load(true); }, 5000);
    return () => clearInterval(t);
  }, [run, runStatus, load]);

  const stepsByNode = useMemo(() => {
    const map = new Map<string, WorkflowRunStep[]>();
    for (const s of run?.steps ?? []) {
      const arr = map.get(s.node_id) ?? [];
      arr.push(s); map.set(s.node_id, arr);
    }
    return map;
  }, [run]);

  const { flowNodes, flowEdges } = useMemo((): { flowNodes: RFNode[]; flowEdges: RFEdge[] } => {
    const graph: WorkflowGraph | undefined = workflow?.graph ?? undefined;
    if (!graph || !run) return { flowNodes: [], flowEdges: [] };
    const executed = new Set((run.steps ?? []).map((s) => s.node_id));
    const nodes: RFNode[] = graph.nodes.map((n, i) => {
      const steps = stepsByNode.get(n.id) ?? [];
      const last = steps[steps.length - 1];
      const status = last ? last.status.toUpperCase() : "NOT_REACHED";
      return {
        id: n.id, type: n.type,
        position: n.position ?? { x: 240, y: 60 + i * 140 },
        data: { ...(n.data ?? {}), __runStatus: status, __editable: false },
        draggable: false, selectable: true,
      };
    });
    // Taken edges: consecutive executed steps, honoring output.branch for conditions.
    const taken = new Set<string>();
    const failedTaken = new Set<string>();
    const ordered = run.steps ?? [];
    for (let idx = 0; idx < ordered.length; idx++) {
      const step = ordered[idx];
      const branch = stepBranch(step);
      const next = ordered[idx + 1];
      for (const e of graph.edges) {
        if (e.source !== step.node_id) continue;
        const branchMatches = branch ? (e.sourceHandle ?? null) === branch : true;
        const nextMatches = next ? e.target === next.node_id : executed.has(e.target);
        if (branchMatches && (branch ? true : nextMatches)) {
          if (step.status.toUpperCase() === "FAILED") failedTaken.add(e.id);
          else taken.add(e.id);
        }
      }
    }
    const edges: RFEdge[] = graph.edges.map((e, i) => {
      const id = e.id || `edge_${e.source}_${e.target}_${i}`;
      const isTaken = taken.has(e.id) || taken.has(id);
      const isFailed = failedTaken.has(e.id) || failedTaken.has(id);
      return {
        id, source: e.source, target: e.target, sourceHandle: e.sourceHandle ?? undefined, type: "default",
        className: isFailed ? "wf-edge-taken-failed" : isTaken ? "wf-edge-taken" : undefined,
        style: { stroke: isFailed ? "#E64833" : isTaken ? "#3A8C6B" : "#244855", strokeWidth: isTaken || isFailed ? 3 : 1.8, opacity: isTaken || isFailed ? 1 : 0.55 },
        ...(e.sourceHandle === "true" || e.sourceHandle === "false" ? { label: e.sourceHandle, labelStyle: EDGE_LABEL_STYLE, labelBgStyle: EDGE_LABEL_BG, labelBgPadding: [6, 3] as [number, number], labelBgBorderRadius: 999 } : {}),
      };
    });
    return { flowNodes: nodes, flowEdges: edges };
  }, [workflow, run, stepsByNode]);

  const nodeTypes = useMemo(() => {
    const extra: Record<string, typeof WorkflowNode> = {};
    for (const n of flowNodes) if (n.type && !isKnownNodeType(n.type)) extra[n.type] = WorkflowNode;
    return { ...baseNodeTypes, ...extra };
  }, [flowNodes]);

  const selectedSteps = selectedNodeId ? (stepsByNode.get(selectedNodeId) ?? []) : [];
  const selectedNodeDef = selectedNodeId && workflow ? nodeDef((workflow.graph?.nodes ?? []).find((n) => n.id === selectedNodeId)?.type ?? "") : null;
  const isWaiting = runStatus === "WAITING";
  const deepLink = run && isWaiting ? waitingDeepLink(run) : null;

  const runTone: BadgeProps["tone"] = runStatus === "COMPLETED" ? "sage" : runStatus === "FAILED" ? "coral" : runStatus === "WAITING" ? "sand" : "neutral";

  return (
    <FebShell
      session={session}
      title={run ? `Run — ${workflow?.name ?? "Workflow"}` : "Workflow run"}
      subtitle="Execution view: the same canvas, read-only, overlaid with what the engine actually did. Only states returned by the server are shown."
      backHref="/dashboard/workflows"
      backLabel="Workflows"
      actions={
        <Button variant="outline" size="sm" onClick={() => { setRefreshing(true); void load().finally(() => setRefreshing(false)); }} disabled={refreshing || loading}>
          <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
        </Button>
      }
    >
      {loading && <SectionLoading label="Loading run…" />}
      {error && <SectionError message={error} onRetry={() => void load()} />}

      {run && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <Badge tone={runTone}>{runStatus}</Badge>
            <span className="text-sm font-semibold text-sage-300">
              {nodeDef(run.trigger_event).label} · {run.entity_label || run.entity_id}
              {run.entity_kind ? ` (${run.entity_kind})` : ""} · Started {new Date(run.started_at).toLocaleString()}
              {run.finished_at ? ` · Finished ${new Date(run.finished_at).toLocaleString()}` : ""}
            </span>
            {!TERMINAL.includes(runStatus) && <span className="text-xs font-bold text-sage-400">Live — refreshing every 5 seconds</span>}
            {workflow && (
              <Button variant="ghost" size="sm" asChild><Link href={`/dashboard/workflows/${workflow.id}`}>Open editor</Link></Button>
            )}
          </div>

          {isWaiting && (
            <div role="status" className="flex flex-wrap items-center gap-3 rounded-2xl border-2 border-sand-400/60 bg-sand-300/15 px-4 py-3">
              <Clock className="size-5 shrink-0 text-sand-300" />
              <p className="flex-1 text-sm font-bold text-sand-100">
                Waiting for approval — {run.entity_label || run.entity_id || "the gated entity"}
              </p>
              {deepLink && (
                deepLink.href === "/dashboard/workflows/brief" ? (
                  <Button size="sm" asChild><Link href={deepLink.href}><Sunrise /> {deepLink.label}</Link></Button>
                ) : (
                  <Button size="sm" variant="outline" asChild><Link href={deepLink.href}>{deepLink.label}</Link></Button>
                )
              )}
            </div>
          )}

          {!workflow && !loading && (
            <SectionError message="This run was loaded, but its workflow graph could not be loaded, so the canvas cannot be drawn." onRetry={() => void load()} />
          )}

          {workflow && (
            <Card className="relative overflow-hidden p-0">
              <div className="paysilo-flow relative h-[560px] w-full" data-testid="workflow-run-canvas">
                <ReactFlow
                  nodes={flowNodes} edges={flowEdges} nodeTypes={nodeTypes}
                  nodesDraggable={false} nodesConnectable={false} elementsSelectable
                  onNodeClick={(_, node) => setSelectedNodeId(node.id)}
                  onPaneClick={() => setSelectedNodeId(null)}
                  deleteKeyCode={null}
                  fitView fitViewOptions={{ padding: 0.25 }}
                >
                  <Background variant={BackgroundVariant.Dots} gap={20} size={1.6} color="#D9B98A" />
                  <Controls showInteractive={false} position="bottom-left" />
                  <MiniMap position="bottom-right" nodeColor={(n) => kindMiniMapColor(nodeDef(n.type ?? "").kind)} maskColor="rgba(251,233,208,0.6)" pannable zoomable />
                </ReactFlow>

                {selectedNodeId && selectedNodeDef && (
                  <aside aria-label="Step detail" className="absolute inset-y-0 right-0 z-20 grid w-[320px] max-w-[92%] content-start gap-3 overflow-y-auto border-l-2 border-ink-600 bg-ink-900 p-4 shadow-2xl">
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <p className="font-display text-base font-semibold text-sand-100">{selectedNodeDef.label}</p>
                        <p className="font-mono text-xs font-semibold text-sage-400">{selectedNodeDef.type}</p>
                      </div>
                      <button type="button" onClick={() => setSelectedNodeId(null)} aria-label="Close step detail" className="rounded-full p-1.5 text-sage-300 hover:bg-ink-700"><X className="size-5" /></button>
                    </div>
                    {selectedSteps.length === 0 ? (
                      <p className="text-sm font-semibold text-sage-400">This step was not reached in this run. No step record was returned for it.</p>
                    ) : selectedSteps.map((s, i) => (
                      <div key={i} className="grid gap-1.5 rounded-2xl border-2 border-ink-600 bg-ink-950/50 p-3">
                        <p className="text-sm font-bold text-sand-100">Status: {s.status}</p>
                        {s.status.toUpperCase() === "WAITING" ? (
                          <p className="flex items-center gap-1.5 text-sm font-bold text-sand-200"><Clock className="size-4" /> Waiting for human approval{s.detail ? <> — {s.detail}</> : null}</p>
                        ) : s.detail ? <p className="text-sm font-semibold text-sage-300">{s.detail}</p> : null}
                        {stepBranch(s) && <p className="text-sm font-semibold text-sage-300">Branch taken: {stepBranch(s)}</p>}
                        {s.output && Object.keys(s.output).length > 0 && (
                          <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs font-semibold text-sage-300">{JSON.stringify(s.output, null, 2)}</pre>
                        )}
                        <p className="text-xs font-semibold text-sage-500">{new Date(s.created_at).toLocaleString()}</p>
                      </div>
                    ))}
                  </aside>
                )}
              </div>

              {/* Timeline strip — steps as segments in order */}
              <div className="border-t-2 border-ink-700 bg-ink-900/60 px-4 py-3">
                <p className="mb-2 text-xs font-extrabold uppercase tracking-wider text-sage-400">Timeline</p>
                {run.steps.length === 0 ? (
                  <p className="text-sm font-semibold text-sage-400">No steps recorded yet.</p>
                ) : (
                  <div className="flex gap-1.5 overflow-x-auto pb-1" role="list" aria-label="Run timeline">
                    {run.steps.map((s, i) => {
                      const up = s.status.toUpperCase();
                      const cls = up === "OK" || up === "RESUMED" ? "border-mint-500 bg-mint-500/25 text-mint-400"
                        : up === "FAILED" ? "border-coral-500 bg-coral-500/20 text-coral-300"
                        : up === "WAITING" ? "border-sand-400 bg-sand-300/20 text-sand-200 border-dashed"
                        : "border-ink-600 bg-ink-800 text-sage-400";
                      return (
                        <button key={i} type="button" role="listitem" onClick={() => setSelectedNodeId(s.node_id)}
                          title={`${nodeDef(s.node_type).label} — ${s.status}${s.detail ? `: ${s.detail}` : ""} — ${new Date(s.created_at).toLocaleString()}`}
                          className={cn("min-w-28 flex-1 rounded-xl border-2 px-2.5 py-2 text-left", cls)}>
                          <span className="block truncate text-xs font-extrabold">{i + 1}. {nodeDef(s.node_type).label}</span>
                          <span className="block text-[11px] font-bold">{up === "WAITING" ? "Waiting for approval" : up}</span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            </Card>
          )}

          <div>
            <Button variant="ghost" size="sm" asChild><Link href="/dashboard/workflows"><ArrowLeft /> Back to workflows and executions</Link></Button>
          </div>
        </>
      )}
    </FebShell>
  );
}

export default function WorkflowRunPage() {
  return (
    <ReactFlowProvider>
      <RunCanvas />
    </ReactFlowProvider>
  );
}
