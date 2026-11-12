"""Graph builders shared by the seed templates, the payroll-schedule wizard and the Silo agent's keyless matcher.

``layout_graph`` lays a graph out left -> right (longest-path layering): triggers on the left, every edge flows
rightwards, the 'true' branch of a condition above its 'false' branch. It is dependency-free on purpose.
"""

from __future__ import annotations

import re

COL_W = 290
ROW_H = 150
X0, Y0 = 40, 60


def layout_graph(graph: dict) -> dict:
    """Return a copy of ``graph`` with horizontal positions and ``layout: "lr"``."""
    nodes = [dict(n) for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id")]
    edges = [e for e in graph.get("edges") or [] if isinstance(e, dict)]
    ids = [n["id"] for n in nodes]
    known = set(ids)
    outgoing: dict[str, list[dict]] = {i: [] for i in ids}
    indeg: dict[str, int] = {i: 0 for i in ids}
    for e in edges:
        if e.get("source") in known and e.get("target") in known and e["source"] != e["target"]:
            outgoing[e["source"]].append(e)
            indeg[e["target"]] += 1
    layer: dict[str, int] = {}
    order = [i for i in ids if indeg[i] == 0] or ids[:1]
    # Kahn-style longest path (cycles, if any, leave their members on the layer first reached).
    remaining = dict(indeg)
    queue = list(order)
    for i in queue:
        layer.setdefault(i, 0)
    seen: set[str] = set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        for e in sorted(outgoing[cur], key=lambda x: 0 if x.get("sourceHandle") == "true" else 1):
            tgt = e["target"]
            layer[tgt] = max(layer.get(tgt, 0), layer[cur] + 1)
            remaining[tgt] -= 1
            if remaining[tgt] <= 0:
                queue.append(tgt)
    for i in ids:
        layer.setdefault(i, 0)
    # Row assignment: process layer by layer, ordering by the first parent's row then handle.
    row: dict[str, float] = {}
    by_layer: dict[int, list[str]] = {}
    for i in ids:
        by_layer.setdefault(layer[i], []).append(i)
    parents: dict[str, list[tuple[str, int]]] = {i: [] for i in ids}
    for src, es in outgoing.items():
        for e in es:
            parents[e["target"]].append((src, 0 if e.get("sourceHandle") == "true" else 1))
    for ly in sorted(by_layer):
        members = by_layer[ly]
        if ly == 0:
            ordered = members
        else:
            def key(n: str) -> tuple:
                ps = [row[p] + h * 0.01 for p, h in parents[n] if p in row]
                return (min(ps) if ps else 1e9, ids.index(n))
            ordered = sorted(members, key=key)
        taken: list[float] = []
        for n in ordered:
            ps = [row[p] for p, _ in parents[n] if p in row]
            want = min(ps) if ps else (taken[-1] + 1 if taken else 0.0)
            # a condition's 'false' child sits below its 'true' child
            while any(abs(want - t) < 0.99 for t in taken):
                want += 1
            row[n] = want
            taken.append(want)
    for n in nodes:
        n["position"] = {"x": X0 + layer[n["id"]] * COL_W, "y": Y0 + int(row[n["id"]] * ROW_H)}
    out = dict(graph)
    out["nodes"] = nodes
    out["layout"] = "lr"
    return out


def _node(node_id: str, node_type: str, data: dict) -> dict:
    return {"id": node_id, "type": node_type, "position": {"x": 0, "y": 0}, "data": data}


def _edge(edge_id: str, source: str, target: str, handle: str | None = None) -> dict:
    edge = {"id": edge_id, "source": source, "target": target}
    if handle:
        edge["sourceHandle"] = handle
    return edge


def build_payroll_schedule_graph(*, employer_id: str, employer_name: str, day: int | None, last_day: bool = False,
                                 time: str = "09:00", timezone: str = "Asia/Kolkata",
                                 weekend_rule: str = "as_is") -> dict:
    """Schedule -> Prepare payroll run -> (funded) email approvers -> wait for approval -> notify;
    (short) funding reminder -> alert employer contact. Never approves or pays."""
    trigger: dict = {"label": f"{employer_name}: " + ("last day of the month" if last_day else f"monthly on day {day}"),
                     "cadence": "monthly", "time": time, "timezone": timezone, "weekend_rule": weekend_rule,
                     "employer_id": employer_id}
    if last_day:
        trigger["last_day"] = True
    else:
        trigger["day"] = day
    graph = {
        "nodes": [
            _node("trigger", "trigger.schedule", trigger),
            _node("prepare", "action.prepare_payroll_run", {"label": "Prepare payroll run"}),
            _node("check", "condition.field_check",
                  {"label": "Wallet covers it?", "field": "shortfall", "operator": "eq", "value": "0"}),
            _node("email-approvers", "action.send_email",
                  {"label": "Email approvers", "template": "approval_requested", "recipient": "decider"}),
            _node("gate", "gate.wait_for_approval", {"label": "Wait for approval", "entity": "run"}),
            _node("notice-approved", "action.post_chat_notice",
                  {"label": "Approved notice",
                   "message": "Payroll for {employer_name} was approved. Execution continues in the payroll module."}),
            _node("reminder", "action.create_funding_reminder",
                  {"label": "Funding reminder", "note": "Top up {employer_name} before the payroll run executes."}),
            _node("email-employer", "action.send_email",
                  {"label": "Alert employer contact", "template": "payroll_held_alert",
                   "recipient": "employer_contact"}),
        ],
        "edges": [
            _edge("e1", "trigger", "prepare"), _edge("e2", "prepare", "check"),
            _edge("e3", "check", "email-approvers", "true"), _edge("e4", "email-approvers", "gate"),
            _edge("e5", "gate", "notice-approved"),
            _edge("e6", "check", "reminder", "false"), _edge("e7", "reminder", "email-employer"),
        ],
    }
    return layout_graph(graph)


_DAY_RES = (re.compile(r"\bday\s+(\d{1,2})\b", re.I), re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.I),
            re.compile(r"\bon\s+the\s+(\d{1,2})\b", re.I), re.compile(r"\bon\s+(\d{1,2})\b", re.I))
_TIME_RE = re.compile(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b")


def parse_payroll_schedule_intent(text: str) -> dict | None:
    """Keyless intent matcher: 'monthly payroll for <employer> on day N' -> {day|last_day, time, weekend_rule}.

    Returns None unless the text is clearly a monthly payroll schedule request with a day. The employer is
    matched by the caller (it needs the organisation's employer list)."""
    lowered = text.casefold()
    if "payroll" not in lowered or not any(w in lowered for w in ("monthly", "every month", "each month", "month")):
        return None
    out: dict = {"time": "09:00", "weekend_rule": "as_is"}
    if "last day" in lowered:
        out["last_day"], out["day"] = True, None
    else:
        for rx in _DAY_RES:
            m = rx.search(text)
            if m and 1 <= int(m.group(1)) <= 31:
                out["day"], out["last_day"] = int(m.group(1)), False
                break
        else:
            return None
    t = _TIME_RE.search(text)
    if t:
        out["time"] = f"{int(t.group(1)):02d}:{t.group(2)}"
    if "previous business day" in lowered or "business day before" in lowered:
        out["weekend_rule"] = "previous_business_day"
    elif "next business day" in lowered:
        out["weekend_rule"] = "next_business_day"
    return out
