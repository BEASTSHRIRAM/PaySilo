// Left -> right auto-layout for workflow graphs (longest-path layering, no dependencies).
// Mirrors backend/app/services/workflow_templates.layout_graph so saved graphs and templates look the same.

import type { WorkflowGraph } from "@/lib/api";

export const COL_W = 300;
export const ROW_H = 160;
const X0 = 40;
const Y0 = 60;

export function layoutPositions(graph: Pick<WorkflowGraph, "nodes" | "edges">): Record<string, { x: number; y: number }> {
  const ids = graph.nodes.map((n) => n.id);
  const known = new Set(ids);
  const outgoing: Record<string, { target: string; handle: string | null | undefined }[]> = {};
  const indeg: Record<string, number> = {};
  const parents: Record<string, { source: string; h: number }[]> = {};
  for (const id of ids) { outgoing[id] = []; indeg[id] = 0; parents[id] = []; }
  for (const e of graph.edges) {
    if (!known.has(e.source) || !known.has(e.target) || e.source === e.target) continue;
    outgoing[e.source].push({ target: e.target, handle: e.sourceHandle });
    indeg[e.target] += 1;
    parents[e.target].push({ source: e.source, h: e.sourceHandle === "true" ? 0 : 1 });
  }
  const layer: Record<string, number> = {};
  const roots = ids.filter((i) => indeg[i] === 0);
  const queue = roots.length ? [...roots] : ids.slice(0, 1);
  for (const r of queue) layer[r] = 0;
  const remaining = { ...indeg };
  const seen = new Set<string>();
  while (queue.length) {
    const cur = queue.shift() as string;
    if (seen.has(cur)) continue;
    seen.add(cur);
    const outs = [...outgoing[cur]].sort((a, b) => (a.handle === "true" ? 0 : 1) - (b.handle === "true" ? 0 : 1));
    for (const { target } of outs) {
      layer[target] = Math.max(layer[target] ?? 0, layer[cur] + 1);
      remaining[target] -= 1;
      if (remaining[target] <= 0) queue.push(target);
    }
  }
  for (const id of ids) if (layer[id] === undefined) layer[id] = 0;

  const byLayer = new Map<number, string[]>();
  for (const id of ids) byLayer.set(layer[id], [...(byLayer.get(layer[id]) ?? []), id]);
  const row: Record<string, number> = {};
  for (const ly of [...byLayer.keys()].sort((a, b) => a - b)) {
    const members = byLayer.get(ly) as string[];
    const ordered = ly === 0 ? members : [...members].sort((a, b) => {
      const key = (n: string) => {
        const ps = parents[n].filter((p) => p.source in row).map((p) => row[p.source] + p.h * 0.01);
        return ps.length ? Math.min(...ps) : 1e9;
      };
      return key(a) - key(b) || ids.indexOf(a) - ids.indexOf(b);
    });
    const taken: number[] = [];
    for (const n of ordered) {
      const ps = parents[n].filter((p) => p.source in row).map((p) => row[p.source]);
      let want = ps.length ? Math.min(...ps) : taken.length ? taken[taken.length - 1] + 1 : 0;
      while (taken.some((t) => Math.abs(want - t) < 0.99)) want += 1;
      row[n] = want;
      taken.push(want);
    }
  }
  const out: Record<string, { x: number; y: number }> = {};
  for (const id of ids) out[id] = { x: X0 + layer[id] * COL_W, y: Y0 + Math.round(row[id] * ROW_H) };
  return out;
}

/** Returns the graph with horizontal positions applied. */
export function layoutGraph<T extends WorkflowGraph>(graph: T): T {
  const pos = layoutPositions(graph);
  return { ...graph, layout: "lr", nodes: graph.nodes.map((n) => ({ ...n, position: pos[n.id] ?? n.position })) };
}

/** Saved graphs without the left-to-right marker (older vertical layouts) are tidied on load. */
export function needsLayout(graph: WorkflowGraph | null | undefined): boolean {
  return !!graph && graph.nodes.length > 0 && graph.layout !== "lr";
}
