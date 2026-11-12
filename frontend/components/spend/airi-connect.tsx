"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Copy, ExternalLink, Link2, Plug, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { apiBenefits, ApiError, type AiriLogin, type VicStatus } from "@/lib/api";

/**
 * "Connect Airi": signs the SERVER's airi-cli in from the browser (device-code
 * flow). Owner/admin only. The code and link are shown verbatim from the server,
 * which only forwards https links on airi.com / airwallex.com hosts. One Airi
 * account serves every organisation on this server, and the modal says so.
 */

const POLL_GAP_MS = 3000;
const SOON_MS = 15 * 60 * 1000;

export function formatLocal(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

function fmtCountdown(sec: number): string {
  const s = Math.max(0, sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

type Phase =
  | { kind: "starting" }
  | { kind: "waiting"; uri: string; code: string; expiresAt: number }
  | { kind: "connected"; sessionExpires: string | null }
  | { kind: "ended"; message: string };

function ConnectModal({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [phase, setPhase] = useState<Phase>({ kind: "starting" });
  const [copied, setCopied] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const now = useNow(1000);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const apply = (r: AiriLogin): boolean => {
      if (!alive.current) return true;
      if (r.state === "connected") {
        setPhase({ kind: "connected", sessionExpires: r.session_expires ?? null });
        onChanged();
        return true;
      }
      if (r.state === "pending" && r.verification_uri && r.user_code) {
        setPhase({ kind: "waiting", uri: r.verification_uri, code: r.user_code,
          expiresAt: Date.now() + (r.expires_in ?? 600) * 1000 });
        return false;
      }
      setPhase({ kind: "ended", message: r.message ?? "The sign-in did not complete. Try again." });
      return true;
    };
    const fail = (e: unknown) => {
      if (!alive.current) return;
      setPhase({ kind: "ended", message: e instanceof ApiError ? e.message : "Could not reach the server. Try again." });
    };
    const loop = async () => {
      try {
        const r = await apiBenefits.airiPoll();
        if (!alive.current) return;
        if (r.state === "pending") {
          // keep the original countdown; only the state matters while waiting
          timer = setTimeout(() => void loop(), POLL_GAP_MS);
          return;
        }
        apply(r);
      } catch (e) {
        fail(e);
      }
    };

    setPhase({ kind: "starting" });
    apiBenefits.airiConnect().then((r) => {
      if (!apply(r)) timer = setTimeout(() => void loop(), POLL_GAP_MS);
    }).catch(fail);

    return () => {
      alive.current = false;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  // The code lapsing locally ends the wait even if a poll is in flight.
  const expired = phase.kind === "waiting" && now >= phase.expiresAt;

  const copy = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard unavailable: the code is selectable text */
    }
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto bg-ink-950/70 p-4" role="dialog" aria-modal="true" aria-label="Connect Airi">
      <Card className="grid max-h-[92dvh] w-full max-w-md gap-4 overflow-y-auto p-5 sm:p-6">
        <div className="flex items-start justify-between gap-3">
          <h3 className="flex items-center gap-2 font-display text-xl font-semibold text-sand-100">
            <Plug className="size-5 text-coral-400" /> Connect Airi
          </h3>
          <button type="button" onClick={onClose} aria-label="Close" className="rounded-full p-1.5 text-sage-300 hover:bg-ink-700">
            <X className="size-5" />
          </button>
        </div>

        {phase.kind === "starting" && <p className="text-sm font-semibold text-sage-300" role="status">Getting a sign-in code from Airi…</p>}

        {phase.kind === "waiting" && !expired && (
          <>
            <p className="text-sm font-semibold text-sage-300">Open this link and enter the code.</p>
            <a href={phase.uri} target="_blank" rel="noopener noreferrer"
              className="flex items-center gap-2 break-all rounded-2xl border-2 border-ink-600 bg-ink-800/70 px-4 py-3 text-sm font-bold text-coral-300 hover:bg-ink-700">
              <Link2 className="size-4 shrink-0" /> <span className="min-w-0">{phase.uri}</span> <ExternalLink className="ml-auto size-4 shrink-0" />
            </a>
            <div className="flex items-center justify-between gap-3 rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-4 py-4">
              <span className="select-all break-all font-mono text-2xl font-extrabold tracking-widest text-sand-100 sm:text-3xl" aria-label="Sign-in code">{phase.code}</span>
              <Button type="button" variant="sand" size="sm" onClick={() => void copy(phase.code)} aria-label="Copy code">
                {copied ? <Check /> : <Copy />} {copied ? "Copied" : "Copy"}
              </Button>
            </div>
            <p className="text-sm font-semibold text-sage-400" role="status">
              Waiting for approval… code expires in <span className="font-mono text-sand-100">{fmtCountdown(Math.round((phase.expiresAt - now) / 1000))}</span>
            </p>
          </>
        )}

        {phase.kind === "connected" && (
          <div className="grid gap-3" role="status">
            <p className="flex items-center gap-2 font-extrabold text-sage-300"><Check className="size-5" /> Airi is connected</p>
            <p className="text-sm font-semibold text-sage-300">Session valid until {formatLocal(phase.sessionExpires)}.</p>
            <Button type="button" variant="sage" onClick={onClose}>Done</Button>
          </div>
        )}

        {(phase.kind === "ended" || expired) && (
          <div className="grid gap-3" role="alert">
            <p className="text-sm font-semibold text-coral-300">
              {phase.kind === "ended" ? phase.message : "The sign-in code expired. Try again for a new code."}
            </p>
            <Button type="button" onClick={() => setAttempt((n) => n + 1)}>Try again</Button>
          </div>
        )}

        <p className="text-xs font-semibold text-sage-500">
          This server uses one Airi account for all organisations. Anyone connecting here connects it for everyone.
        </p>
      </Card>
    </div>
  );
}

/**
 * Banner actions. Not signed in: a Connect button (owner/admin) or a hint.
 * Signed in: a small chip with the session expiry, Disconnect (inline confirm)
 * and a gentle Reconnect when the session is about to lapse.
 */
export function AiriConnectControls({
  status, canEdit, onChanged,
}: { status: VicStatus; canEdit: boolean; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const now = useNow(30_000);

  const disconnect = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await apiBenefits.airiDisconnect();
      setConfirming(false);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not disconnect.");
    } finally {
      setBusy(false);
    }
  }, [onChanged]);

  if (status.mode === "SIMULATED") return null;

  const modal = open ? <ConnectModal onClose={() => { setOpen(false); onChanged(); }} onChanged={onChanged} /> : null;

  if (status.mode === "VIC_UNAVAILABLE") {
    if (!status.cli_available) return null;
    if (!canEdit) return <p className="text-sm font-bold text-coral-300/90">Ask an owner or admin to connect Airi.</p>;
    return (
      <>
        <div><Button type="button" onClick={() => setOpen(true)}><Plug /> Connect Airi</Button></div>
        {modal}
      </>
    );
  }

  // mode === "VIC": connected
  const exp = status.session_expires ? new Date(status.session_expires).getTime() : NaN;
  const soon = Number.isFinite(exp) && exp - now < SOON_MS;
  return (
    <>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className={`inline-flex items-center gap-1.5 rounded-full border-2 px-3 py-1 text-xs font-extrabold ${
          soon ? "border-sand-400/60 bg-sand-300/10 text-sand-200" : "border-sage-600/60 bg-sage-500/10 text-sage-300"}`}>
          {soon ? <Link2 className="size-3.5" /> : <Check className="size-3.5" />}
          {soon ? `Airi session ends soon · ${formatLocal(status.session_expires)}` : `Airi connected · valid until ${formatLocal(status.session_expires)}`}
        </span>
        {canEdit && soon && !confirming && (
          <Button type="button" variant="sand" size="sm" onClick={() => setConfirming(true)}>Reconnect</Button>
        )}
        {canEdit && !confirming && (
          <Button type="button" variant="link" className="h-auto p-0 text-xs" onClick={() => setConfirming(true)}>Disconnect</Button>
        )}
        {canEdit && confirming && (
          <span className="flex flex-wrap items-center gap-2 text-xs font-bold text-sage-300">
            {soon ? "Sign Airi out so you can connect again?" : "Disconnect Airi for all organisations?"}
            <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void disconnect()}>{soon ? "Sign out" : "Disconnect"}</Button>
            <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => setConfirming(false)}>Cancel</Button>
          </span>
        )}
        {error && <span className="text-xs font-bold text-coral-300" role="alert">{error}</span>}
      </div>
      {modal}
    </>
  );
}
