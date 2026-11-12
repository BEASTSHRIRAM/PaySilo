"""Workflow graph validation: precise, structured errors shared by save, import, /validate and the Silo agent.

``validate_graph_detailed`` returns ``{node_id, edge_id, field, code, message}`` rows (empty = valid). The same
rules run on every path that can store a graph, so a hand-written JSON import, a canvas save and an agent draft are
all held to one standard.
"""

from __future__ import annotations

from .workflows import (
    CONDITION_TYPES, EMAIL_RECIPIENTS, GATE_ENTITIES, KNOWN_TYPES, OPERATORS, TRIGGER_TYPES,
    _is_forbidden_action, _parse_uuid, trigger_type_of,
)

MAX_NODES = 60
MAX_EDGES = 150
MAX_STRING = 2000
MAX_GRAPH_BYTES = 200_000
PAYROLL_ACTIONS = frozenset({"action.prepare_payroll_run", "action.revalidate_held_run"})
CONDITION_HANDLES = ("true", "false")


def _err(errors: list[dict], message: str, *, node_id: str | None = None, edge_id: str | None = None,
         field: str | None = None, code: str = "invalid") -> None:
    errors.append({"node_id": node_id, "edge_id": edge_id, "field": field, "code": code, "message": message})


def connection_problem(source_type: str, target_type: str, *, source_handle: str | None = None) -> str | None:
    """Friendly reason why ``source -> target`` is not allowed by node TYPE, or None. The canvas shows the same
    sentence in its toast."""
    if target_type in TRIGGER_TYPES:
        if source_type in TRIGGER_TYPES:
            return ("A trigger can't connect to another trigger. A workflow starts from exactly one trigger: "
                    "connect the trigger to an action or condition instead, or put the second trigger in its own "
                    "workflow.")
        return ("Nothing can connect into a trigger: it is where the workflow starts. Connect this step to a node "
                "to the right of it instead.")
    if source_type in CONDITION_TYPES and source_handle not in (None, *CONDITION_HANDLES):
        return "A condition's outputs are 'yes' (true) and 'no' (false)."
    return None


def _find_cycle(node_ids: set[str], edges: list[dict]) -> list[str] | None:
    adjacency: dict[str, list[str]] = {n: [] for n in node_ids}
    for edge in edges:
        adjacency[edge["source"]].append(edge["target"])
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in adjacency[node]:
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if nxt not in state:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for n in sorted(node_ids):
        if n not in state:
            found = visit(n)
            if found:
                return found
    return None


def validate_graph_detailed(graph, *, employer_ids: set[str] | None = None) -> list[dict]:
    """Precise validation errors; empty means valid.

    ``employer_ids`` (the caller's organization's employers, as strings) enables the employer-scope check.
    """
    errors: list[dict] = []
    if not isinstance(graph, dict):
        return [{"node_id": None, "edge_id": None, "field": None, "code": "shape",
                 "message": "Graph must be an object with 'nodes' and 'edges'."}]
    nodes = graph.get("nodes")
    edges = graph.get("edges") or []
    if not isinstance(nodes, list) or not nodes:
        return [{"node_id": None, "edge_id": None, "field": "nodes", "code": "shape",
                 "message": "Graph must contain at least one node."}]
    if not isinstance(edges, list):
        return [{"node_id": None, "edge_id": None, "field": "edges", "code": "shape",
                 "message": "Graph 'edges' must be a list."}]
    if len(nodes) > MAX_NODES:
        _err(errors, f"Graph has {len(nodes)} nodes; the limit is {MAX_NODES}.", field="nodes", code="size")
    if len(edges) > MAX_EDGES:
        _err(errors, f"Graph has {len(edges)} edges; the limit is {MAX_EDGES}.", field="edges", code="size")
    if errors:
        return errors

    node_ids: set[str] = set()
    trigger_count = 0
    node_types: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            _err(errors, "Every node must be an object with 'id' and 'type'.", code="shape")
            continue
        node_id, node_type = node.get("id"), node.get("type")
        if not node_id or not isinstance(node_id, str):
            _err(errors, "Every node must have a string 'id'.", field="id", code="shape")
            continue
        if node_id in node_ids:
            _err(errors, f"Duplicate node id '{node_id}'.", node_id=node_id, field="id", code="duplicate_id")
        node_ids.add(node_id)
        if not node_type or not isinstance(node_type, str):
            _err(errors, f"Node '{node_id}' must have a 'type'.", node_id=node_id, field="type", code="shape")
            continue
        node_types[node_id] = node_type
        if _is_forbidden_action(node_type):
            _err(errors, f"Node '{node_id}': '{node_type}' is forbidden — workflows never approve, "
                         "purchase or pay. Use gate.wait_for_approval to park for a human decision.",
                 node_id=node_id, field="type", code="forbidden")
            continue
        if node_type not in KNOWN_TYPES:
            _err(errors, f"Node '{node_id}': unknown node type '{node_type}'.", node_id=node_id, field="type",
                 code="unknown_type")
            continue
        if node_type in TRIGGER_TYPES:
            trigger_count += 1
        data = node.get("data") or {}
        if not isinstance(data, dict):
            _err(errors, f"Node '{node_id}': 'data' must be an object.", node_id=node_id, field="data", code="shape")
            continue
        for key, value in data.items():
            if isinstance(value, str) and len(value) > MAX_STRING:
                _err(errors, f"Node '{node_id}': data.{key} is longer than {MAX_STRING} characters.",
                     node_id=node_id, field=key, code="size")
        if node_type == "trigger.schedule":
            _validate_schedule(errors, node_id, data, employer_ids)
        if node_type == "condition.field_check":
            if not data.get("field"):
                _err(errors, f"Node '{node_id}': condition.field_check requires data.field.", node_id=node_id,
                     field="field", code="required")
            if data.get("operator", "eq") not in OPERATORS:
                _err(errors, f"Node '{node_id}': operator must be one of {', '.join(OPERATORS)}.",
                     node_id=node_id, field="operator", code="invalid_value")
        if node_type == "gate.wait_for_approval":
            entity = data.get("entity", "trigger")
            if entity not in GATE_ENTITIES:
                _err(errors, f"Node '{node_id}': wait gate entity must be one of "
                             f"{', '.join(GATE_ENTITIES)} (got '{entity}') — a gate with no "
                             "resolvable entity cannot park.", node_id=node_id, field="entity", code="invalid_value")
            elif entity == "kit" and "action.draft_onboarding_kit" not in node_types.values() \
                    and trigger_type_of(graph) not in ("trigger.kit_approved", "trigger.kit_completed"):
                # No kit is created by this graph and the trigger is not a
                # kit, so a kit gate can never resolve.
                _err(errors, f"Node '{node_id}': wait gate for entity 'kit' has no resolvable entity — "
                             "no node in this graph drafts a kit and the trigger is not a kit event.",
                     node_id=node_id, field="entity", code="unresolvable")
        if node_type == "action.send_email":
            from .spend_mail import TEMPLATE_NAMES

            if not data.get("template"):
                _err(errors, f"Node '{node_id}': action.send_email requires data.template.", node_id=node_id,
                     field="template", code="required")
            elif data["template"] not in TEMPLATE_NAMES:
                _err(errors, f"Node '{node_id}': unknown email template '{data['template']}' "
                             f"(use one of {', '.join(TEMPLATE_NAMES)}).", node_id=node_id, field="template",
                     code="invalid_value")
            if data.get("recipient", "decider") not in EMAIL_RECIPIENTS:
                _err(errors, f"Node '{node_id}': send_email recipient must be one of "
                             f"{', '.join(EMAIL_RECIPIENTS)}.", node_id=node_id, field="recipient",
                     code="invalid_value")
        if node_type == "action.create_exception" and not str(data.get("title") or "").strip():
            _err(errors, f"Node '{node_id}': action.create_exception requires data.title.", node_id=node_id,
                 field="title", code="required")
        if node_type == "action.post_chat_notice" and not str(data.get("message") or "").strip():
            _err(errors, f"Node '{node_id}': action.post_chat_notice requires data.message.", node_id=node_id,
                 field="message", code="required")
    if trigger_count != 1:
        _err(errors, f"Graph must have exactly one trigger node (found {trigger_count}).", field="nodes",
             code="trigger_count")

    # Payroll actions on a schedule must be scoped to an employer (one employer, or every employer).
    schedule = next((n for n in nodes if isinstance(n, dict) and n.get("type") == "trigger.schedule"), None)
    if schedule is not None and PAYROLL_ACTIONS & set(node_types.values()):
        sdata = schedule.get("data") if isinstance(schedule.get("data"), dict) else {}
        if not sdata.get("employer_id") and sdata.get("fan_out") != "employers":
            _err(errors, f"Node '{schedule.get('id')}': a scheduled payroll workflow must be scoped to one "
                         "employer. Pick the employer in the Schedule trigger (employer_id), or set fan_out to "
                         "'employers' to run once per employer.", node_id=schedule.get("id"),
                 field="employer_id", code="required")

    valid_edges: list[dict] = []
    seen_edges: set[tuple] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            _err(errors, "Every edge must be an object with 'source' and 'target'.", code="shape")
            continue
        edge_id = edge.get("id", "?")
        missing = False
        for end in ("source", "target"):
            if edge.get(end) not in node_ids:
                _err(errors, f"Edge '{edge_id}': {end} references missing node '{edge.get(end)}'.",
                     edge_id=str(edge_id), field=end, code="missing_node")
                missing = True
        if missing:
            continue
        source, target, handle = edge["source"], edge["target"], edge.get("sourceHandle")
        if source == target:
            _err(errors, f"Edge '{edge_id}': node '{source}' can't connect to itself.", edge_id=str(edge_id),
                 code="self_loop")
            continue
        s_type, t_type = node_types.get(source, ""), node_types.get(target, "")
        problem = connection_problem(s_type, t_type, source_handle=handle)
        if problem:
            _err(errors, f"Edge '{edge_id}' ({source} -> {target}): {problem}", edge_id=str(edge_id),
                 code="connection")
            continue
        key = (source, target, handle)
        if key in seen_edges:
            _err(errors, f"Edge '{edge_id}': {source} is already connected to {target}"
                         f"{' on that output' if handle else ''}.", edge_id=str(edge_id), code="duplicate_edge")
            continue
        seen_edges.add(key)
        valid_edges.append(edge)
    cycle = _find_cycle(node_ids, valid_edges)
    if cycle:
        _err(errors, "These steps form a loop (" + " -> ".join(cycle) + "). A workflow runs forward only: "
                     "remove one of the connections.", node_id=cycle[0], code="cycle")
    return errors


def _validate_schedule(errors: list[dict], node_id: str, data: dict, employer_ids: set[str] | None) -> None:
    from . import workflow_schedule as sched

    cadence = data.get("cadence")
    if cadence not in sched.CADENCES:
        _err(errors, f"Node '{node_id}': trigger.schedule requires data.cadence "
                     "('monthly', 'weekly' or 'once').", node_id=node_id, field="cadence", code="required")
        day = data.get("day")
        if not isinstance(day, int) or isinstance(day, bool):
            _err(errors, f"Node '{node_id}': trigger.schedule requires integer data.day "
                         "(day of month for monthly, 0-6 weekday for weekly).", node_id=node_id, field="day",
                 code="required")
        return
    day = data.get("day")
    probe = dict(data)
    if cadence in ("monthly", "weekly") and not (cadence == "monthly" and data.get("last_day") is True):
        if not isinstance(day, int) or isinstance(day, bool):
            _err(errors, f"Node '{node_id}': trigger.schedule requires integer data.day "
                         "(day of month for monthly, 0-6 weekday for weekly).", node_id=node_id, field="day",
                 code="required")
            probe["day"] = 1 if cadence == "monthly" else 0
        elif cadence == "monthly" and not 1 <= day <= 31:
            _err(errors, f"Node '{node_id}': monthly schedule day must be 1-31.", node_id=node_id, field="day",
                 code="invalid_value")
            probe["day"] = 1
        elif cadence == "weekly" and not 0 <= day <= 6:
            _err(errors, f"Node '{node_id}': weekly schedule day must be 0-6 (Mon-Sun).", node_id=node_id,
                 field="day", code="invalid_value")
            probe["day"] = 0
    if data.get("fan_out", "org") not in ("org", "employers"):
        _err(errors, f"Node '{node_id}': trigger.schedule fan_out must be 'org' or 'employers'.",
             node_id=node_id, field="fan_out", code="invalid_value")
    # time / timezone / weekend rule / once-date, via the same parser the engine uses.
    try:
        sched.parse_config(probe)
    except sched.ScheduleConfigError as exc:
        _err(errors, f"Node '{node_id}': {exc}", node_id=node_id, field=exc.field, code="invalid_value")
    employer_id = data.get("employer_id")
    if employer_id:
        if _parse_uuid(employer_id) is None:
            _err(errors, f"Node '{node_id}': employer_id must be an employer id.", node_id=node_id,
                 field="employer_id", code="invalid_value")
        elif employer_ids is not None and str(_parse_uuid(employer_id)) not in employer_ids:
            _err(errors, f"Node '{node_id}': employer_id is not an employer of this organisation.",
                 node_id=node_id, field="employer_id", code="unknown_employer")
