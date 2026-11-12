"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ChevronDown, Clock, Pencil, Play, Plus, RefreshCw, Trash2, Workflow as WorkflowIcon,
} from "lucide-react";

import { PageNotice, SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { FebShell, PickerField } from "@/components/feb/shell";
import { errMsg, useSession } from "@/components/feb/use-session";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/select";
import {
  ApiError, apiWorkflows,
  type WorkflowRun, type WorkflowRunDetail, type WorkflowSummary,
} from "@/lib/api";
import { humanizeTrigger, nodeDef } from "@/lib/workflows";
import { cn } from "@/lib/utils";

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function workflowError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.validationErrors.length > 0) return e.validationErrors.join(" ");
    return e.code ? `${e.message} (${e.code})` : e.message;
  }
  return errMsg(e);
}

const RUN_TONES: Record<string, BadgeProps["tone"]> = {
  WAITING: "sand",
  COMPLETED: "sage",
  FAILED: "coral",
  RUNNING: "neutral",
};

function RunStatusPill({ status }: { status: string }) {
  const upper = status.toUpperCase();
  if (upper === "WAITING") {
    return (
      <Badge tone="sand" className="border-sand-400 bg-sand-300/20 text-sand-200">
        <Clock className="size-3.5" /> Waiting
      </Badge>
    );
  }
  if (upper === "RUNNING") {
    return <Badge tone="neutral" className="border-ink-500 bg-ink-700 text-sand-100">Running</Badge>;
  }
  return <Badge tone={RUN_TONES[upper] ?? "neutral"}>{upper}</Badge>;
}

function StepStatusPill({ status }: { status: string }) {
  const upper = status.toUpperCase();
  const tone: BadgeProps["tone"] =
    upper === "OK" || upper === "RESUMED" ? "sage" : upper === "FAILED" ? "coral" : upper === "WAITING" ? "sand" : "neutral";
  return <Badge tone={tone}>{upper}</Badge>;
}

export default function WorkflowsPage() {
  const { session } = useSession("/dashboard/workflows");
  const router = useRouter();

  const [workflows, setWorkflows] = useState<WorkflowSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [deleteFor, setDeleteFor] = useState<WorkflowSummary | null>(null);
  const [deleting, setDeleting] = useState(false);

  const [runs, setRuns] = useState<WorkflowRun[] | null>(null);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runStatusFilter, setRunStatusFilter] = useState("");
  const [runWorkflowFilter, setRunWorkflowFilter] = useState("");
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<WorkflowRunDetail | null>(null);
  const [runDetailLoading, setRunDetailLoading] = useState(false);
  const [runDetailError, setRunDetailError] = useState<string | null>(null);

  const loadWorkflows = useCallback(async () => {
    try {
      setWorkflows(await apiWorkflows.list());
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setError(workflowError(e));
    }
    setLoading(false);
  }, []);

  const loadRuns = useCallback(async () => {
    setRunsLoading(true);
    try {
      const params: { workflow_id?: string; status?: string } = {};
      if (runWorkflowFilter) params.workflow_id = runWorkflowFilter;
      if (runStatusFilter) params.status = runStatusFilter;
      setRuns(await apiWorkflows.runs(Object.keys(params).length ? params : undefined));
      setRunsError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setRuns(null);
      setRunsError(workflowError(e));
    }
    setRunsLoading(false);
  }, [runStatusFilter, runWorkflowFilter]);

  useEffect(() => { void loadWorkflows(); }, [loadWorkflows]);
  useEffect(() => { void loadRuns(); }, [loadRuns]);

  async function refresh() {
    setRefreshing(true);
    await Promise.all([loadWorkflows(), loadRuns()]);
    setRefreshing(false);
  }

  async function toggleEnabled(wf: WorkflowSummary) {
    setBusyId(wf.id);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await apiWorkflows.update(wf.id, { enabled: !wf.enabled });
      setWorkflows((cur) => (cur ?? []).map((w) => (w.id === updated.id ? { ...w, enabled: updated.enabled, updated_at: updated.updated_at } : w)));
      setNotice(`${updated.name} is now ${updated.enabled ? "enabled" : "disabled"}.`);
    } catch (e) {
      setActionError(workflowError(e));
    }
    setBusyId(null);
  }

  async function createWorkflow() {
    setCreating(true);
    setActionError(null);
    try {
      const created = await apiWorkflows.create({
        name: "Untitled workflow",
        description: "",
        graph: {
          nodes: [
            {
              id: "trigger_1",
              type: "trigger.employer_added",
              position: { x: 240, y: 60 },
              data: {},
            },
          ],
          edges: [],
        },
      });
      router.push(`/dashboard/workflows/${created.id}`);
    } catch (e) {
      setActionError(workflowError(e));
      setCreating(false);
    }
  }

  async function confirmDelete() {
    if (!deleteFor) return;
    setDeleting(true);
    try {
      await apiWorkflows.remove(deleteFor.id);
      setWorkflows((cur) => (cur ?? []).filter((w) => w.id !== deleteFor.id));
      setNotice(`Deleted workflow "${deleteFor.name}".`);
      setDeleteFor(null);
    } catch (e) {
      setActionError(workflowError(e));
    }
    setDeleting(false);
  }

  async function toggleRunDetail(run: WorkflowRun) {
    if (expandedRunId === run.id) {
      setExpandedRunId(null);
      setRunDetail(null);
      return;
    }
    setExpandedRunId(run.id);
    setRunDetail(null);
    setRunDetailError(null);
    setRunDetailLoading(true);
    try {
      setRunDetail(await apiWorkflows.run(run.id));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      setRunDetailError(workflowError(e));
    }
    setRunDetailLoading(false);
  }

  return (
    <FebShell
      session={session}
      title="Workflows"
      subtitle="Automations that react to PaySilo events. Build them on a canvas — what you save is what the engine runs. Workflows can prepare work and send notices, but they can never approve, sign, pay or purchase: a person decides at each Wait for approval gate, on the real screens."
      actions={
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={refreshing}>
            <RefreshCw className={refreshing ? "animate-spin" : undefined} /> Refresh
          </Button>
          <Button size="sm" onClick={() => void createWorkflow()} disabled={creating}>
            <Plus /> {creating ? "Creating…" : "New workflow"}
          </Button>
        </div>
      }
    >
      {notice && <PageNotice message={notice} />}
      {actionError && (
        <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
          {actionError}
        </p>
      )}

      {/* ------------------------------------------------ workflows */}
      <section className="grid gap-4">
        <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
          <WorkflowIcon className="size-5 text-coral-400" /> Workflows
        </h2>
        {loading && <SectionLoading label="Loading workflows…" />}
        {error && <SectionError message={error} onRetry={() => void loadWorkflows()} />}
        {workflows && workflows.length === 0 && !loading && (
          <SectionEmpty
            title="No workflows yet"
            body="Create your first workflow to automate what happens when an employer is added, a payroll is held, a benefit is requested, and more."
          />
        )}
        {workflows && workflows.length > 0 && (
          <div className="grid gap-4">
            {workflows.map((wf) => (
              <Card key={wf.id} className="grid gap-3 p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-display text-lg font-semibold text-sand-100">{wf.name}</p>
                    {wf.description ? (
                      <p className="mt-0.5 text-sm font-semibold text-sage-300">{wf.description}</p>
                    ) : null}
                    <p className="mt-1 text-xs font-semibold text-sage-400">
                      Trigger: {humanizeTrigger(wf.trigger_type)} · Updated {fmtDate(wf.updated_at)}
                    </p>
                  </div>
                  <Badge tone={wf.enabled ? "sage" : "neutral"}>{wf.enabled ? "Enabled" : "Disabled"}</Badge>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <label className="flex items-center gap-2.5 text-sm font-bold text-sand-100">
                    <input
                      type="checkbox"
                      checked={wf.enabled}
                      onChange={() => void toggleEnabled(wf)}
                      disabled={busyId === wf.id}
                      className="size-5 shrink-0 accent-coral-500"
                      aria-label={`Enable ${wf.name}`}
                    />
                    Enabled
                  </label>
                  <Button variant="outline" size="sm" asChild>
                    <Link href={`/dashboard/workflows/${wf.id}`}>
                      <Pencil /> Edit
                    </Link>
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setDeleteFor(wf)}
                    disabled={busyId === wf.id}
                    className="text-coral-300 hover:text-coral-200"
                  >
                    <Trash2 /> Delete
                  </Button>
                  {busyId === wf.id && <span className="text-sm font-semibold text-sage-400">Working…</span>}
                </div>
              </Card>
            ))}
          </div>
        )}
      </section>

      {/* ------------------------------------------------ executions */}
      <section className="grid gap-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
            <Play className="size-5 text-coral-400" /> Executions
          </h2>
          <div className="flex flex-wrap gap-3">
            <div className="w-52">
              <PickerField label="Workflow">
                <NativeSelect value={runWorkflowFilter} onChange={(e) => setRunWorkflowFilter(e.target.value)} aria-label="Filter runs by workflow">
                  <option value="">All workflows</option>
                  {(workflows ?? []).map((w) => (
                    <option key={w.id} value={w.id}>{w.name}</option>
                  ))}
                </NativeSelect>
              </PickerField>
            </div>
            <div className="w-44">
              <PickerField label="Status">
                <NativeSelect value={runStatusFilter} onChange={(e) => setRunStatusFilter(e.target.value)} aria-label="Filter runs by status">
                  <option value="">All statuses</option>
                  <option value="RUNNING">Running</option>
                  <option value="WAITING">Waiting</option>
                  <option value="COMPLETED">Completed</option>
                  <option value="FAILED">Failed</option>
                </NativeSelect>
              </PickerField>
            </div>
          </div>
        </div>

        {runsLoading && <SectionLoading label="Loading executions…" />}
        {runsError && <SectionError message={runsError} onRetry={() => void loadRuns()} />}
        {runs && runs.length === 0 && !runsLoading && (
          <SectionEmpty
            title="No executions yet"
            body="When a workflow is triggered, each run will appear here with its step-by-step path. Waiting runs are paused at an approval gate."
            lottie={false}
          />
        )}
        {runs && runs.length > 0 && (
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[820px] text-left text-sm">
                <thead>
                  <tr className="border-b-2 border-ink-700 text-sage-400">
                    <th scope="col" className="px-5 py-3 font-extrabold">Workflow</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Trigger</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Entity</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Status</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Started</th>
                    <th scope="col" className="px-3 py-3 font-extrabold">Finished</th>
                    <th scope="col" className="px-3 py-3 font-extrabold"><span className="sr-only">Details</span></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-ink-700/70">
                  {runs.map((run) => {
                    const expanded = expandedRunId === run.id;
                    return [
                      <tr
                        key={run.id}
                        className={cn("align-top", expanded ? "bg-ink-800/60" : "bg-ink-900/30 hover:bg-ink-800/40")}
                      >
                        <td className="px-5 py-3 font-bold text-sand-100">
                          <Link href={`/dashboard/workflows/runs/${run.id}`} className="hover:text-coral-300 hover:underline hover:underline-offset-4" title="Open this run on the canvas">
                            {workflows?.find((w) => w.id === run.workflow_id)?.name ?? "Workflow"}
                          </Link>
                        </td>
                        <td className="px-3 py-3 font-semibold text-sage-300">{humanizeTrigger(run.trigger_event)}</td>
                        <td className="px-3 py-3 font-semibold text-sand-100">
                          {run.entity_label || run.entity_id}
                          {run.entity_kind && <span className="block text-xs font-bold text-sage-400">{run.entity_kind}</span>}
                        </td>
                        <td className="px-3 py-3"><RunStatusPill status={run.status} /></td>
                        <td className="px-3 py-3 font-semibold text-sage-300">{fmtDate(run.started_at)}</td>
                        <td className="px-3 py-3 font-semibold text-sage-300">{fmtDate(run.finished_at)}</td>
                        <td className="px-3 py-3">
                          <div className="flex flex-wrap gap-1">
                            <Button variant="outline" size="sm" asChild>
                              <Link href={`/dashboard/workflows/runs/${run.id}`}>Open run</Link>
                            </Button>
                            <Button variant="ghost" size="sm" onClick={() => void toggleRunDetail(run)} aria-expanded={expanded}>
                              {expanded ? "Hide" : "Steps"} <ChevronDown className={cn("size-4 transition-transform", expanded && "rotate-180")} />
                            </Button>
                          </div>
                        </td>
                      </tr>,
                      expanded ? (
                        <tr key={`${run.id}-detail`} className="bg-ink-900/60">
                          <td colSpan={7} className="px-5 py-4">
                            {runDetailLoading && <SectionLoading label="Loading steps…" />}
                            {runDetailError && <p role="alert" className="text-sm font-bold text-coral-300">{runDetailError}</p>}
                            {runDetail && runDetail.id === run.id && (
                              <div className="grid gap-3">
                                <p className="text-sm font-bold text-sage-300">
                                  Step path for this run, in order. Only steps returned by the server are shown.
                                </p>
                                {runDetail.steps.length === 0 ? (
                                  <p className="text-sm font-semibold text-sage-400">No steps recorded for this run yet.</p>
                                ) : (
                                  <ol className="grid gap-2">
                                    {runDetail.steps.map((step, idx) => {
                                      const isWaiting = step.status.toUpperCase() === "WAITING";
                                      return (
                                        <li
                                          key={`${step.node_id}-${idx}`}
                                          className={cn(
                                            "flex flex-wrap items-start gap-3 rounded-2xl border-2 px-4 py-3",
                                            isWaiting ? "border-sand-400/50 bg-sand-300/10" : "border-ink-600 bg-ink-900/50",
                                          )}
                                        >
                                          <span className="grid size-7 shrink-0 place-items-center rounded-full bg-ink-700 text-xs font-extrabold text-sand-100">
                                            {idx + 1}
                                          </span>
                                          <div className="min-w-0 flex-1">
                                            <p className="font-bold text-sand-100">
                                              {nodeDef(step.node_type).label}
                                              <span className="ml-2 font-mono text-xs font-semibold text-sage-400">{step.node_type}</span>
                                            </p>
                                            {isWaiting ? (
                                              <p className="mt-0.5 flex items-center gap-1.5 text-sm font-bold text-sand-200">
                                                <Clock className="size-4" /> Waiting for human approval
                                                {step.detail ? <> — {step.detail}</> : null}
                                              </p>
                                            ) : step.detail ? (
                                              <p className="mt-0.5 text-sm font-semibold text-sage-300">{step.detail}</p>
                                            ) : null}
                                            <p className="mt-0.5 text-xs font-semibold text-sage-500">{fmtDate(step.created_at)}</p>
                                          </div>
                                          <StepStatusPill status={step.status} />
                                        </li>
                                      );
                                    })}
                                  </ol>
                                )}
                                <div className="flex flex-wrap gap-2">
                                  <Button variant="outline" size="sm" asChild>
                                    <Link href={`/dashboard/workflows/runs/${run.id}`}>Open run canvas</Link>
                                  </Button>
                                  <Button variant="ghost" size="sm" asChild>
                                    <Link href={`/dashboard/workflows/${run.workflow_id}`}>Open workflow editor</Link>
                                  </Button>
                                </div>
                              </div>
                            )}
                          </td>
                        </tr>
                      ) : null,
                    ];
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </section>

      {/* ------------------------------------------------ delete confirmation */}
      {deleteFor && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/70 p-4" role="dialog" aria-modal="true" aria-label="Delete workflow">
          <Card className="grid w-full max-w-lg gap-4 p-6">
            <h3 className="font-display text-xl font-semibold text-sand-100">Delete this workflow?</h3>
            <p className="text-sm text-sage-300">
              This deletes <span className="font-bold text-sand-100">{deleteFor.name}</span> permanently. Past executions remain in the history, but the workflow will no longer run.
            </p>
            {actionError && <p role="alert" className="text-sm font-bold text-coral-300">{actionError}</p>}
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => void confirmDelete()} disabled={deleting}>
                <Trash2 /> {deleting ? "Deleting…" : "Delete workflow"}
              </Button>
              <Button variant="ghost" onClick={() => setDeleteFor(null)} disabled={deleting}>Cancel</Button>
            </div>
          </Card>
        </div>
      )}
    </FebShell>
  );
}
