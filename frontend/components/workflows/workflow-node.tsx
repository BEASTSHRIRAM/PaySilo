"use client";

import { memo } from "react";
import { Check, Clock, Plus, TriangleAlert } from "lucide-react";
import { Handle, Position, type NodeProps } from "reactflow";

import { ADD_NEXT_EVENT, configSummary, kindChipClass, kindLabel, nodeDef, type AddNextDetail } from "@/lib/workflows";
import { cn } from "@/lib/utils";

export type RunOverlayStatus = "OK" | "FAILED" | "SKIPPED" | "WAITING" | "RESUMED" | "NOT_REACHED";

/**
 * One custom node component for every workflow node type (registry-driven).
 * Two silhouettes only:
 *  - trigger card: sage band on top, NO input handle;
 *  - step card: white PaySilo card, hairline ink border, ink text.
 * Body shows the configured essence in one line (registry summary).
 * Editor nodes carry a "+" stub trailing each output: clicking it opens
 * the node picker pre-connected to that output (dispatches ADD_NEXT_EVENT;
 * internal __ keys are stripped before any graph is saved).
 * In execution mode, data.__runStatus overlays run state: sage check (OK),
 * coral triangle (FAILED), amber pulsing ring (WAITING), grey (SKIPPED /
 * not reached).
 */
export const WorkflowNode = memo(function WorkflowNode({ id, data, type, selected }: NodeProps) {
  const wfType = typeof type === "string" && type ? type : "";
  const def = nodeDef(wfType);
  const record = (data ?? {}) as Record<string, unknown>;
  const summary = configSummary(wfType, record);
  const isTrigger = def.kind === "trigger";
  const isCondition = def.kind === "condition";
  const editable = record.__editable === true;
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
    "nodrag nopan absolute z-10 grid size-6 place-items-center rounded-full border-2 border-[#244855] bg-white text-[#244855] shadow hover:bg-[#E64833] hover:border-[#E64833] hover:text-white transition-colors";

  return (
    <div
      className={cn(
        "relative w-[220px] rounded-2xl border bg-white text-[#11262E] shadow-[0_6px_16px_rgba(17,38,46,0.10)]",
        selected ? "border-[#E64833]" : "border-[rgba(36,72,85,0.28)]",
        runStatus === "WAITING" && "wf-node-waiting border-[#E5BF8C]",
        (runStatus === "SKIPPED" || runStatus === "NOT_REACHED") && "opacity-55 saturate-50",
      )}
    >
      {isTrigger && <div aria-hidden className="h-2 rounded-t-2xl bg-[#90AEAD]" />}
      <div className="px-3.5 py-3">
        {!isTrigger && <Handle type="target" position={Position.Top} />}

        {/* Run-state badge (execution mode only) */}
        {runStatus === "OK" || runStatus === "RESUMED" ? (
          <span className="absolute -right-2 -top-2 grid size-6 place-items-center rounded-full bg-[#5BBF97] text-white shadow" aria-label="Step completed">
            <Check className="size-4" />
          </span>
        ) : runStatus === "FAILED" ? (
          <span className="absolute -right-2 -top-2 grid size-6 place-items-center rounded-full bg-[#E64833] text-white shadow" aria-label="Step failed">
            <TriangleAlert className="size-4" />
          </span>
        ) : runStatus === "WAITING" ? (
          <span className="absolute -right-2 -top-2 grid size-6 place-items-center rounded-full bg-[#E5BF8C] text-[#563129] shadow" aria-label="Waiting for approval">
            <Clock className="size-4" />
          </span>
        ) : null}

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

        {isCondition ? (
          <div className="relative mt-2 h-6">
            <span className="absolute bottom-0 left-[22%] -translate-x-1/2 rounded-full bg-[#FBE9D0] px-1.5 text-[10px] font-extrabold uppercase text-[#874F41]">true</span>
            <span className="absolute bottom-0 left-[78%] -translate-x-1/2 rounded-full bg-[#FBE9D0] px-1.5 text-[10px] font-extrabold uppercase text-[#874F41]">false</span>
            <Handle type="source" position={Position.Bottom} id="true" style={{ left: "22%" }} />
            <Handle type="source" position={Position.Bottom} id="false" style={{ left: "78%" }} />
            {editable && (
              <>
                <button type="button" aria-label="Add step on the true branch" title="Add step (true branch)" onClick={addNext("true")} className={cn(plusBtn, "-bottom-9")} style={{ left: "calc(22% - 12px)" }}>
                  <Plus className="size-3.5" />
                </button>
                <button type="button" aria-label="Add step on the false branch" title="Add step (false branch)" onClick={addNext("false")} className={cn(plusBtn, "-bottom-9")} style={{ left: "calc(78% - 12px)" }}>
                  <Plus className="size-3.5" />
                </button>
              </>
            )}
          </div>
        ) : (
          <>
            <Handle type="source" position={Position.Bottom} />
            {editable && (
              <button type="button" aria-label={`Add next step after ${def.label}`} title="Add next step" onClick={addNext(null)} className={cn(plusBtn, "-bottom-3 left-1/2 -translate-x-1/2")}>
                <Plus className="size-3.5" />
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
});
