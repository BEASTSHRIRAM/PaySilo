"""The workflow node registry: every node type, its config fields and its connection rules, as plain data.

Served by ``GET /workflows/node-registry`` and handed to the Silo agent when it drafts a graph, so both see exactly
the vocabulary the validator accepts. There is deliberately no approve / pay / purchase node: a person decides
on the real screen after a ``gate.wait_for_approval``.
"""

from __future__ import annotations

from .workflows import (
    ACTION_TYPES, CONDITION_TYPES, EMAIL_RECIPIENTS, GATE_ENTITIES, GATE_TYPES, OPERATORS, TRIGGER_TYPES,
)

_LABELS = {
    "trigger.employer_added": ("Employer added", "Runs when a new employer is added."),
    "trigger.contractor_joined": ("Contractor joined", "Runs when a contractor joins an employer."),
    "trigger.payroll_held": ("Payroll held", "Runs when a payroll run is held (shortfall or human hold)."),
    "trigger.payroll_settled": ("Payroll settled", "Runs when a payroll run settles."),
    "trigger.benefit_requested": ("Benefit requested", "Runs when a benefit request is created."),
    "trigger.trip_proposed": ("Trip proposed", "Runs when a trip is proposed."),
    "trigger.kit_approved": ("Kit approved", "Runs when an onboarding kit is approved."),
    "trigger.kit_completed": ("Kit completed", "Runs when an onboarding kit is completed."),
    "trigger.schedule": ("Schedule", "Runs on a calendar: monthly on a day, weekly on a weekday, or once on a date."),
    "trigger.funds_received": ("Funds received", "Runs when funds land in an employer's wallet."),
    "condition.field_check": ("Field check", "Branches on a fact of the run: 'true' (yes) or 'false' (no) output."),
    "gate.wait_for_approval": ("Wait for approval", "Parks the run until a human decides on the real screen."),
    "action.draft_onboarding_kit": ("Draft onboarding kit", "Drafts an onboarding kit for the triggering contractor."),
    "action.create_approval_request": ("Create approval request", "Opens a pending approval for a human."),
    "action.send_email": ("Send email", "Queues a standard email in Notifications."),
    "action.create_exception": ("Create exception", "Raises an item in the Exceptions inbox."),
    "action.post_chat_notice": ("Post chat notice", "Posts a notice message to the Silo chat."),
    "action.create_funding_reminder": ("Create funding reminder", "Creates a funding reminder for the employer."),
    "action.prepare_payroll_run": ("Prepare payroll run",
                                   "Prices the employer's payroll and leaves it RESERVED for human approval. "
                                   "Never approves or pays."),
    "action.revalidate_held_run": ("Revalidate held run", "Re-checks whether held payroll lines now fit the wallet."),
}


def _opts(values) -> list[dict]:
    return [{"value": v, "label": v.replace("_", " ")} for v in values]


def _email_templates() -> list[str]:
    from .spend_mail import TEMPLATE_NAMES

    return list(TEMPLATE_NAMES)


def _fields(node_type: str) -> list[dict]:
    from . import workflow_schedule as sched

    if node_type == "trigger.schedule":
        return [
            {"key": "cadence", "kind": "select", "required": True, "options": _opts(sched.CADENCES),
             "help": "monthly | weekly | once"},
            {"key": "day", "kind": "number", "required": False,
             "help": "monthly: day of month 1-31 (shorter months use their last day); weekly: 0=Monday..6=Sunday"},
            {"key": "last_day", "kind": "boolean", "help": "monthly: run on the last day of every month"},
            {"key": "date", "kind": "text", "help": "once: YYYY-MM-DD"},
            {"key": "time", "kind": "text", "help": "24h local time, e.g. 09:00 (default 09:00)"},
            {"key": "timezone", "kind": "text", "help": f"IANA zone, default {sched.DEFAULT_TIMEZONE}"},
            {"key": "weekend_rule", "kind": "select", "options": _opts(sched.WEEKEND_RULES),
             "help": "weekends only; public holidays are not modelled"},
            {"key": "employer_id", "kind": "employer",
             "help": "scope to ONE employer (required for payroll actions unless fan_out is 'employers')"},
            {"key": "fan_out", "kind": "select", "options": _opts(("org", "employers")),
             "help": "'employers' runs once per employer"},
        ]
    if node_type == "condition.field_check":
        return [{"key": "field", "kind": "text", "required": True,
                 "help": "a fact of the run, e.g. shortfall, amount, status, employer_name, currency"},
                {"key": "operator", "kind": "select", "options": _opts(OPERATORS)},
                {"key": "value", "kind": "text"}]
    if node_type == "gate.wait_for_approval":
        return [{"key": "entity", "kind": "select", "options": _opts(GATE_ENTITIES),
                 "help": "what the human decides on; use 'run' after Prepare payroll run"}]
    if node_type == "action.send_email":
        return [{"key": "template", "kind": "select", "required": True, "options": _opts(_email_templates())},
                {"key": "recipient", "kind": "select", "options": _opts(EMAIL_RECIPIENTS)}]
    if node_type == "action.create_exception":
        return [{"key": "title", "kind": "text", "required": True}, {"key": "detail", "kind": "textarea"}]
    if node_type == "action.post_chat_notice":
        return [{"key": "message", "kind": "textarea", "required": True,
                 "help": "may use {employer_name}, {currency}, {shortfall} ..."}]
    if node_type == "action.create_funding_reminder":
        return [{"key": "note", "kind": "textarea"}]
    return []


def node_registry() -> dict:
    """Plain-data registry: node types with config fields and handles, plus the connection rules."""
    nodes = []
    for node_type in sorted(TRIGGER_TYPES | CONDITION_TYPES | GATE_TYPES | ACTION_TYPES):
        kind = node_type.split(".", 1)[0]
        label, description = _LABELS[node_type]
        nodes.append({
            "type": node_type, "kind": kind, "label": label, "description": description,
            "fields": _fields(node_type),
            "inputs": [] if kind == "trigger" else ["in"],
            "outputs": ["true", "false"] if kind == "condition" else ["out"],
        })
    return {
        "nodes": nodes,
        "rules": [
            "Exactly one trigger node; a trigger has no input (nothing connects into it).",
            "Connections flow left to right: an edge goes from a node's output (right) to another node's input (left).",
            "A condition has two outputs: sourceHandle 'true' (yes) and 'false' (no); other nodes have one output.",
            "No self-connections, no duplicate edges, no cycles.",
            "A scheduled payroll workflow (Prepare payroll run / Revalidate held run) must name one employer "
            "(trigger.schedule data.employer_id) or set fan_out to 'employers'.",
            "There is no approve / pay / purchase node. Use gate.wait_for_approval; a person decides on the real screen.",
        ],
        "graph_shape": {"nodes": [{"id": "str", "type": "str", "position": {"x": 0, "y": 0}, "data": {}}],
                        "edges": [{"id": "str", "source": "node id", "target": "node id",
                                   "sourceHandle": "'true' | 'false' | null"}]},
    }
