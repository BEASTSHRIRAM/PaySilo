"use client";

import { memo } from "react";
import { Check, Clock, Plus, TriangleAlert } from "lucide-react";
import { Handle, Position, type NodeProps } from "reactflow";

import { ADD_NEXT_EVENT, configSummary, handlesFor, kindChipClass, kindLabel, nodeDef, type AddNextDetail } from "@/lib/workflows";
import { cn } from "@/lib/utils";

export type RunOverlayStatus = "OK" | "FAILED" | "SKIPPED" | "WAITING" | "RESUMED" | "NOT_REACHED";

/**
 * One custom node component for every workflow node type (registry-driven). The flow reads LEFT -> RIGHT:
 *  - input handle on the LEFT edge (triggers have none: they start the workflow);
 *  - output handle on the RIGHT edge (conditions have two, labelled Yes / No);
 *  - an unconnected output shows a "+" on its right that opens the node picker pre-connected to it
 *    (ADD_NEXT_EVENT; internal __ keys are stripped before any graph is saved).
 * In execution mode data.__runStatus overlays run state (check / failed / waiting / skipped).
 */
export const WorkflowNode = memo(function WorkflowNode({ id, data, type, selected }: NodeProps) {
  const wfType = typeof type === "string" && type ? type : "";
  const def = nodeDef(wfType);
  const record = (data ?? {}) as Record<string, unknown>;
  const summary = configSummary(wfType, record);
  const isTrigger = def.kind === "trigger";
  const handles = handlesFor(def.kind);
  const editable = record.__editable === true;
  const connectedOut = Array.isArray(record.__out) ? (record.__out as string[]) : [];
  const runStatus = typeof record.__runStatus === "string" ? (record.__runStatus as RunOverlayStatus) : null;

  function addNext(sourceHandle: string | null) {
    return (e: React.MouseEvent) => {
      e.stopPropagation();
      e.preventDefault();
      const detail: AddNextDetail = { sourceId: id, sourceHandle };
      window.dispatchEvent(new CustomEvent<AddNextDetail>(ADD_NEXT_EVENT, { detail }));
    };
  }

  const plusBtn =
    "nodrag nopan absolute right-[-46px] top-1/2 z-10 grid size-7 -translate-y-1/2 place-items-center rounded-full border-2 border-[#244855] bg-white text-[#244855] shadow hover:border-[#E64833] hover:bg-[#E64833] hover:text-white transition-colors";

  return (
    <div
      className={cn(
        "relative w-[240px] rounded-2xl border bg-white text-[#11262E] shadow-[0_6px_16px_rgba(17,38,46,0.10)]",
        selected ? "border-[#E64833]" : "border-[rgba(36,72,85,0.28)]",
        runStatus === "WAITING" && "wf-node-waiting border-[#E5BF8C]",
        (runStatus === "SKIPPED" || runStatus === "NOT_REACHED") && "opacity-55 saturate-50",
      )}
    >
      {isTrigger && <div aria-hidden className="h-2 rounded-t-2xl bg-[#90AEAD]" />}

      {/* INPUT: left edge, vertically centred on the card */}
      {handles.input && (
        <Handle type="target" position={Position.Left} id="in" className="wf-handle wf-handle-in" title="Input: drop a connection here" aria-label={`${def.label} input`} />
      )}

      {/* Run-state badge (execution mode only) */}
      {runStatus === "OK" || runStatus === "RESUMED" ? (
        <span className="absolute -right-2 -top-2 z-10 grid size-6 place-items-center rounded-full bg-[#5BBF97] text-white shadow" aria-label="Step completed">
          <Check className="size-4" />
        </span>
      ) : runStatus === "FAILED" ? (
        <span className="absolute -right-2 -top-2 z-10 grid size-6 place-items-center rounded-full bg-[#E64833] text-white shadow" aria-label="Step failed">
          <TriangleAlert className="size-4" />
        </span>
      ) : runStatus === "WAITING" ? (
        <span className="absolute -right-2 -top-2 z-10 grid size-6 place-items-center rounded-full bg-[#E5BF8C] text-[#563129] shadow" aria-label="Waiting for approval">
          <Clock className="size-4" />
        </span>
      ) : null}

      <div className="px-3.5 py-3">
        <div className="flex items-center gap-2">
          <span className={cn("inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[10px] font-extrabold uppercase tracking-wide", kindChipClass(def.kind))}>
            {kindLabel(def.kind)}
          </span>
          <span className="truncate text-[10px] font-bold text-[#6F8F8E]">{wfType}</span>
        </div>
        <p className="mt-1.5 text-sm font-extrabold leading-tight text-[#11262E]">{def.label}</p>
        {summary ? (
          <p className="mt-1 break-words text-xs font-semibold leading-snug text-[#3D6E7E]">{summary}</p>
        ) : (
          <p className="mt-1 line-clamp-2 text-xs font-medium leading-snug text-[#6F8F8E]">{def.description}</p>
        )}
      </div>

      {/* OUTPUTS: right edge. A condition has two rows (Yes / No), each with its own handle. */}
      {def.kind === "condition" ? (
        <div className="border-t border-[rgba(36,72,85,0.12)]">
          {handles.outputs.map((h) => (
            <div key={h.id} className="relative flex h-9 items-center justify-end pr-5">
              <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-extrabold uppercase tracking-wide", h.id === "true" ? "bg-[#DDF3E9] text-[#2C6E54]" : "bg-[#FBE9D0] text-[#874F41]")}>
                {h.label} · {h.id}
              </span>
              <Handle type="source" position={Position.Right} id={h.id} className="wf-handle wf-handle-out" title={`${h.label} output: drag to the next step's input`} aria-label={`${def.label} ${h.label} output`} />
              {editable && !connectedOut.includes(h.id) && (
                <button type="button" aria-label={`Add step on the ${h.label} branch`} title={`Add a step on the ${h.label} branch`} onClick={addNext(h.id)} className={plusBtn}>
                  <Plus className="size-4" />
                </button>
              )}
            </div>
          ))}
        </div>
      ) : (
        <>
          <Handle type="source" position={Position.Right} id="out" className="wf-handle wf-handle-out" title="Output: drag to the next step's input" aria-label={`${def.label} output`} />
          {editable && !connectedOut.includes("out") && (
            <button type="button" aria-label={`Add next step after ${def.label}`} title="Add next step" onClick={addNext(null)} className={plusBtn}>
              <Plus className="size-4" />
            </button>
          )}
        </>
      )}
    </div>
  );
});
