"""Spend Workflows — a deterministic workflow engine.

Domain events fire saved node graphs; human gates park runs durably;
there is NO LLM at runtime and NO approve/purchase/pay action in the
graph vocabulary. The only way a parked run moves again is a human
decision recorded through :func:`resolve_approval_for_entity` (wired by
the kit/trip decision routes — see the lane report for wiring points).

Execution model
---------------
``dispatch_event`` creates one :class:`WorkflowRun` per ENABLED workflow
in the organization whose (denormalised) ``trigger_type`` matches the
event, then executes it synchronously until it completes, parks at a
wait gate (``WAITING``), or fails (``FAILED`` with ``error_detail``).
Dedupe: the pair the DB enforces is (workflow, event, entity_kind,
entity_id) — dispatching the same event for the same entity twice
creates ONE run; ``dispatch_event`` returns only newly created runs.

Traversal is depth-first in edge order. A condition node branches on
the ``true``/``false`` sourceHandle of its outgoing edges; every other
node follows its outgoing edge(s) in edge order. Every node execution
writes a :class:`WorkflowRunStep`, including the trigger itself.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import (
    BenefitRequest,
    ChatMessage,
    ChatThread,
    Contractor,
    Employer,
    LedgerEntry,
    Membership,
    Organization,
    PayLine,
    PayrollRun,
    User,
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowTask,
    utcnow,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary (exact strings — the frontend canvas uses the same)
# ---------------------------------------------------------------------------

TRIGGER_TYPES = {
    "trigger.employer_added", "trigger.contractor_joined", "trigger.payroll_held",
    "trigger.payroll_settled", "trigger.benefit_requested", "trigger.trip_proposed",
    "trigger.kit_approved", "trigger.kit_completed", "trigger.schedule",
    "trigger.funds_received",
}
CONDITION_TYPES = {"condition.field_check"}
GATE_TYPES = {"gate.wait_for_approval"}
ACTION_TYPES = {
    "action.draft_onboarding_kit", "action.create_approval_request", "action.send_email",
    "action.create_exception", "action.post_chat_notice", "action.create_funding_reminder",
    "action.prepare_payroll_run", "action.revalidate_held_run",
}
KNOWN_TYPES = TRIGGER_TYPES | CONDITION_TYPES | GATE_TYPES | ACTION_TYPES
OPERATORS = ("eq", "neq", "gt", "gte", "lt", "lte", "contains")
EMAIL_RECIPIENTS = ("decider", "contractor", "employer_contact", "operator")
GATE_ENTITIES = ("trigger", "kit", "trip", "run")
DECIDER_ROLES = ("owner", "admin", "approver")

# Event-name aliases -> canonical trigger types. Callers in routes and
# runtime use short domain names ("employer.created"); saved graphs use
# the trigger.* vocabulary. Both normalise to the trigger type.
_EVENT_ALIASES = {
    "employer.created": "trigger.employer_added",
    "employer_added": "trigger.employer_added",
    "employer.added": "trigger.employer_added",
    "contractor.created": "trigger.contractor_joined",
    "contractor_joined": "trigger.contractor_joined",
    "contractor.joined": "trigger.contractor_joined",
    "payroll.held": "trigger.payroll_held",
    "payroll_held": "trigger.payroll_held",
    "payroll.settled": "trigger.payroll_settled",
    "payroll_settled": "trigger.payroll_settled",
    "benefit.requested": "trigger.benefit_requested",
    "benefit_requested": "trigger.benefit_requested",
    "trip.proposed": "trigger.trip_proposed",
    "trip_proposed": "trigger.trip_proposed",
    "kit.approved": "trigger.kit_approved",
    "kit_approved": "trigger.kit_approved",
    "kit.completed": "trigger.kit_completed",
    "kit_completed": "trigger.kit_completed",
    "funds.received": "trigger.funds_received",
    "funds_received": "trigger.funds_received",
}
# PayrollRun statuses that count as "held" for dispatch purposes.
HELD_RUN_STATUSES = ("HELD_SHORTFALL", "HELD_BY_HUMAN")

# Overridable lazy-import hooks (tests may set these directly, or stub
# ``sys.modules`` for the real parallel-lane modules — both work).
draft_kit_from_template = None  # app.services.onboarding.draft_kit_from_template
enqueue_email = None  # app.services.email.enqueue_email
prepare_payroll_run = None  # hook: (s, *, employer, settings) -> PayrollRun
revalidate_held_run = None  # hook: (s, *, run, employer) -> dict {now_funded, shortfall, balance, total_required}


class WorkflowError(Exception):
    """A workflow refusal with a stable machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def normalize_event(event_type: str) -> str:
    key = (event_type or "").strip()
    if key in TRIGGER_TYPES:
        return key
    return _EVENT_ALIASES.get(key, _EVENT_ALIASES.get(key.lower(), key))


def trigger_type_of(graph: dict | None) -> str | None:
    for node in (graph or {}).get("nodes") or []:
        if isinstance(node, dict) and node.get("type") in TRIGGER_TYPES:
            return node["type"]
    return None


# ---------------------------------------------------------------------------
# Graph validation
# ---------------------------------------------------------------------------

def _is_forbidden_action(node_type: str) -> bool:
    lowered = node_type.lower()
    if lowered in GATE_TYPES:
        return False  # the gate is a wait, not an action
    if not lowered.startswith("action."):
        return False
    # Token-based, not substring: "pay" must not flag "payroll"
    # (action.prepare_payroll_run is a legitimate non-money action).
    import re

    parts = set(re.split(r"[._\-]", lowered))
    return bool(parts & {"approve", "approves", "approval_action", "purchase", "purchases",
                         "pay", "pays", "payment", "payments", "buy", "charge", "refund"})


def validate_graph(graph) -> list[str]:
    """Return a list of human-readable errors; empty means valid."""
    errors: list[str] = []
    if not isinstance(graph, dict):
        return ["Graph must be an object with 'nodes' and 'edges'."]
    nodes = graph.get("nodes")
    edges = graph.get("edges") or []
    if not isinstance(nodes, list) or not nodes:
        return ["Graph must contain at least one node."]
    if not isinstance(edges, list):
        return ["Graph 'edges' must be a list."]

    node_ids: set[str] = set()
    trigger_count = 0
    node_types: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            errors.append("Every node must be an object with 'id' and 'type'.")
            continue
        node_id, node_type = node.get("id"), node.get("type")
        if not node_id or not isinstance(node_id, str):
            errors.append("Every node must have a string 'id'.")
            continue
        if node_id in node_ids:
            errors.append(f"Duplicate node id '{node_id}'.")
        node_ids.add(node_id)
        if not node_type or not isinstance(node_type, str):
            errors.append(f"Node '{node_id}' must have a 'type'.")
            continue
        node_types[node_id] = node_type
        if _is_forbidden_action(node_type):
            errors.append(
                f"Node '{node_id}': '{node_type}' is forbidden — workflows never approve, "
                "purchase or pay. Use gate.wait_for_approval to park for a human decision.")
            continue
        if node_type not in KNOWN_TYPES:
            errors.append(f"Node '{node_id}': unknown node type '{node_type}'.")
            continue
        if node_type in TRIGGER_TYPES:
            trigger_count += 1
        data = node.get("data") or {}
        if not isinstance(data, dict):
            errors.append(f"Node '{node_id}': 'data' must be an object.")
            continue
        if node_type == "trigger.schedule":
            cadence = data.get("cadence")
            if cadence not in ("monthly", "weekly"):
                errors.append(f"Node '{node_id}': trigger.schedule requires data.cadence "
                              "('monthly' or 'weekly').")
            day = data.get("day")
            if not isinstance(day, int) or isinstance(day, bool):
                errors.append(f"Node '{node_id}': trigger.schedule requires integer data.day "
                              "(day of month for monthly, 0-6 weekday for weekly).")
            elif cadence == "monthly" and not 1 <= day <= 31:
                errors.append(f"Node '{node_id}': monthly schedule day must be 1-31.")
            elif cadence == "weekly" and not 0 <= day <= 6:
                errors.append(f"Node '{node_id}': weekly schedule day must be 0-6 (Mon-Sun).")
            if data.get("fan_out", "org") not in ("org", "employers"):
                errors.append(f"Node '{node_id}': trigger.schedule fan_out must be 'org' or 'employers'.")
        if node_type == "condition.field_check":
            if not data.get("field"):
                errors.append(f"Node '{node_id}': condition.field_check requires data.field.")
            if data.get("operator", "eq") not in OPERATORS:
                errors.append(f"Node '{node_id}': operator must be one of {', '.join(OPERATORS)}.")
        if node_type == "gate.wait_for_approval":
            entity = data.get("entity", "trigger")
            if entity not in GATE_ENTITIES:
                errors.append(
                    f"Node '{node_id}': wait gate entity must be one of "
                    f"{', '.join(GATE_ENTITIES)} (got '{entity}') — a gate with no "
                    "resolvable entity cannot park.")
            elif entity == "kit" and "action.draft_onboarding_kit" not in node_types.values() \
                    and trigger_type_of(graph) not in ("trigger.kit_approved", "trigger.kit_completed"):
                # No kit is created by this graph and the trigger is not a
                # kit, so a kit gate can never resolve.
                errors.append(
                    f"Node '{node_id}': wait gate for entity 'kit' has no resolvable entity — "
                    "no node in this graph drafts a kit and the trigger is not a kit event.")
        if node_type == "action.send_email":
            if not data.get("template"):
                errors.append(f"Node '{node_id}': action.send_email requires data.template.")
            if data.get("recipient", "decider") not in EMAIL_RECIPIENTS:
                errors.append(
                    f"Node '{node_id}': send_email recipient must be one of "
                    f"{', '.join(EMAIL_RECIPIENTS)}.")
    if trigger_count != 1:
        errors.append(f"Graph must have exactly one trigger node (found {trigger_count}).")

    for edge in edges:
        if not isinstance(edge, dict):
            errors.append("Every edge must be an object with 'source' and 'target'.")
            continue
        for end in ("source", "target"):
            if edge.get(end) not in node_ids:
                errors.append(f"Edge '{edge.get('id', '?')}': {end} references missing node "
                              f"'{edge.get(end)}'.")
    return errors


# ---------------------------------------------------------------------------
# Facts / entity snapshots
# ---------------------------------------------------------------------------

def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _parse_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _residual_shortfall(run) -> str | None:
    """Residual shortfall for partially settled runs (PP lane), else None."""
    value = getattr(run, "residual_shortfall", None)
    if value is None and isinstance(getattr(run, "assessment", None), dict):
        value = run.assessment.get("residual_shortfall")
    return str(value) if value is not None else None


def _generic_facts(obj) -> dict:
    """Flat scalar facts from any ORM row (kit/trip models included)."""
    out: dict = {}
    for key, value in vars(obj).items():
        if key.startswith("_"):
            continue
        if isinstance(value, (str, int, float, bool, Decimal, uuid.UUID)) or value is None:
            out[key] = _jsonable(value)
    return out


def _entity_facts(s: Session, organization_id: uuid.UUID, entity_kind: str, entity_id: str) -> dict:
    """Snapshot facts for the trigger entity. Best-effort: {} if unknown."""
    kind = (entity_kind or "").lower()
    eid = _parse_uuid(entity_id)
    facts: dict = {"entity_kind": entity_kind, "entity_id": entity_id}
    try:
        if kind == "employer" and eid:
            emp = s.get(Employer, eid)
            if emp is not None and emp.organization_id == organization_id:
                facts.update({"employer_id": str(emp.id), "employer_name": emp.name,
                              "status": emp.status, "currency": emp.funding_currency,
                              "contact_email": emp.contact_email, "contact_name": emp.contact_name})
        elif kind in ("contractor",) and eid:
            c = s.get(Contractor, eid)
            if c is not None and c.organization_id == organization_id:
                emp = s.get(Employer, c.employer_id)
                facts.update({"contractor_id": str(c.id), "contractor_name": c.full_name,
                              "contractor_email": c.email, "email": c.email,
                              "employer_id": str(c.employer_id),
                              "employer_name": emp.name if emp else None,
                              "currency": c.currency,
                              "amount": str(c.default_amount) if c.default_amount is not None else None,
                              "status": "ACTIVE" if c.is_active else "INACTIVE"})
        elif kind in ("payroll_run", "run", "payroll") and eid:
            run = s.get(PayrollRun, eid)
            if run is not None and run.organization_id == organization_id:
                emp = s.get(Employer, run.employer_id)
                amount = run.shortfall_usd if run.shortfall_usd is not None else run.required_usd
                facts.update({"run_id": str(run.id), "employer_id": str(run.employer_id),
                              "employer_name": emp.name if emp else None,
                              "status": run.status, "currency": run.payroll_currency or "USD",
                              "amount": str(amount) if amount is not None else None,
                              "shortfall_usd": str(run.shortfall_usd) if run.shortfall_usd is not None else None,
                              "residual_shortfall": _residual_shortfall(run),
                              "required_usd": str(run.required_usd) if run.required_usd is not None else None})
        elif kind in ("benefit_request", "benefit") and eid:
            req = s.get(BenefitRequest, eid)
            if req is not None and req.organization_id == organization_id:
                emp = s.get(Employer, req.employer_id)
                facts.update({"employer_id": str(req.employer_id),
                              "employer_name": emp.name if emp else None,
                              "contractor_id": str(req.contractor_id) if req.contractor_id else None,
                              "contractor_name": req.contractor_name, "merchant": req.merchant,
                              "item": req.item, "kind": "benefit",
                              "amount": str(req.amount), "currency": req.currency, "status": req.status})
        elif kind in ("kit", "onboarding_kit", "trip", "trip_request") and eid:
            model = None
            models = importlib.import_module("app.db.models")
            model = getattr(models, "OnboardingKit", None) if "kit" in kind else getattr(models, "TripRequest", None)
            if model is not None:
                obj = s.get(model, eid)
                if obj is not None and getattr(obj, "organization_id", organization_id) == organization_id:
                    generic = _generic_facts(obj)
                    facts.update(generic)
                    if "kit" in kind:
                        facts.setdefault("kit_id", str(obj.id))
                    else:
                        facts.setdefault("trip_id", str(obj.id))
                    emp_id = generic.get("employer_id")
                    emp = s.get(Employer, _parse_uuid(emp_id)) if emp_id else None
                    if emp is not None:
                        facts.setdefault("employer_name", emp.name)
                    facts.setdefault("amount", generic.get("total") or generic.get("total_amount")
                                     or generic.get("amount") or generic.get("estimated_total"))
    except Exception:  # snapshot is best-effort; dispatch facts still apply
        logger.exception("workflow entity snapshot failed for %s %s", entity_kind, entity_id)
    return {k: v for k, v in facts.items() if v is not None}


def _run_facts(s: Session, run: WorkflowRun) -> dict:
    """Entity snapshot (base) + dispatch facts + created ids (overrides)."""
    context = run.context or {}
    facts = _entity_facts(s, run.organization_id, run.entity_kind, run.entity_id)
    facts.update(context.get("facts") or {})
    for key in ("kit_id", "trip_id", "approval_id"):
        if context.get(key):
            facts[key] = context[key]
    facts.setdefault("entity_kind", run.entity_kind)
    facts.setdefault("entity_id", run.entity_id)
    return facts


def _resolve_employer(s: Session, run: WorkflowRun, facts: dict) -> Employer | None:
    for candidate in (facts.get("employer_id"),):
        emp = s.get(Employer, _parse_uuid(candidate)) if candidate else None
        if emp is not None and emp.organization_id == run.organization_id:
            return emp
    kind, eid = (run.entity_kind or "").lower(), _parse_uuid(run.entity_id)
    if kind == "employer" and eid:
        emp = s.get(Employer, eid)
        return emp if emp is not None and emp.organization_id == run.organization_id else None
    if kind == "contractor" and eid:
        c = s.get(Contractor, eid)
        if c is not None:
            return s.get(Employer, c.employer_id)
    if kind in ("payroll_run", "run") and eid:
        r = s.get(PayrollRun, eid)
        if r is not None:
            return s.get(Employer, r.employer_id)
    if kind in ("benefit_request", "benefit") and eid:
        r = s.get(BenefitRequest, eid)
        if r is not None:
            return s.get(Employer, r.employer_id)
    return None


def _resolve_contractor(s: Session, run: WorkflowRun, facts: dict) -> Contractor | None:
    for candidate in (facts.get("contractor_id"), run.entity_id if run.entity_kind == "contractor" else None):
        c = s.get(Contractor, _parse_uuid(candidate)) if candidate else None
        if c is not None and c.organization_id == run.organization_id:
            return c
    # Kit path: the kit's contractor, when the kit model is present.
    kit_id = (run.context or {}).get("kit_id") or (run.entity_id if "kit" in (run.entity_kind or "") else None)
    if kit_id:
        models = importlib.import_module("app.db.models")
        kit_model = getattr(models, "OnboardingKit", None)
        if kit_model is not None:
            kit = s.get(kit_model, _parse_uuid(kit_id))
            contractor_id = getattr(kit, "contractor_id", None) if kit is not None else None
            if contractor_id:
                return s.get(Contractor, contractor_id)
    return None


def _approval_target(run: WorkflowRun) -> tuple[str, str]:
    """Entity an approval concerns: created kit, else created trip, else trigger."""
    context = run.context or {}
    if context.get("kit_id"):
        return "kit", str(context["kit_id"])
    if context.get("trip_id"):
        return "trip", str(context["trip_id"])
    run_id = _context_run_id(run)
    if run_id:
        return "run", run_id
    return run.entity_kind, run.entity_id


def _context_run_id(run: WorkflowRun) -> str | None:
    context = run.context or {}
    for key in ("run_id", "payroll_run_id"):
        if context.get(key):
            return str(context[key])
    facts = context.get("facts") or {}
    for key in ("run_id", "payroll_run_id"):
        if facts.get(key):
            return str(facts[key])
    if (run.entity_kind or "") in ("payroll_run", "run"):
        return str(run.entity_id)
    return None


def _gate_target(run: WorkflowRun, entity: str) -> tuple[str, str] | None:
    context = run.context or {}
    if entity == "trigger":
        return run.entity_kind, run.entity_id
    if entity == "kit":
        if context.get("kit_id"):
            return "kit", str(context["kit_id"])
        if "kit" in (run.entity_kind or ""):
            return run.entity_kind, run.entity_id
        return None
    if entity == "trip":
        if context.get("trip_id"):
            return "trip", str(context["trip_id"])
        if "trip" in (run.entity_kind or ""):
            return run.entity_kind, run.entity_id
        return None
    if entity == "run":
        run_id = _context_run_id(run)
        return ("run", run_id) if run_id else None
    return run.entity_kind, run.entity_id


def _get_or_create_approval(s: Session, run: WorkflowRun, entity_kind: str, entity_id: str) -> WorkflowApproval:
    existing = s.scalar(
        select(WorkflowApproval).where(
            WorkflowApproval.workflow_run_id == run.id,
            WorkflowApproval.entity_kind == entity_kind,
            WorkflowApproval.entity_id == str(entity_id),
            WorkflowApproval.status == "PENDING")
        .order_by(WorkflowApproval.created_at))
    if existing is not None:
        return existing
    approval = WorkflowApproval(organization_id=run.organization_id, workflow_run_id=run.id,
                                entity_kind=entity_kind, entity_id=str(entity_id), status="PENDING")
    s.add(approval)
    s.flush()
    return approval


def _decision_for(s: Session, run: WorkflowRun, entity_kind: str, entity_id: str) -> WorkflowApproval | None:
    """Latest decided approval for this entity — this run's first, then any."""
    base = select(WorkflowApproval).where(
        WorkflowApproval.organization_id == run.organization_id,
        WorkflowApproval.entity_kind == entity_kind,
        WorkflowApproval.entity_id == str(entity_id),
        WorkflowApproval.status != "PENDING")
    own = s.scalar(base.where(WorkflowApproval.workflow_run_id == run.id)
                   .order_by(WorkflowApproval.created_at.desc()))
    if own is not None:
        return own
    return s.scalar(base.order_by(WorkflowApproval.created_at.desc()))


# ---------------------------------------------------------------------------
# Lazy parallel-lane services
# ---------------------------------------------------------------------------

def _draft_kit_fn():
    fn = globals().get("draft_kit_from_template")
    if callable(fn):
        return fn
    module = importlib.import_module("app.services.onboarding")
    return getattr(module, "draft_kit_from_template")


def _enqueue_email_fn():
    fn = globals().get("enqueue_email")
    if callable(fn):
        return fn
    module = importlib.import_module("app.services.email")
    return getattr(module, "enqueue_email")


def _email_context(s: Session, run: WorkflowRun, facts: dict) -> dict:
    """Template context from facts + the related entity's own snapshot."""
    context = dict(facts)
    kind, entity_id = _approval_target(run) if (run.context or {}).get("kit_id") else (run.entity_kind, run.entity_id)
    related_facts = _entity_facts(s, run.organization_id, kind, entity_id)
    for key, value in related_facts.items():
        context.setdefault(key, value)
    # Kit/trip totals for approval_requested-style templates, when present.
    models = importlib.import_module("app.db.models")
    for kind_name, model_name in (("kit", "OnboardingKit"), ("trip", "TripRequest")):
        ref = (run.context or {}).get(f"{kind_name}_id") or (entity_id if kind == kind_name else None)
        model = getattr(models, model_name, None)
        if ref and model is not None:
            obj = s.get(model, _parse_uuid(ref))
            if obj is not None:
                generic = _generic_facts(obj)
                items = getattr(obj, "items", None)
                lines = []
                if items is not None:
                    try:
                        lines = [{"label": str(getattr(i, "label", None) or getattr(i, "name", None) or ""),
                                  "amount": _jsonable(getattr(i, "amount", None) or getattr(i, "price", None))}
                                 for i in list(items)]
                    except TypeError:
                        lines = []
                total = generic.get("total") or generic.get("total_amount") or generic.get("estimated_total") \
                    or generic.get("amount")
                context.setdefault(f"{kind_name}_lines", lines)
                context.setdefault("lines", lines)
                if total is None and lines:
                    try:
                        from decimal import Decimal as _Dec

                        total = str(sum((_Dec(str(l.get("amount") or "0")) for l in lines),
                                        _Dec("0")))
                    except Exception:  # noqa: BLE001
                        total = None
                if total is not None:
                    context.setdefault("total", total)
                    context.setdefault("amount", total)
                # The kit_completed / trip_itinerary templates read the
                # child rows under their own keys — same snapshot figures.
                context.setdefault("items", lines)
                context.setdefault("components", lines)
    context.setdefault("amount", facts.get("amount"))
    return {k: _jsonable(v) for k, v in context.items() if v is not None or k in ("lines",)}


class _SafeFormat(dict):
    def __missing__(self, key):  # leave unknown placeholders intact
        return "{" + key + "}"


def _render(template_text: str, facts: dict) -> str:
    try:
        return template_text.format_map(_SafeFormat({k: v for k, v in facts.items()}))
    except (ValueError, IndexError):
        return template_text


def _to_decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _compare(actual, operator: str, expected) -> bool:
    a_num, e_num = _to_decimal(actual), _to_decimal(expected)
    if operator == "contains":
        if isinstance(actual, (list, tuple)):
            return expected in actual or str(expected) in [str(x) for x in actual]
        return str(expected) in str(actual)
    if a_num is not None and e_num is not None and operator in ("gt", "gte", "lt", "lte"):
        return {"gt": a_num > e_num, "gte": a_num >= e_num, "lt": a_num < e_num, "lte": a_num <= e_num}[operator]
    a_str, e_str = ("" if actual is None else str(actual)), ("" if expected is None else str(expected))
    if operator == "eq":
        return a_str == e_str or (a_num is not None and e_num is not None and a_num == e_num)
    if operator == "neq":
        return not _compare(actual, "eq", expected)
    return {"gt": a_str > e_str, "gte": a_str >= e_str, "lt": a_str < e_str, "lte": a_str <= e_str}[operator]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class _RunFailure(Exception):
    """Internal: a node failed; the run is FAILED with this detail."""


def _log_step(s: Session, run: WorkflowRun, node: dict, status: str, detail: str,
              output: dict | None = None) -> WorkflowRunStep:
    now = utcnow()
    step = WorkflowRunStep(run_id=run.id, node_id=node.get("id", ""), node_type=node.get("type", ""),
                           status=status, detail=detail[:2000], output=_jsonable(output) if output else None,
                           started_at=now, finished_at=None if status == "WAITING" else now)
    s.add(step)
    s.flush()
    return step


def _fail_run(s: Session, run: WorkflowRun, detail: str) -> WorkflowRun:
    run.status = "FAILED"
    run.error_detail = detail[:2000]
    run.finished_at = utcnow()
    context = dict(run.context or {})
    context.pop("waiting_node_id", None)
    run.context = context
    s.flush()
    return run


def dispatch_event(s: Session, *, organization_id: uuid.UUID, event_type: str, entity_kind: str,
                   entity_id, facts: dict, settings=None, dedupe_suffix: str | None = None,
                   only_workflow_id: uuid.UUID | None = None) -> list[WorkflowRun]:
    """Fire every enabled workflow in the org matching this event.

    Returns only newly created runs (deduped events return []).
    """
    canonical = normalize_event(event_type)
    entity_id = str(entity_id)
    wf_query = (select(Workflow).where(Workflow.organization_id == organization_id,
                                       Workflow.enabled.is_(True),
                                       Workflow.trigger_type == canonical)
                .order_by(Workflow.created_at))
    if only_workflow_id is not None:
        wf_query = wf_query.where(Workflow.id == only_workflow_id)
    workflows = s.scalars(wf_query).all()
    created: list[WorkflowRun] = []
    for workflow in workflows:
        dedupe_key = f"{workflow.id}:{canonical}:{entity_kind}:{entity_id}"
        if dedupe_suffix:
            dedupe_key = f"{dedupe_key}:{dedupe_suffix}"
        existing = s.scalar(select(WorkflowRun).where(WorkflowRun.dedupe_key == dedupe_key))
        if existing is not None:
            continue  # dedupe: same workflow + event + entity already ran
        run = WorkflowRun(
            workflow_id=workflow.id, organization_id=organization_id, trigger_event=canonical,
            entity_kind=entity_kind, entity_id=entity_id, dedupe_key=dedupe_key, status="RUNNING",
            context={"facts": _jsonable(facts or {}), "entity_kind": entity_kind, "entity_id": entity_id})
        s.add(run)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            continue
        created.append(run)
        execute_run(s, run, settings=settings)
        s.flush()
    return created


def execute_run(s: Session, run: WorkflowRun, settings=None, *, _start_node_id: str | None = None) -> WorkflowRun:
    """Execute (or resume) one run until completion, a gate, or failure."""
    workflow = s.get(Workflow, run.workflow_id)
    if workflow is None or not workflow.graph:
        return _fail_run(s, run, "Workflow or its graph no longer exists.")
    graph = workflow.graph if isinstance(workflow.graph, dict) else json.loads(workflow.graph)
    nodes = {n["id"]: n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id")}
    outgoing: dict[str, list[dict]] = {}
    for edge in graph.get("edges") or []:
        if isinstance(edge, dict) and edge.get("source") in nodes and edge.get("target") in nodes:
            outgoing.setdefault(edge["source"], []).append(edge)

    if _start_node_id is not None:
        start = nodes.get(_start_node_id)
        if start is None:
            return _fail_run(s, run, f"Resume node '{_start_node_id}' is no longer in the graph.")
    else:
        start = next((n for n in nodes.values() if n.get("type") in TRIGGER_TYPES), None)
        if start is None:
            return _fail_run(s, run, "Graph has no trigger node.")
        run.status = "RUNNING"
        run.finished_at = None
        run.error_detail = None
        s.flush()

    visited: set[str] = set()
    resuming_from = _start_node_id

    def walk(node: dict) -> str:
        """Execute one node and its successors. Returns continue|wait|stop."""
        node_id = node["id"]
        if node_id in visited:
            _log_step(s, run, node, "SKIPPED", "Already executed in this run; skipped to avoid a loop.")
            return "continue"
        visited.add(node_id)
        node_type = node.get("type", "")
        is_resume_gate = resuming_from is not None and node_id == resuming_from and node_type in GATE_TYPES
        try:
            outcome, branch = _execute_node(s, run, node, settings=settings, facts=_run_facts(s, run),
                                            resuming=is_resume_gate)
        except _RunFailure as exc:
            _fail_run(s, run, str(exc))
            return "stop"
        if outcome == "wait":
            return "wait"
        if outcome == "stop":  # e.g. gate rejection: legitimate completion
            run.status = "COMPLETED"
            run.finished_at = utcnow()
            context = dict(run.context or {})
            context.pop("waiting_node_id", None)
            run.context = context
            s.flush()
            return "stop"
        edges = outgoing.get(node_id, [])
        if node_type in CONDITION_TYPES:
            wanted = "true" if branch else "false"
            chosen = [e for e in edges if (e.get("sourceHandle") or wanted) == wanted]
            if not chosen and edges:
                chosen = edges[:1]
            for edge in chosen:
                if walk(nodes[edge["target"]]) != "continue":
                    return "wait" if run.status == "WAITING" else "stop"
            return "continue"
        for edge in edges:
            result = walk(nodes[edge["target"]])
            if result != "continue":
                return result
        return "continue"

    result = walk(start)
    if result == "continue" and run.status not in ("WAITING", "FAILED"):
        run.status = "COMPLETED"
        run.finished_at = utcnow()
        s.flush()
    return run


def _execute_node(s: Session, run: WorkflowRun, node: dict, *, settings, facts: dict,
                  resuming: bool = False) -> tuple[str, bool | None]:
    """Run one node, log its step. Returns (outcome, condition_branch)."""
    node_type = node.get("type", "")
    data = node.get("data") or {}

    if node_type in TRIGGER_TYPES:
        _log_step(s, run, node, "OK", f"Triggered by {run.trigger_event} on {run.entity_kind} {run.entity_id}.")
        return "continue", None

    if node_type == "condition.field_check":
        field, operator = data.get("field", ""), data.get("operator", "eq")
        expected = data.get("value")
        if field not in facts:
            _log_step(s, run, node, "FAILED", f"Condition field '{field}' is not in the run facts.")
            raise _RunFailure(f"Condition field '{field}' is not available for node '{node['id']}'.")
        actual = facts[field]
        result = _compare(actual, operator, expected)
        _log_step(s, run, node, "OK",
                  f"Checked {field} {operator} {expected!r}: actual {actual!r} -> {'true' if result else 'false'}.",
                  output={"field": field, "operator": operator, "value": _jsonable(expected),
                          "actual": _jsonable(actual), "result": result,
                          "branch": "true" if result else "false"})
        return "continue", result

    if node_type == "gate.wait_for_approval":
        return _execute_gate(s, run, node, data, resuming=resuming), None

    handler = _ACTION_HANDLERS.get(node_type)
    if handler is None:
        _log_step(s, run, node, "FAILED", f"Unknown node type '{node_type}'.")
        raise _RunFailure(f"Unknown node type '{node_type}'.")
    handler(s, run, node, data, settings=settings, facts=facts)
    return "continue", None


def _execute_gate(s: Session, run: WorkflowRun, node: dict, data: dict, *, resuming: bool) -> str:
    target = _gate_target(run, data.get("entity", "trigger"))
    if target is None:
        _log_step(s, run, node, "FAILED",
                  f"Wait gate entity '{data.get('entity')}' cannot be resolved for this run.")
        raise _RunFailure(f"Wait gate in node '{node['id']}' has no resolvable entity.")
    entity_kind, entity_id = target
    decision = _decision_for(s, run, entity_kind, entity_id)
    if decision is not None:
        # The parked WAITING step for this gate now concludes.
        waiting_step = s.scalar(
            select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id,
                                          WorkflowRunStep.node_id == node["id"],
                                          WorkflowRunStep.status == "WAITING")
            .order_by(WorkflowRunStep.created_at.desc()))
        if waiting_step is not None and waiting_step.finished_at is None:
            waiting_step.finished_at = utcnow()
            s.flush()
    if decision is not None and decision.status == "APPROVED":
        _log_step(s, run, node, "RESUMED",
                  f"Approval for {entity_kind} {entity_id} was approved; resuming.",
                  output={"entity_kind": entity_kind, "entity_id": entity_id, "approval_id": str(decision.id)})
        return "continue"
    if decision is not None and decision.status == "REJECTED":
        context = dict(run.context or {})
        context["gate_outcome"] = "rejected"
        context["gate_entity_kind"], context["gate_entity_id"] = entity_kind, entity_id
        run.context = context
        _log_step(s, run, node, "RESUMED",
                  f"Approval for {entity_kind} {entity_id} was rejected; downstream nodes not executed.",
                  output={"entity_kind": entity_kind, "entity_id": entity_id, "approval_id": str(decision.id),
                          "outcome": "rejected"})
        return "stop"
    approval = _get_or_create_approval(s, run, entity_kind, entity_id)
    context = dict(run.context or {})
    context["waiting_node_id"] = node["id"]
    context["gate_entity_kind"], context["gate_entity_id"] = entity_kind, entity_id
    run.context = context
    run.status = "WAITING"
    run.finished_at = None
    _log_step(s, run, node, "WAITING",
              f"Waiting for a human decision on {entity_kind} {entity_id}.",
              output={"entity_kind": entity_kind, "entity_id": entity_id, "approval_id": str(approval.id)})
    s.flush()
    return "wait"


# ---------------------------------------------------------------------------
# Action executors (each logs exactly one step; failures raise _RunFailure
# after logging a FAILED step, except post_chat_notice which never fails)
# ---------------------------------------------------------------------------

def _act_draft_kit(s: Session, run: WorkflowRun, node: dict, data: dict, *, settings, facts: dict) -> None:
    contractor = _resolve_contractor(s, run, facts)
    if contractor is None or run.entity_kind != "contractor":
        _log_step(s, run, node, "FAILED", "draft_onboarding_kit requires the trigger entity to be a contractor.")
        raise _RunFailure("draft_onboarding_kit: trigger entity is not a contractor.")
    employer = s.get(Employer, contractor.employer_id)
    if employer is None:
        _log_step(s, run, node, "FAILED", "draft_onboarding_kit: contractor's employer no longer exists.")
        raise _RunFailure("draft_onboarding_kit: employer not found.")
    try:
        fn = _draft_kit_fn()
    except Exception:
        _log_step(s, run, node, "FAILED", "onboarding service unavailable")
        raise _RunFailure("onboarding service unavailable") from None
    try:
        kit = fn(s, organization_id=run.organization_id, employer=employer, contractor=contractor,
                 created_by=None)
    except Exception as exc:
        _log_step(s, run, node, "FAILED", f"draft_onboarding_kit failed: {exc}")
        raise _RunFailure(f"draft_onboarding_kit failed: {exc}") from None
    kit_id = str(getattr(kit, "id", ""))
    context = dict(run.context or {})
    context["kit_id"] = kit_id
    run.context = context
    s.flush()
    _log_step(s, run, node, "OK", f"Drafted onboarding kit {kit_id} for {contractor.full_name}.",
              output={"kit_id": kit_id})


def _act_create_approval(s: Session, run: WorkflowRun, node: dict, data: dict, *, settings, facts: dict) -> None:
    entity_kind, entity_id = _approval_target(run)
    approval = _get_or_create_approval(s, run, entity_kind, entity_id)
    context = dict(run.context or {})
    context["approval_id"] = str(approval.id)
    run.context = context
    s.flush()
    _log_step(s, run, node, "OK",
              f"Approval request {approval.id} is pending for {entity_kind} {entity_id}.",
              output={"approval_id": str(approval.id), "entity_kind": entity_kind, "entity_id": entity_id})


def _recipient_addresses(s: Session, run: WorkflowRun, recipient: str, facts: dict) -> list[str]:
    if recipient == "decider":
        rows = s.execute(
            select(User.email).join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == run.organization_id,
                   Membership.role.in_(DECIDER_ROLES))).all()
        return sorted({row[0] for row in rows if row[0]})
    if recipient == "operator":
        row = s.execute(
            select(User.email).join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == run.organization_id, Membership.role == "owner")
            .order_by(Membership.created_at)).first()
        return [row[0]] if row and row[0] else []
    if recipient == "contractor":
        contractor = _resolve_contractor(s, run, facts)
        if contractor is not None and contractor.email:
            return [contractor.email]
        return [facts["contractor_email"]] if facts.get("contractor_email") else []
    if recipient == "employer_contact":
        employer = _resolve_employer(s, run, facts)
        if employer is not None and employer.contact_email:
            return [employer.contact_email]
        return [facts["contact_email"]] if facts.get("contact_email") else []
    return []


def _act_send_email(s: Session, run: WorkflowRun, node: dict, data: dict, *, settings, facts: dict) -> None:
    template = data.get("template")
    recipient = data.get("recipient", "decider")
    if template == "payslip_receipt" and recipient == "contractor":
        _act_send_payslips(s, run, node, data, settings=settings, facts=facts)
        return
    if not template:
        _log_step(s, run, node, "FAILED", "send_email requires data.template.")
        raise _RunFailure("send_email: no template configured.")
    try:
        fn = _enqueue_email_fn()
    except Exception:
        _log_step(s, run, node, "FAILED", "email service unavailable")
        raise _RunFailure("email service unavailable") from None
    addresses = _recipient_addresses(s, run, recipient, facts)
    if not addresses:
        detail = f"No email address resolved for recipient '{recipient}'."
        _log_step(s, run, node, "FAILED", detail)
        raise _RunFailure(detail)
    related_kind, related_id = _approval_target(run)
    employer = _resolve_employer(s, run, facts)
    related_label = None
    if related_kind == "kit":
        related_label = "Onboarding kit"
    elif employer is not None:
        related_label = employer.name
    email_context = _email_context(s, run, facts)
    if settings is None:
        from ..config import load_settings
        settings = load_settings()
    outbox_ids: list[str] = []
    not_configured = False
    for address in addresses:
        try:
            row = fn(s, settings, organization_id=run.organization_id, template=template, to=address,
                     context=email_context, related_kind=related_kind, related_id=str(related_id),
                     related_label=related_label)
        except Exception as exc:
            _log_step(s, run, node, "FAILED", f"send_email ({template} to {address}) failed: {exc}")
            raise _RunFailure(f"send_email failed: {exc}") from None
        outbox_ids.append(str(getattr(row, "id", "")))
        if getattr(row, "status", None) == "NOT_CONFIGURED":
            not_configured = True
    detail = (f"Recorded in outbox (email not configured): {template} to {', '.join(addresses)}."
              if not_configured else
              f"Enqueued {template} email to {', '.join(addresses)}.")
    _log_step(s, run, node, "OK", detail,
              output={"template": template, "recipient": recipient, "to": addresses,
                      "outbox_ids": outbox_ids, "related_kind": related_kind,
                      "related_id": str(related_id)})


def _act_create_exception(s: Session, run: WorkflowRun, node: dict, data: dict, *, settings, facts: dict) -> None:
    title = _render(data.get("title") or "Workflow exception", facts)
    detail = _render(data.get("detail") or "", facts)
    employer = _resolve_employer(s, run, facts)
    task = WorkflowTask(organization_id=run.organization_id, kind="EXCEPTION", title=title[:300],
                        detail=detail, employer_id=employer.id if employer else None,
                        entity_kind=run.entity_kind, entity_id=run.entity_id, status="OPEN",
                        created_by_run_id=run.id)
    s.add(task)
    s.flush()
    _log_step(s, run, node, "OK", f"Created exception task: {title}", output={"task_id": str(task.id)})


def _act_post_chat_notice(s: Session, run: WorkflowRun, node: dict, data: dict, *, settings, facts: dict) -> None:
    # A notice must never fail a run: every failure path is an OK step.
    try:
        message = _render(data.get("message") or "", facts)
        if not message.strip():
            _log_step(s, run, node, "OK", "no chat thread; notice skipped (empty message)")
            return
        thread = s.scalar(
            select(ChatThread).where(ChatThread.organization_id == run.organization_id)
            .order_by(ChatThread.updated_at.desc()))
        if thread is None:
            _log_step(s, run, node, "OK", "no chat thread; notice skipped")
            return
        s.add(ChatMessage(thread_id=thread.id, role="assistant", content=message))
        thread.title = thread.title  # touch updated_at, mirroring chat _save
        s.flush()
        _log_step(s, run, node, "OK", f"Posted chat notice to thread {thread.id}.",
                  output={"thread_id": str(thread.id)})
    except Exception as exc:  # noqa: BLE001 - a notice never fails a run
        logger.warning("post_chat_notice failed for run %s: %s", run.id, exc)
        try:
            _log_step(s, run, node, "OK", f"chat notice skipped: {exc}")
        except Exception:
            pass


def _act_funding_reminder(s: Session, run: WorkflowRun, node: dict, data: dict, *, settings, facts: dict) -> None:
    employer = _resolve_employer(s, run, facts)
    note = _render(data.get("note") or "Fund this employer's wallet before the next payroll run.", facts)
    title = f"Funding reminder — {employer.name}" if employer is not None else "Funding reminder"
    task = WorkflowTask(organization_id=run.organization_id, kind="REMINDER", title=title[:300],
                        detail=note, employer_id=employer.id if employer else None,
                        entity_kind=run.entity_kind, entity_id=run.entity_id, status="OPEN",
                        created_by_run_id=run.id)
    s.add(task)
    s.flush()
    _log_step(s, run, node, "OK", f"Created funding reminder for {title}.",
              output={"task_id": str(task.id), "employer_id": str(employer.id) if employer else None})



# ---------------------------------------------------------------------------
# Payslip fan-out (settled run -> one email per contractor)
# ---------------------------------------------------------------------------

def _act_send_payslips(s: Session, run: WorkflowRun, node: dict, data: dict, *, settings, facts: dict) -> None:
    """template=payslip_receipt + recipient=contractor: ONE enqueue per
    contractor on the settled run, each with that contractor's own
    Worker Receipt link (/receipt/<pay_line_id>?token=... — the HMAC
    token scheme from api.routes.bedata.receipt_token)."""
    try:
        fn = _enqueue_email_fn()
    except Exception:
        _log_step(s, run, node, "FAILED", "email service unavailable")
        raise _RunFailure("email service unavailable") from None
    if settings is None:
        from ..config import load_settings
        settings = load_settings()
    run_id = _context_run_id(run)
    payroll = s.get(PayrollRun, _parse_uuid(run_id)) if run_id else None
    if payroll is None or payroll.organization_id != run.organization_id:
        _log_step(s, run, node, "FAILED", "payslip_receipt: no payroll run in this workflow's context.")
        raise _RunFailure("payslip_receipt: no payroll run in context.")
    employer = s.get(Employer, payroll.employer_id)
    lines = s.scalars(select(PayLine).where(PayLine.run_id == payroll.id)
                      .order_by(PayLine.position)).all()
    base_context = _email_context(s, run, facts)
    sent: list[dict] = []
    outbox_ids: list[str] = []
    not_configured = False
    for line in lines:
        contractor = s.get(Contractor, line.contractor_id) if line.contractor_id else None
        address = contractor.email if contractor is not None else None
        if not address:
            continue  # no address on file for this contractor — noted in the step output below
        try:
            from ..api.routes.bedata import receipt_token
            receipt_link = f"/receipt/{line.id}?token={receipt_token(settings, line.id)}"
        except Exception:
            receipt_link = f"/receipt/{line.id}"  # run's receipt page path for this line
        context = dict(base_context)
        context.update({"contractor_name": line.beneficiary_name, "amount": str(line.amount),
                        "currency": line.currency, "pay_line_id": str(line.id),
                        "run_id": str(payroll.id), "receipt_link": receipt_link,
                        "receipt_path": receipt_link})
        try:
            row = fn(s, settings, organization_id=run.organization_id, template="payslip_receipt",
                     to=address, context=context, related_kind="payroll_run",
                     related_id=str(payroll.id), related_label=employer.name if employer else None)
        except Exception as exc:
            _log_step(s, run, node, "FAILED", f"payslip_receipt to {address} failed: {exc}")
            raise _RunFailure(f"payslip_receipt failed: {exc}") from None
        outbox_ids.append(str(getattr(row, "id", "")))
        if getattr(row, "status", None) == "NOT_CONFIGURED":
            not_configured = True
        sent.append({"pay_line_id": str(line.id), "contractor_name": line.beneficiary_name, "to": address})
    if not sent:
        _log_step(s, run, node, "FAILED",
                  "payslip_receipt: no contractor on the settled run has an email address.")
        raise _RunFailure("payslip_receipt: no contractor email addresses on the settled run.")
    skipped = len(lines) - len(sent)
    detail = (f"Recorded in outbox (email not configured): payslip_receipt to {len(sent)} contractor(s)."
              if not_configured else f"Enqueued payslip_receipt to {len(sent)} contractor(s).")
    if skipped:
        detail += f" {skipped} pay line(s) had no contractor email and were skipped."
    _log_step(s, run, node, "OK", detail,
              output={"template": "payslip_receipt", "recipient": "contractor", "sent": sent,
                      "outbox_ids": outbox_ids, "related_kind": "payroll_run",
                      "related_id": str(payroll.id)})


# ---------------------------------------------------------------------------
# Prepare / revalidate payroll (NON-money: prepare or check only)
# ---------------------------------------------------------------------------

PREPARED_RUN_STATUSES = ("RECEIVED", "PRICED", "RESERVED")


def _run_figures(payroll: PayrollRun) -> dict:
    assessment = payroll.assessment if isinstance(payroll.assessment, dict) else {}
    total = payroll.required_usd if payroll.required_usd is not None else assessment.get("required_usd")
    shortfall = _residual_shortfall(payroll)
    if shortfall is None:
        shortfall = (str(payroll.shortfall_usd) if payroll.shortfall_usd is not None
                     else assessment.get("full_shortfall_usd") or assessment.get("shortfall_usd"))
    return {"total_required": str(total) if total is not None else None,
            "balance": assessment.get("available_usd"),
            "shortfall": str(shortfall) if shortfall is not None else None,
            "currency": payroll.payroll_currency or assessment.get("payroll_currency") or "USD",
            "status": payroll.status}


def _store_payroll_context(run: WorkflowRun, payroll: PayrollRun) -> dict:
    figures = _run_figures(payroll)
    context = dict(run.context or {})
    context["run_id"] = str(payroll.id)
    context["payroll_run_id"] = str(payroll.id)
    facts = dict(context.get("facts") or {})
    facts.update({"run_id": str(payroll.id), "total_required": figures["total_required"],
                  "balance": figures["balance"], "shortfall": figures["shortfall"],
                  "currency": figures["currency"]})
    residual = _residual_shortfall(payroll)
    if residual is not None:
        facts["residual_shortfall"] = residual
    context["facts"] = {k: v for k, v in facts.items() if v is not None}
    run.context = context
    return figures


def _prepare_via_runtime(s: Session, employer: Employer, settings) -> PayrollRun:
    """Prepare through the SAME path the runs API uses: runtime.start_run
    (intake + pricing; the graph parks at its human approval interrupt —
    the workflow never approves or executes it)."""
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    from ..config import load_settings
    from ..db.session import Database
    from ..services.runtime import PlatformRuntime

    settings_obj = settings or load_settings()
    db_obj = Database(engine=s.get_bind(),
                      sessionmaker=_sessionmaker(bind=s.get_bind(), expire_on_commit=False))
    runtime = PlatformRuntime(settings_obj, db_obj)
    org = s.get(Organization, employer.organization_id)
    owner = s.scalar(
        select(User).join(Membership, Membership.user_id == User.id)
        .where(Membership.organization_id == employer.organization_id, Membership.role == "owner")
        .order_by(Membership.created_at))
    if owner is None:
        raise WorkflowError("NO_ORG_OWNER", "No organization owner to prepare the run as.")
    lines = [runtime.contractor_line(c, c.default_amount) for c in employer.contractors
             if c.is_active and c.default_amount is not None]
    if not lines:
        raise WorkflowError("NO_PAYROLL_DATA",
                            f"{employer.name} has no active contractors with default amounts to prepare.")
    return runtime.start_run(s, org=org, user=owner, employer=employer, pay_lines=lines,
                             raw_input=None, source="workflow", period_label=None)


def _act_prepare_payroll(s: Session, run: WorkflowRun, node: dict, data: dict, *, settings, facts: dict) -> None:
    employer = _resolve_employer(s, run, facts)
    if employer is None:
        _log_step(s, run, node, "FAILED", "prepare_payroll_run: no employer in this workflow's context.")
        raise _RunFailure("prepare_payroll_run: no employer in context.")
    existing = s.scalar(
        select(PayrollRun).where(PayrollRun.employer_id == employer.id,
                                 PayrollRun.status.in_(PREPARED_RUN_STATUSES))
        .order_by(PayrollRun.created_at.desc()))
    if existing is not None:
        figures = _store_payroll_context(run, existing)
        s.flush()
        _log_step(s, run, node, "OK",
                  f"Reusing existing prepared run {existing.id} for {employer.name} "
                  f"(status {existing.status}) — 'existing prepared run'.",
                  output={"run_id": str(existing.id), "existing": True, **figures})
        return
    hook = globals().get("prepare_payroll_run")
    try:
        payroll = hook(s, employer=employer, settings=settings) if callable(hook) \
            else _prepare_via_runtime(s, employer, settings)
    except Exception as exc:
        _log_step(s, run, node, "FAILED", f"prepare_payroll_run failed for {employer.name}: {exc}")
        raise _RunFailure(f"prepare_payroll_run failed: {exc}") from None
    figures = _store_payroll_context(run, payroll)
    s.flush()
    _log_step(s, run, node, "OK",
              f"Prepared payroll run {payroll.id} for {employer.name} (status {payroll.status}; "
              "left for human approval — the workflow does not approve or execute it).",
              output={"run_id": str(payroll.id), "existing": False, **figures})


def _line_unpaid(line: PayLine) -> bool:
    state = getattr(line, "settlement_state", None)
    if state is not None:
        return state != "PAID"
    return line.status in ("HELD", "PENDING")


def _rescuable_run_for(s: Session, run: WorkflowRun, facts: dict, employer: Employer) -> PayrollRun | None:
    """A HELD run, or a PARTIALLY_SETTLED run that still has unpaid lines."""
    candidates: list[PayrollRun] = []
    rid = facts.get("run_id") or _context_run_id(run)
    if rid:
        one = s.get(PayrollRun, _parse_uuid(rid))
        if one is not None and one.employer_id == employer.id and one.organization_id == run.organization_id:
            candidates.append(one)
    candidates.extend(s.scalars(
        select(PayrollRun).where(PayrollRun.employer_id == employer.id)
        .order_by(PayrollRun.created_at.desc())).all())
    seen: set = set()
    for payroll in candidates:
        if payroll.id in seen:
            continue
        seen.add(payroll.id)
        if payroll.status in HELD_RUN_STATUSES:
            return payroll
        if payroll.status == "PARTIALLY_SETTLED" and any(_line_unpaid(l) for l in payroll.pay_lines):
            return payroll
    return None


def _cash_balance_usd(s: Session, employer: Employer) -> Decimal:
    """Employer's own cash, in USD (demo/static rates; USD identity always)."""
    from ..airwallex.simulator import USD_VALUE

    rates = {"USD": Decimal("1")}
    try:
        rates.update({k: Decimal(v) for k, v in USD_VALUE.items()})
    except Exception:
        pass
    rows = s.execute(
        select(LedgerEntry.currency, LedgerEntry.amount)
        .where(LedgerEntry.account.like(f"{employer.ledger_key}:cash:%"))).all()
    total = Decimal("0")
    for currency, amount in rows:
        rate = rates.get(currency)
        if rate is not None:
            total += Decimal(amount) * rate
    return total


def _read_only_revalidation(s: Session, employer: Employer, payroll: PayrollRun) -> dict:
    """Read-only affordability re-check (the platform has no revalidate
    function): current wallet vs the run's OWN stored required figure +
    the policy reserve floor — the same formula the graph's execute-node
    recheck uses. Changes no state."""
    from ..policy import PAYROLL_POLICY

    assessment = payroll.assessment if isinstance(payroll.assessment, dict) else {}
    required = Decimal(payroll.required_usd) if payroll.required_usd is not None \
        else Decimal(str(assessment.get("required_usd") or "0"))
    reserve = Decimal(str(assessment.get("reserve_floor_usd") or PAYROLL_POLICY.reserve_floor_usd))
    available = _cash_balance_usd(s, employer)
    shortfall = max(Decimal("0"), required + reserve - available)
    return {"now_funded": shortfall == 0, "shortfall": str(shortfall),
            "balance": str(available), "total_required": str(required)}


def _act_revalidate_held(s: Session, run: WorkflowRun, node: dict, data: dict, *, settings, facts: dict) -> None:
    employer = _resolve_employer(s, run, facts)
    if employer is None:
        _log_step(s, run, node, "FAILED", "revalidate_held_run: no employer in this workflow's context.")
        raise _RunFailure("revalidate_held_run: no employer in context.")
    payroll = _rescuable_run_for(s, run, facts, employer)
    if payroll is None:
        context = dict(run.context or {})
        facts_out = dict(context.get("facts") or {})
        facts_out["now_funded"] = "false"
        context["facts"] = facts_out
        run.context = context
        s.flush()
        _log_step(s, run, node, "OK",
                  f"No HELD or PARTIALLY_SETTLED run with unpaid lines for {employer.name}; "
                  "nothing to revalidate.",
                  output={"run_id": None, "now_funded": False,
                          "shortfall": facts.get("shortfall") or "0.00"})
        return
    hook = globals().get("revalidate_held_run")
    try:
        result = hook(s, run=payroll, employer=employer) if callable(hook) \
            else _read_only_revalidation(s, employer, payroll)
    except Exception as exc:
        _log_step(s, run, node, "FAILED", f"revalidate_held_run failed: {exc}")
        raise _RunFailure(f"revalidate_held_run failed: {exc}") from None
    figures = _store_payroll_context(run, payroll)
    context = dict(run.context or {})
    facts_out = dict(context.get("facts") or {})
    facts_out.update({"now_funded": "true" if result.get("now_funded") else "false",
                      "shortfall": str(result.get("shortfall", figures["shortfall"] or "0")),
                      "balance": str(result.get("balance", figures["balance"] or "")),
                      "total_required": str(result.get("total_required", figures["total_required"] or ""))})
    residual = _residual_shortfall(payroll)
    if residual is not None:
        facts_out["residual_shortfall"] = residual
    context["facts"] = {k: v for k, v in facts_out.items() if v not in (None, "")}
    run.context = context
    s.flush()
    _log_step(s, run, node, "OK",
              f"Revalidated {payroll.status} run {payroll.id} for {employer.name}: "
              f"now_funded={bool(result.get('now_funded'))}, shortfall {result.get('shortfall')}.",
              output={"run_id": str(payroll.id), "now_funded": bool(result.get("now_funded")),
                      "shortfall": str(result.get("shortfall")),
                      "balance": str(result.get("balance")),
                      "total_required": str(result.get("total_required")),
                      "residual_shortfall": residual})


_ACTION_HANDLERS = {
    "action.draft_onboarding_kit": _act_draft_kit,
    "action.create_approval_request": _act_create_approval,
    "action.send_email": _act_send_email,
    "action.create_exception": _act_create_exception,
    "action.post_chat_notice": _act_post_chat_notice,
    "action.create_funding_reminder": _act_funding_reminder,
    "action.prepare_payroll_run": _act_prepare_payroll,
    "action.revalidate_held_run": _act_revalidate_held,
}



# ---------------------------------------------------------------------------
# Schedules (external cron hits POST /workflows/schedules/tick; no daemon)
# ---------------------------------------------------------------------------

def _period_stamp(cadence: str, now) -> str:
    if cadence == "weekly":
        iso = now.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}"
    return f"{now.year:04d}-{now.month:02d}"


def run_due_schedules(s: Session, *, now, settings=None) -> list[WorkflowRun]:
    """Fire enabled schedule workflows due at ``now``.

    Monthly: ``now.day == day``. Weekly: ``now.weekday() == day``.
    Once per period per entity: the period stamp is part of the run's
    dedupe key, so a second tick in the same period creates nothing.
    fan_out="employers" creates one run per employer (entity=employer);
    fan_out="org" creates one run (entity=organization).
    """
    created: list[WorkflowRun] = []
    workflows = s.scalars(
        select(Workflow).where(Workflow.enabled.is_(True),
                               Workflow.trigger_type == "trigger.schedule")
        .order_by(Workflow.created_at)).all()
    for workflow in workflows:
        trigger_node = next((n for n in (workflow.graph or {}).get("nodes") or []
                             if isinstance(n, dict) and n.get("type") == "trigger.schedule"), None)
        if trigger_node is None:
            continue
        data = trigger_node.get("data") or {}
        cadence, day = data.get("cadence"), data.get("day")
        if cadence == "monthly" and now.day != day:
            continue
        if cadence == "weekly" and now.weekday() != day:
            continue
        if cadence not in ("monthly", "weekly"):
            continue
        period = _period_stamp(cadence, now)
        if data.get("fan_out", "org") == "employers":
            employers = s.scalars(
                select(Employer).where(Employer.organization_id == workflow.organization_id)
                .order_by(Employer.created_at)).all()
            for employer in employers:
                facts = _entity_facts(s, workflow.organization_id, "employer", str(employer.id))
                created.extend(dispatch_event(
                    s, organization_id=workflow.organization_id, event_type="trigger.schedule",
                    entity_kind="employer", entity_id=str(employer.id), facts=facts,
                    settings=settings, dedupe_suffix=period, only_workflow_id=workflow.id))
        else:
            created.extend(dispatch_event(
                s, organization_id=workflow.organization_id, event_type="trigger.schedule",
                entity_kind="organization", entity_id=str(workflow.organization_id),
                facts={"organization_id": str(workflow.organization_id)},
                settings=settings, dedupe_suffix=period, only_workflow_id=workflow.id))
    return created


# ---------------------------------------------------------------------------
# Payroll Morning Brief
# ---------------------------------------------------------------------------

def _brief_split(payroll: PayrollRun, lines: list[PayLine]) -> tuple[list[PayLine], list[PayLine]]:
    """will_pay / will_hold for one gated payroll run.

    Derivation, in order (partial-pay update, Sriram 2026-11-12):
    1. The run's OWN per-line settlement states (settlement_state
       PAID|HELD, PP lane) when present.
    2. The pricing split stored in the run's assessment (partial plan:
       held_lines by beneficiary name) when present.
    3. PayLine.status on a PARTIALLY_SETTLED run (PAID lines pay).
    4. Fallback whole-run rule: shortfall (residual first) > 0 -> ALL
       lines held; otherwise all pay.
    """
    states = [getattr(line, "settlement_state", None) for line in lines]
    if any(state in ("PAID", "HELD") for state in states):
        return ([line for line in lines if getattr(line, "settlement_state", None) == "PAID"],
                [line for line in lines if getattr(line, "settlement_state", None) != "PAID"])
    assessment = payroll.assessment if isinstance(payroll.assessment, dict) else {}
    held_lines = assessment.get("held_lines") or []
    if assessment.get("partial") and held_lines:
        held_names = {h.get("name") for h in held_lines if isinstance(h, dict)}
        return ([line for line in lines if line.beneficiary_name not in held_names],
                [line for line in lines if line.beneficiary_name in held_names])
    if payroll.status == "PARTIALLY_SETTLED" and any(line.status in ("PAID", "HELD") for line in lines):
        return ([line for line in lines if line.status == "PAID"],
                [line for line in lines if line.status != "PAID"])
    shortfall_raw = _residual_shortfall(payroll)
    shortfall = Decimal(shortfall_raw) if shortfall_raw is not None else \
        (Decimal(payroll.shortfall_usd) if payroll.shortfall_usd is not None else Decimal("0"))
    if shortfall > 0:
        return [], list(lines)
    return list(lines), []


def build_payroll_brief(s: Session, *, organization_id: uuid.UUID) -> list[dict]:
    """One entry per WAITING workflow run gated on a payroll run."""
    from ..money import money_str

    entries: list[dict] = []
    waiting = s.scalars(
        select(WorkflowRun).where(WorkflowRun.organization_id == organization_id,
                                  WorkflowRun.status == "WAITING")
        .order_by(WorkflowRun.created_at)).all()
    for wf_run in waiting:
        context = wf_run.context or {}
        run_id = None
        if context.get("gate_entity_kind") in ("run", "payroll_run") and context.get("gate_entity_id"):
            run_id = str(context["gate_entity_id"])
        else:
            run_id = _context_run_id(wf_run)
        if not run_id:
            continue
        payroll = s.get(PayrollRun, _parse_uuid(run_id))
        if payroll is None or payroll.organization_id != organization_id:
            continue
        employer = s.get(Employer, payroll.employer_id)
        workflow = s.get(Workflow, wf_run.workflow_id)
        lines = s.scalars(select(PayLine).where(PayLine.run_id == payroll.id)
                          .order_by(PayLine.position)).all()
        will_pay, will_hold = _brief_split(payroll, lines)
        figures = _run_figures(payroll)
        assessment = payroll.assessment if isinstance(payroll.assessment, dict) else {}
        balance = figures["balance"]
        if balance is None:
            balance = str(_cash_balance_usd(s, employer)) if employer is not None else None
        settlement_mode = getattr(employer, "settlement_mode", None) or assessment.get("settlement_mode")

        def _line_out(line: PayLine) -> dict:
            return {"contractor_name": line.beneficiary_name, "amount": money_str(line.amount),
                    "currency": line.currency}

        entries.append({
            "workflow_run_id": str(wf_run.id),
            "workflow_name": workflow.name if workflow is not None else None,
            "run_id": str(payroll.id),
            "run_status": payroll.status,
            "employer_id": str(payroll.employer_id),
            "employer_name": employer.name if employer is not None else None,
            "settlement_mode": settlement_mode,
            "pay_date": payroll.pay_date.isoformat() if payroll.pay_date else None,
            "total_required": figures["total_required"],
            "balance": balance,
            "shortfall": figures["shortfall"],
            "currency": figures["currency"],
            "will_pay": [_line_out(line) for line in will_pay],
            "will_hold": [_line_out(line) for line in will_hold],
        })
    return entries


# ---------------------------------------------------------------------------
# Human decisions
# ---------------------------------------------------------------------------

def resolve_approval_for_entity(s: Session, *, organization_id: uuid.UUID, entity_kind: str, entity_id,
                                decision: str, decided_by, settings=None) -> list[WorkflowRun]:
    """Record a human decision and resume every run gated on that entity.

    ``decision`` is "approved"/"approve" or "rejected"/"reject"/"deny".
    ``decided_by`` may be a User, a user id, or an email string.
    Returns the runs that were resumed (now COMPLETED or re-parked).
    """
    normalized = (decision or "").strip().lower()
    if normalized in ("approved", "approve"):
        status = "APPROVED"
    elif normalized in ("rejected", "reject", "denied", "deny"):
        status = "REJECTED"
    else:
        raise WorkflowError("INVALID_DECISION", "Decision must be 'approved' or 'rejected'.")
    entity_id = str(entity_id)
    decided_by_id = None
    if isinstance(decided_by, User):
        decided_by_id = decided_by.id
    elif decided_by is not None:
        parsed = _parse_uuid(decided_by)
        decided_by_id = parsed if parsed is not None else None

    pending = s.scalars(
        select(WorkflowApproval).where(
            WorkflowApproval.organization_id == organization_id,
            WorkflowApproval.entity_kind == entity_kind,
            WorkflowApproval.entity_id == entity_id,
            WorkflowApproval.status == "PENDING")).all()
    run_ids: list[uuid.UUID] = []
    for approval in pending:
        approval.status = status
        approval.decided_by_id = decided_by_id
        approval.decided_at = utcnow()
        if approval.workflow_run_id is not None:
            run_ids.append(approval.workflow_run_id)
    s.flush()
    if not pending:
        # The decision arrived without a workflow approval row (e.g. the
        # entity was decided before any gate parked): still resume any
        # WAITING run gated on this entity by recording the decision on it.
        waiting = s.scalars(
            select(WorkflowRun).where(WorkflowRun.organization_id == organization_id,
                                      WorkflowRun.status == "WAITING")).all()
        for run in waiting:
            context = run.context or {}
            if context.get("gate_entity_kind") == entity_kind and context.get("gate_entity_id") == entity_id:
                approval = WorkflowApproval(organization_id=organization_id, workflow_run_id=run.id,
                                            entity_kind=entity_kind, entity_id=entity_id, status=status,
                                            decided_by_id=decided_by_id, decided_at=utcnow())
                s.add(approval)
                run_ids.append(run.id)
        s.flush()

    resumed: list[WorkflowRun] = []
    for run_id in dict.fromkeys(run_ids):
        run = s.get(WorkflowRun, run_id)
        if run is None or run.status != "WAITING":
            continue
        waiting_node = (run.context or {}).get("waiting_node_id")
        run.status = "RUNNING"
        s.flush()
        execute_run(s, run, settings=settings, _start_node_id=waiting_node)
        resumed.append(run)
    s.flush()
    return resumed


# ---------------------------------------------------------------------------
# Best-effort dispatch helper for payroll wiring (never raises)
# ---------------------------------------------------------------------------

def dispatch_payroll_status(s: Session, run) -> None:
    """Dispatch payroll_held / payroll_settled for a PayrollRun row.

    Guarded: a workflow error must NEVER break payroll — log, don't raise.
    Call after the run's new status has been persisted.
    """
    try:
        if run.status in HELD_RUN_STATUSES:
            event = "trigger.payroll_held"
        elif run.status == "SETTLED":
            event = "trigger.payroll_settled"
        else:
            return
        employer = s.get(Employer, run.employer_id)
        amount = run.shortfall_usd if run.shortfall_usd is not None else run.required_usd
        facts = {"employer_id": str(run.employer_id),
                 "employer_name": employer.name if employer else None,
                 "amount": str(amount) if amount is not None else None,
                 "shortfall_usd": str(run.shortfall_usd) if run.shortfall_usd is not None else None,
                 "currency": run.payroll_currency or "USD", "status": run.status,
                 "destination": None, "kind": "payroll"}
        facts = {k: v for k, v in facts.items() if v is not None}
        dispatch_event(s, organization_id=run.organization_id, event_type=event,
                       entity_kind="payroll_run", entity_id=str(run.id), facts=facts)
    except Exception:  # noqa: BLE001 - workflows never break payroll
        logger.exception("workflow dispatch for payroll run %s failed (ignored)", getattr(run, "id", "?"))


__all__ = [
    "ACTION_TYPES", "GATE_TYPES", "KNOWN_TYPES", "TRIGGER_TYPES", "WorkflowError",
    "build_payroll_brief", "dispatch_event", "dispatch_payroll_status", "execute_run", "normalize_event",
    "run_due_schedules",
    "resolve_approval_for_entity", "trigger_type_of", "validate_graph",
]
