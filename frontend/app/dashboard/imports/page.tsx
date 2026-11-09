"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft, ArrowRight, Check, CircleCheck, CircleX, Copy, Download,
  FileSpreadsheet, FileUp, Loader2, Play, RefreshCw, TriangleAlert, Upload,
} from "lucide-react";

import { AppBar } from "@/components/dashboard/app-bar";
import { PageError, PageNotice, SectionEmpty, SectionError, SectionLoading } from "@/components/dashboard/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/select";
import {
  api, apiV3, ApiError,
  type Employer, type ImportPreview, type RunSummary, type Session,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : "Something went wrong. Try again.");

/*
 * Canonical import fields. These mirror the contractor / pay-line fields
 * the rest of PaySilo already uses (see ContractorIn and RunLineIn in
 * lib/api.ts): a row becomes a pay line for one person.
 */
const CANONICAL_FIELDS: { key: string; label: string; required?: boolean; aliases: string[] }[] = [
  { key: "full_name", label: "Full name", required: true, aliases: ["full_name", "fullname", "name", "contractor_name", "beneficiary_name", "employee_name", "payee", "payee_name"] },
  { key: "email", label: "Email", aliases: ["email", "email_address", "e_mail"] },
  { key: "country", label: "Country (2-letter code)", aliases: ["country", "country_code"] },
  { key: "currency", label: "Currency", required: true, aliases: ["currency", "pay_currency", "curr", "ccy"] },
  { key: "amount", label: "Amount", required: true, aliases: ["amount", "gross_amount", "default_amount", "pay", "salary", "gross", "gross_pay", "net_amount"] },
  { key: "payout_method", label: "Payout method (SWIFT or LOCAL)", aliases: ["payout_method", "method", "payment_method"] },
  { key: "iban", label: "IBAN", aliases: ["iban"] },
  { key: "swift_code", label: "SWIFT / BIC code", aliases: ["swift_code", "swift", "bic", "swift_bic"] },
  { key: "account_number", label: "Account number", aliases: ["account_number", "account", "acct_number", "account_no"] },
  { key: "routing_code", label: "Routing code", aliases: ["routing_code", "routing", "sort_code", "ifsc", "ifsc_code"] },
];

const SAMPLE_CSV = [
  "full_name,email,country,currency,amount,payout_method,iban,swift_code,account_number,routing_code",
  "Sample Person One,sample.one@example.com,IN,INR,85000,LOCAL,,,,",
  "Sample Person Two,sample.two@example.com,DE,EUR,3400,SWIFT,DE89370400440532013000,COBADEHDXXX,,",
].join("\n");

function normalizeHeader(h: string): string {
  return h.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

function autoMapping(headers: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  const used = new Set<string>();
  for (const header of headers) {
    const norm = normalizeHeader(header);
    const match = CANONICAL_FIELDS.find((f) => !used.has(f.key) && (f.key === norm || f.aliases.includes(norm)));
    out[header] = match ? match.key : "";
    if (match) used.add(match.key);
  }
  return out;
}

/** Small CSV reader for headers + a few sample rows (handles quotes). The full file is validated by the server. */
function parseCsvSample(content: string, maxRows = 4): { headers: string[]; rows: string[][] } {
  const records: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  const pushField = () => { row.push(field); field = ""; };
  const pushRow = () => { pushField(); records.push(row); row = []; };
  for (let i = 0; i < content.length && records.length <= maxRows; i++) {
    const ch = content[i];
    if (inQuotes) {
      if (ch === '"') {
        if (content[i + 1] === '"') { field += '"'; i++; } else inQuotes = false;
      } else field += ch;
    } else if (ch === '"') inQuotes = true;
    else if (ch === ",") pushField();
    else if (ch === "\n") pushRow();
    else if (ch !== "\r") field += ch;
  }
  if ((field !== "" || row.length > 0) && records.length <= maxRows) pushRow();
  const [headers = [], ...rows] = records;
  return { headers: headers.map((h) => h.trim()), rows };
}

const STEPS = ["Upload CSV", "Field mapping", "Validation", "Commit"] as const;

function rowTone(status: string): "success" | "warning" | "danger" {
  const s = status.toLowerCase();
  if (s === "ok" || s === "valid") return "success";
  if (s === "duplicate") return "warning";
  return "danger";
}
function rowLabel(status: string): string {
  const s = status.toLowerCase();
  if (s === "ok" || s === "valid") return "Valid";
  if (s === "duplicate") return "Duplicate";
  return "Error";
}

export default function ImportsPage() {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [employers, setEmployers] = useState<Employer[]>([]);
  const [employerId, setEmployerId] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [step, setStep] = useState(0);
  const [filename, setFilename] = useState("payroll.csv");
  const [content, setContent] = useState("");
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [periodLabel, setPeriodLabel] = useState("");
  const [payDate, setPayDate] = useState("");
  const [committing, setCommitting] = useState(false);
  const [commitError, setCommitError] = useState<string | null>(null);
  const [createdRun, setCreatedRun] = useState<RunSummary | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [me, overview] = await Promise.all([api.me(), api.overview()]);
      setSession(me);
      setEmployers(overview.employers);
      setEmployerId((prev) => prev || overview.employers[0]?.id || "");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login?next=/dashboard/imports");
        return;
      }
      setLoadError(errMsg(err));
    }
    setLoading(false);
  }, [router]);

  useEffect(() => { void load(); }, [load]);

  const parsed = useMemo(() => (content.trim() ? parseCsvSample(content) : { headers: [], rows: [] }), [content]);
  const employer = employers.find((e) => e.id === employerId) ?? null;

  const unmappedRequired = useMemo(() => {
    const mapped = new Set(Object.values(mapping).filter(Boolean));
    return CANONICAL_FIELDS.filter((f) => f.required && !mapped.has(f.key));
  }, [mapping]);

  function applyContent(next: string, name?: string) {
    setContent(next);
    if (name) setFilename(name);
    const { headers } = next.trim() ? parseCsvSample(next) : { headers: [] };
    setMapping(autoMapping(headers));
    setPreview(null);
    setPreviewError(null);
    setCreatedRun(null);
  }

  async function onFile(file: File) {
    try {
      applyContent(await file.text(), file.name);
    } catch {
      setPreviewError("Could not read that file. Try pasting the CSV instead.");
    }
  }

  function downloadSample() {
    const blob = new Blob([SAMPLE_CSV], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "paysilo-payroll-sample.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  async function runPreview() {
    if (!employer) return;
    setPreviewing(true);
    setPreviewError(null);
    try {
      const cleaned: Record<string, string> = {};
      for (const [col, field] of Object.entries(mapping)) if (field) cleaned[col] = field;
      const result = await apiV3.previewImport(employer.id, {
        filename,
        content,
        mapping: Object.keys(cleaned).length > 0 ? cleaned : undefined,
      });
      setPreview(result);
      setStep(2);
    } catch (err) {
      setPreviewError(errMsg(err));
    } finally {
      setPreviewing(false);
    }
  }

  async function commit() {
    if (!employer || !preview) return;
    setCommitting(true);
    setCommitError(null);
    try {
      const run = await apiV3.commitImport(employer.id, preview.import_id, {
        period_label: periodLabel.trim() || undefined,
        pay_date: payDate || undefined,
      });
      setCreatedRun(run);
      setNotice(`Import committed. A payroll run was created for ${employer.name}.`);
    } catch (err) {
      setCommitError(errMsg(err));
    } finally {
      setCommitting(false);
    }
  }

  function resetWizard() {
    setStep(0);
    setContent("");
    setMapping({});
    setPreview(null);
    setPreviewError(null);
    setCommitError(null);
    setCreatedRun(null);
    setPeriodLabel("");
    setPayDate("");
    setNotice(null);
  }

  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}

      <main className="mx-auto mt-8 grid max-w-6xl gap-8 px-4 sm:px-6">
        <section>
          <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Import payroll</h1>
          <p className="mt-1 max-w-2xl text-sage-300">
            Bring a payroll sheet in as a run. PaySilo checks every row before anything is committed —
            nothing is paid from an import until the run itself is approved.
          </p>
        </section>

        {loading && <SectionLoading label="Loading employers…" />}
        {loadError && <SectionError message={loadError} onRetry={() => void load()} />}
        {notice && <PageNotice message={notice} />}

        {!loading && !loadError && employers.length === 0 && (
          <SectionEmpty
            title="Add an employer first"
            body="Imports belong to one employer's silo. Add an employer, then import its payroll sheet here."
          />
        )}

        {!loading && employers.length > 0 && (
          <>
            <Card className="p-5">
              <Label htmlFor="import-employer">Employer</Label>
              <div className="mt-1.5 max-w-md">
                <NativeSelect
                  id="import-employer"
                  value={employerId}
                  onChange={(e) => { setEmployerId(e.target.value); setPreview(null); setCreatedRun(null); }}
                >
                  {employers.map((e) => (
                    <option key={e.id} value={e.id}>{e.name} · {e.contractor_count} contractors</option>
                  ))}
                </NativeSelect>
              </div>
              <p className="mt-2 text-sm text-sage-400">
                The imported run is created in {employer ? <span className="font-bold text-sand-200">{employer.name}</span> : "this employer"}&apos;s silo only.
              </p>
            </Card>

            <ol className="flex flex-wrap gap-2" aria-label="Import progress">
              {STEPS.map((label, i) => (
                <li key={label}
                    aria-current={step === i ? "step" : undefined}
                    className={cn(
                      "flex items-center gap-2 rounded-full border-2 px-4 py-2 text-sm font-extrabold",
                      step === i
                        ? "border-coral-500 bg-coral-500/15 text-sand-100"
                        : step > i
                          ? "border-sage-600/60 bg-sage-500/10 text-sage-300"
                          : "border-ink-600 bg-ink-850/60 text-sage-500",
                    )}>
                  {step > i ? <Check className="size-4" /> : <span>{i + 1}</span>} {label}
                </li>
              ))}
            </ol>

            {step === 0 && (
              <Card className="grid gap-5 p-5 sm:p-6">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-display text-xl font-semibold text-sand-100">Upload a CSV</h2>
                    <p className="mt-1 max-w-xl text-sm text-sage-400">
                      One row per person. Expected columns: {CANONICAL_FIELDS.map((f) => f.key).join(", ")}.
                      Only full name, currency and amount are required — headers are matched automatically in the next step.
                    </p>
                  </div>
                  <Button variant="outline" size="sm" onClick={downloadSample}>
                    <Download /> Download sample CSV
                  </Button>
                </div>

                <div className="flex flex-wrap gap-3">
                  <Button variant="sage" onClick={() => fileRef.current?.click()}>
                    <Upload /> Choose a CSV file
                  </Button>
                  <input
                    ref={fileRef} type="file" accept=".csv,text/csv,text/plain" className="sr-only"
                    aria-label="Choose a CSV file"
                    onChange={(e) => { const f = e.target.files?.[0]; if (f) void onFile(f); e.target.value = ""; }}
                  />
                  <span className="self-center text-sm font-semibold text-sage-400">{filename} · {content ? `${content.length.toLocaleString()} characters` : "no content yet"}</span>
                </div>

                <div>
                  <Label htmlFor="csv-paste">Or paste CSV content</Label>
                  <textarea
                    id="csv-paste"
                    value={content}
                    onChange={(e) => applyContent(e.target.value)}
                    rows={8}
                    spellCheck={false}
                    placeholder={"full_name,email,country,currency,amount,payout_method,iban,swift_code,account_number,routing_code\n…"}
                    className="mt-1.5 w-full rounded-2xl border-2 border-ink-600 bg-ink-900/80 px-4 py-3 font-mono text-[13px] leading-relaxed text-sand-100 placeholder:text-muted-foreground/60 focus-visible:border-sage-400 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-sage-500/25"
                  />
                </div>

                {parsed.headers.length > 0 && (
                  <p className="text-sm font-semibold text-sage-300">
                    Detected {parsed.headers.length} columns: {parsed.headers.join(", ")}
                  </p>
                )}
                {previewError && <PageError message={previewError} />}

                <div className="flex justify-end">
                  <Button onClick={() => setStep(1)} disabled={!employer || parsed.headers.length === 0}>
                    Map fields <ArrowRight />
                  </Button>
                </div>
              </Card>
            )}

            {step === 1 && (
              <Card className="grid gap-5 p-5 sm:p-6">
                <div>
                  <h2 className="font-display text-xl font-semibold text-sand-100">Field mapping</h2>
                  <p className="mt-1 max-w-xl text-sm text-sage-400">
                    PaySilo matched your columns automatically. Adjust anything that looks wrong — unmapped columns are ignored.
                  </p>
                </div>

                <div className="grid gap-3">
                  {parsed.headers.map((header) => (
                    <div key={header} className="grid items-center gap-2 rounded-2xl bg-ink-950/40 px-4 py-3 sm:grid-cols-[1fr_auto_1fr]">
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-sm font-bold text-sand-100">{header}</span>
                        <span className="block truncate text-xs font-semibold text-sage-500">
                          {parsed.rows[0]?.[parsed.headers.indexOf(header)] ?? "—"}
                        </span>
                      </span>
                      <ArrowRight className="hidden size-4 text-sage-500 sm:block" />
                      <NativeSelect
                        aria-label={`Map column ${header} to a PaySilo field`}
                        value={mapping[header] ?? ""}
                        onChange={(e) => setMapping((m) => ({ ...m, [header]: e.target.value }))}
                      >
                        <option value="">Ignore this column</option>
                        {CANONICAL_FIELDS.map((f) => (
                          <option key={f.key} value={f.key}>{f.label}{f.required ? " (required)" : ""}</option>
                        ))}
                      </NativeSelect>
                    </div>
                  ))}
                </div>

                {unmappedRequired.length > 0 && (
                  <p role="alert" className="flex gap-2 rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-4 py-3 text-sm font-bold text-sand-200">
                    <TriangleAlert className="size-5 shrink-0" />
                    Map a column to {unmappedRequired.map((f) => f.label).join(", ")} before validating — rows cannot be checked without {unmappedRequired.length === 1 ? "it" : "them"}.
                  </p>
                )}
                {previewError && <PageError message={previewError} />}

                <div className="flex flex-wrap justify-between gap-3">
                  <Button variant="ghost" onClick={() => setStep(0)}><ArrowLeft /> Back</Button>
                  <Button onClick={() => void runPreview()} disabled={previewing || unmappedRequired.length > 0}>
                    {previewing ? <Loader2 className="animate-spin" /> : <FileSpreadsheet />} Validate rows
                  </Button>
                </div>
              </Card>
            )}

            {step === 2 && (
              <div className="grid gap-5">
                {previewing && <SectionLoading label="Validating rows…" />}
                {previewError && <SectionError message={previewError} onRetry={() => void runPreview()} />}
                {preview && (
                  <>
                    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                      {[
                        { label: "Rows read", value: preview.row_count },
                        { label: "Valid", value: preview.valid_count },
                        { label: "Errors", value: preview.error_count },
                        { label: "Duplicates", value: preview.duplicate_count },
                      ].map((s) => (
                        <div key={s.label} className="rounded-3xl border-2 border-ink-700 bg-ink-850/80 p-4">
                          <p className="text-sm font-bold text-sage-400">{s.label}</p>
                          <p className="mt-1 font-display text-3xl font-semibold tabular-nums text-sand-100">{s.value}</p>
                        </div>
                      ))}
                    </div>

                    <p className="text-xs font-semibold text-sage-500">
                      Content hash <span className="break-all font-mono text-sage-400">{preview.content_hash}</span> — the server checks this exact content again at commit.
                      {typeof preview.summary === "string" && preview.summary ? ` ${preview.summary}` : ""}
                    </p>
                    {preview.summary && typeof preview.summary === "object" && Object.keys(preview.summary).length > 0 && (
                      <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
                        {Object.entries(preview.summary).map(([k, v]) => (
                          <div key={k} className="flex gap-2">
                            <dt className="font-bold text-sage-500">{k.replaceAll("_", " ")}</dt>
                            <dd className="font-semibold text-sand-200">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
                          </div>
                        ))}
                      </dl>
                    )}

                    {preview.rows.length === 0 ? (
                      <SectionEmpty title="No rows returned" body="The server read the file but returned no rows to review. Go back and check the CSV content." />
                    ) : (
                      <div className="overflow-x-auto rounded-4xl border-2 border-ink-700">
                        <table className="w-full min-w-[720px] border-collapse text-left">
                          <thead>
                            <tr className="bg-ink-900/70 text-xs font-extrabold uppercase tracking-widest text-sage-500">
                              <th scope="col" className="px-5 py-3">Row</th>
                              <th scope="col" className="px-5 py-3">Status</th>
                              <th scope="col" className="px-5 py-3">Data</th>
                              <th scope="col" className="px-5 py-3">Problems</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y-2 divide-ink-700">
                            {preview.rows.map((row) => (
                              <tr key={row.row_number} className="bg-ink-850/80 align-top">
                                <td className="px-5 py-3 font-display font-semibold tabular-nums text-sand-100">{row.row_number}</td>
                                <td className="px-5 py-3">
                                  <Badge tone={rowTone(row.status)}>
                                    {rowTone(row.status) === "success" ? <CircleCheck className="size-3.5" /> : rowTone(row.status) === "warning" ? <Copy className="size-3.5" /> : <CircleX className="size-3.5" />}
                                    {rowLabel(row.status)}
                                  </Badge>
                                </td>
                                <td className="px-5 py-3">
                                  <span className="flex max-w-md flex-wrap gap-1.5">
                                    {Object.entries(row.data ?? {}).filter(([, v]) => v !== null && v !== "").map(([k, v]) => (
                                      <span key={k} className="rounded-full bg-ink-950/60 px-2.5 py-1 text-xs font-semibold text-sand-200">
                                        <span className="text-sage-500">{k}:</span> {String(v)}
                                      </span>
                                    ))}
                                    {Object.keys(row.data ?? {}).length === 0 && <span className="text-sm text-sage-500">—</span>}
                                  </span>
                                </td>
                                <td className="px-5 py-3 text-sm font-semibold text-coral-300">
                                  {row.errors.length > 0 ? row.errors.join(" ") : <span className="text-sage-500">—</span>}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}

                    <div className="flex flex-wrap justify-between gap-3">
                      <Button variant="ghost" onClick={() => setStep(1)}><ArrowLeft /> Adjust mapping</Button>
                      <div className="flex gap-3">
                        <Button variant="outline" onClick={() => void runPreview()} disabled={previewing}>
                          <RefreshCw className={previewing ? "animate-spin" : undefined} /> Re-validate
                        </Button>
                        <Button onClick={() => setStep(3)}>Continue to commit <ArrowRight /></Button>
                      </div>
                    </div>
                  </>
                )}
              </div>
            )}

            {step === 3 && preview && (
              <Card className="grid gap-5 p-5 sm:p-6">
                <div>
                  <h2 className="font-display text-xl font-semibold text-sand-100">Commit this import</h2>
                  <p className="mt-1 max-w-xl text-sm text-sage-400">
                    Committing creates a payroll run for {employer?.name} from the {preview.valid_count} valid row{preview.valid_count === 1 ? "" : "s"}.
                    The run still goes through pricing, the funding check and a signed approval before anyone is paid.
                  </p>
                </div>

                {createdRun ? (
                  <div className="grid gap-4">
                    <p role="status" className="flex gap-2 rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-4 py-3 text-sm font-bold text-sage-300">
                      <CircleCheck className="size-5 shrink-0" />
                      Run created — {createdRun.period_label ?? "Payroll run"} · status {createdRun.status.replaceAll("_", " ")}
                      {createdRun.required_usd ? ` · required ${createdRun.required_usd} USD` : ""}.
                    </p>
                    <div className="flex flex-wrap gap-3">
                      <Button asChild>
                        <Link href={`/dashboard/runs/${createdRun.id}`}>View the run</Link>
                      </Button>
                      <Button variant="outline" asChild>
                        <Link href="/dashboard/execution"><Play /> Open execution</Link>
                      </Button>
                      <Button variant="ghost" onClick={resetWizard}>
                        <FileUp /> Import another file
                      </Button>
                    </div>
                  </div>
                ) : (
                  <>
                    <dl className="grid gap-3 text-sm sm:grid-cols-2">
                      {[
                        ["Employer", employer?.name ?? "—"],
                        ["Rows", `${preview.row_count} read · ${preview.valid_count} valid · ${preview.error_count} errors · ${preview.duplicate_count} duplicates`],
                        ["File", filename],
                        ["Import ID", preview.import_id],
                      ].map(([k, v]) => (
                        <div key={k} className="rounded-2xl bg-ink-950/40 px-4 py-3">
                          <dt className="font-bold text-sage-500">{k}</dt>
                          <dd className="mt-0.5 break-all font-semibold text-sand-100">{v}</dd>
                        </div>
                      ))}
                    </dl>

                    {preview.error_count > 0 && (
                      <p role="alert" className="flex gap-2 rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
                        <TriangleAlert className="size-5 shrink-0" />
                        {preview.error_count} row{preview.error_count === 1 ? " has" : "s have"} errors. The server may reject this commit — go back to Validation to see exactly which rows fail, or fix the file and re-import.
                      </p>
                    )}
                    {preview.duplicate_count > 0 && (
                      <p className="flex gap-2 rounded-2xl border-2 border-sand-400/50 bg-sand-300/10 px-4 py-3 text-sm font-bold text-sand-200">
                        <Copy className="size-5 shrink-0" />
                        {preview.duplicate_count} duplicate row{preview.duplicate_count === 1 ? "" : "s"} detected. Duplicates are flagged in Validation; whether they are skipped is decided by the server at commit.
                      </p>
                    )}

                    <div className="grid gap-4 sm:grid-cols-2">
                      <div>
                        <Label htmlFor="import-period">Period label (optional)</Label>
                        <Input id="import-period" value={periodLabel} onChange={(e) => setPeriodLabel(e.target.value)} placeholder="e.g. November 2026 payroll" className="mt-1.5" maxLength={60} />
                      </div>
                      <div>
                        <Label htmlFor="import-paydate">Pay date (optional)</Label>
                        <Input id="import-paydate" type="date" value={payDate} onChange={(e) => setPayDate(e.target.value)} className="mt-1.5" />
                      </div>
                    </div>

                    {commitError && <PageError message={commitError} />}

                    <div className="flex flex-wrap justify-between gap-3">
                      <Button variant="ghost" onClick={() => setStep(2)}><ArrowLeft /> Back to validation</Button>
                      <Button onClick={() => void commit()} disabled={committing}>
                        {committing ? <Loader2 className="animate-spin" /> : <Check />} Commit import &amp; create run
                      </Button>
                    </div>
                  </>
                )}
              </Card>
            )}
          </>
        )}
      </main>
    </div>
  );
}
