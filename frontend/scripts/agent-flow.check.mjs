// Plain-node check for lib/agent-flow.ts (the repo has no frontend test runner). Run: node --experimental-strip-types scripts/agent-flow.check.mjs
import assert from "node:assert/strict";
import { deriveFlow, parseBedrock, formatElapsed } from "../lib/agent-flow.ts";

const ev = (agent, level, message, t) => ({ agent, level, message, created_at: t ? `2026-11-13T10:00:${t}Z` : undefined });
const st = (f, id) => [...f.main, ...f.branches].find((n) => n.id === id)?.state;

// Settled run with bedrock
const settled = [
  ev("intake", "info", "Read 3 pay line(s).", "00"), ev("price", "info", "Priced.", "01"),
  ev("bedrock", "info", "re computed ... Amazon Bedrock AgentCore (us-east-1, session abcd1234, 812 ms) and matches", "02"),
  ev("assess", "success", "CAN RUN", "03"), ev("approval", "success", "a@b decided 'approve'; Visa TAP signature verified.", "10"),
  ev("execute", "success", "paid", "12"), ev("fee", "success", "fee", "13"), ev("reconcile", "success", "ok", "14"), ev("attest", "success", "anchored", "15"),
];
let f = deriveFlow(settled, "SETTLED");
assert.equal(st(f, "intake"), "done"); assert.equal(st(f, "risk"), "done"); assert.equal(st(f, "attest"), "done");
assert.equal(f.branches.find((b) => b.id === "bedrock").detail, "us-east-1, 812 ms");
assert.deepEqual(f.summary.systems, ["Airwallex", "Bedrock", "Metal"]);
assert.equal(f.summary.humanDecisions, 1); assert.equal(f.summary.elapsedMs, 15000); assert.equal(f.summary.agentsRan, 9);
assert.equal(f.currentId, null);

// Waiting for approval
f = deriveFlow(settled.slice(0, 4), "RESERVED", { pendingKind: "approval" });
assert.equal(st(f, "approval"), "waiting"); assert.equal(f.currentId, "approval"); assert.equal(st(f, "execute"), "upcoming");
assert.equal(st(f, "assess"), "done");

// Live, mid-flight: frontier is active
f = deriveFlow(settled.slice(0, 2), "PRICED");
assert.equal(st(f, "assess"), "active"); assert.equal(f.currentId, "assess"); assert.equal(st(f, "bedrock"), "off");
// No bedrock event after pricing => off ("not configured")
f = deriveFlow([ev("intake", "info", "x"), ev("price", "info", "x"), ev("assess", "success", "x")], "RESERVED");
assert.equal(st(f, "bedrock"), "off");

// Partially paid with held lines (escalate warning branch)
f = deriveFlow([...settled.slice(0, 3), ev("assess", "warning", "SHORTFALL"), ev("escalate", "warning", "1 line held"), ...settled.slice(4)], "PARTIALLY_PAID");
assert.equal(st(f, "assess"), "warning"); assert.equal(st(f, "escalate"), "warning");

// Refused by the risk gate: branch hangs off risk, later nodes skipped
f = deriveFlow([ev("intake", "info", "x"), ev("risk", "warning", "holds"), ev("risk", "error", "POLICY REFUSAL")], "REFUSED");
assert.equal(st(f, "risk"), "failed"); assert.equal(st(f, "price"), "skipped");
assert.deepEqual(f.branches.find((b) => b.id === "refused"), { id: "refused", state: "failed", count: 0, parent: "risk" });

// Failed payout, held by human, escalation waiting
f = deriveFlow([...settled.slice(0, 5), ev("execute", "error", "boom")], "FAILED");
assert.equal(st(f, "execute"), "failed"); assert.equal(st(f, "fee"), "skipped");
f = deriveFlow(settled.slice(0, 5), "HELD_BY_HUMAN");
assert.ok(f.branches.some((b) => b.id === "held"));
f = deriveFlow([ev("intake", "info", "x"), ev("price", "info", "x"), ev("assess", "warning", "SHORT")], "HELD_SHORTFALL", { pendingKind: "escalation" });
assert.equal(st(f, "escalate"), "waiting");

// Helpers + no timestamps
assert.equal(parseBedrock("nothing"), null);
assert.equal(formatElapsed(null), "n/a"); assert.equal(formatElapsed(65000), "1 m 5 s");
assert.equal(deriveFlow([ev("intake", "info", "x")], "RECEIVED").summary.elapsedMs, null);
assert.equal(deriveFlow([], "RECEIVED").currentId, "intake");
console.log("agent-flow checks passed");
