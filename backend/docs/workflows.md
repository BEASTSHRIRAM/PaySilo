# Workflows: graph JSON, validation, schedules, Silo drafting

A workflow is a saved node graph that a deterministic engine runs when an event happens or a calendar date arrives.
There is no model at run time, and there is no approve / pay / purchase node: a person decides on the real screen
after a `gate.wait_for_approval`.

## Graph JSON

```jsonc
{
  "layout": "lr",                      // optional marker: positions are laid out left to right
  "nodes": [
    { "id": "trigger", "type": "trigger.schedule", "position": {"x": 40, "y": 60}, "data": { /* config */ } }
  ],
  "edges": [
    { "id": "e1", "source": "trigger", "target": "prepare", "sourceHandle": null }
  ]
}
```

* `nodes[].id` unique string; `type` from `GET /workflows/node-registry`; `data` holds the config fields below.
* `edges[]`: `source` and `target` are node ids. `sourceHandle` is `"true"` or `"false"` on a **condition**'s two
  outputs (the canvas labels them Yes / No) and `null` everywhere else.
* The flow runs **left to right**: a node's input is its left edge, its output its right edge. Triggers have no input.
* Export envelope used by the JSON drawer: `{ "name": "...", "description": "...", "graph": { nodes, edges } }`.
  `POST /workflows/validate` accepts either the envelope or a bare graph.

### Rules (enforced identically on save, JSON import, `/validate` and agent drafts)

| Rule | Error message (summary) |
| --- | --- |
| Exactly one trigger | "Graph must have exactly one trigger node (found N)." |
| Nothing connects into a trigger; trigger -> trigger is refused | "A trigger can't connect to another trigger..." |
| No self-connection, no duplicate edge, no cycle | "...can't connect to itself", "already connected", "form a loop" |
| Condition outputs are `true` / `false` | "A condition's outputs are 'yes' (true) and 'no' (false)." |
| Known node types only; money-moving words are forbidden | "unknown node type", "'...' is forbidden" |
| Required config per type | e.g. `send_email` needs `template`; `create_exception` needs `title`; `post_chat_notice` needs `message` |
| A scheduled payroll workflow must name an employer | "must be scoped to one employer" (`employer_id`, or `fan_out: "employers"`) |
| `employer_id` must belong to the caller's organisation | "not an employer of this organisation" |
| Size limits | at most 60 nodes, 150 edges, 2000 characters per text field, 200 KB total |

Every error is `{ node_id, edge_id, field, code, message }`, so a UI (or the model) can point at the exact node
and field. Codes: `shape`, `size`, `duplicate_id`, `unknown_type`, `forbidden`, `required`, `invalid_value`,
`unresolvable`, `trigger_count`, `missing_node`, `self_loop`, `connection`, `duplicate_edge`, `cycle`,
`unknown_employer`.

## Node types

`GET /workflows/node-registry` returns every type with its label, config fields, `inputs` and `outputs`.

* Triggers: `trigger.employer_added`, `contractor_joined`, `payroll_held`, `payroll_settled`, `benefit_requested`,
  `trip_proposed`, `kit_approved`, `kit_completed`, `funds_received`, `schedule`.
* Logic: `condition.field_check` (`field`, `operator`, `value`; outputs `true` / `false`).
* Human: `gate.wait_for_approval` (`entity`: `trigger` | `kit` | `trip` | `run`).
* Actions: `draft_onboarding_kit`, `create_approval_request`, `send_email` (`template`, `recipient`),
  `create_exception` (`title`, `detail`), `post_chat_notice` (`message`), `create_funding_reminder` (`note`),
  `prepare_payroll_run` (prepares and RESERVES the run; never approves), `revalidate_held_run`.

## Schedule trigger

`trigger.schedule` data:

| Field | Meaning |
| --- | --- |
| `cadence` | `monthly`, `weekly` or `once` |
| `day` | monthly: 1-31 (a shorter month uses its last day); weekly: 0 = Monday ... 6 = Sunday |
| `last_day` | monthly: `true` = last day of every month |
| `date` | once: `YYYY-MM-DD` |
| `time` | 24h local time, default `09:00` |
| `timezone` | IANA zone, default `Asia/Kolkata` (no daylight saving) |
| `weekend_rule` | `as_is`, `previous_business_day`, `next_business_day` (weekends only; public holidays are NOT modelled) |
| `employer_id` | scope to ONE employer of the organisation |
| `fan_out` | `org` (default) or `employers` (one run per employer) |

Configs without `time` / `timezone` keep their original meaning (the whole UTC day, one run per calendar period).

* `POST /workflows/schedule-preview` `{config, count}` returns the next runs computed on the server; the UI never
  does calendar maths.
* `GET /workflows/schedule-occurrences?start=&end=` lists the occurrences of the organisation's enabled schedules (the
  calendar's read-only chips).
* `GET|POST /workflows/employer-schedules`: duplicate check and the wizard that builds *Schedule -> Prepare payroll
  run -> (wallet covers it) email approvers -> Wait for approval -> notice; (short) funding reminder -> alert the
  employer contact* for ONE employer. It is created enabled only when the caller sends `enabled: true` after the
  wizard's explicit confirm.

### Execution

The existing background loop (`funding_reminder_loop`, every 30 minutes, also `POST /workflows/schedules/tick` for
the caller's organisation) calls `run_due_schedules` per organisation:

* an occurrence is due when it is at or before now and under 24 hours old: after downtime **at most one** missed
  occurrence is caught up;
* the run's dedupe key contains the workflow, the entity and the occurrence instant (`...:2026-12-03T03:30Z`), so a
  repeated tick or a second worker never fires twice (the key is unique in the database);
* a schedule saved or enabled after an occurrence's time does not fire for it;
* a workflow whose `employer_id` is not in its own organisation never fires;
* the loop assumes one worker process (see `services/reminders.py`); the unique dedupe key is the second defence.

The firing precision is the loop interval (up to ~30 minutes after the configured time).

## Import / export

The editor's **JSON** drawer shows the graph, copies it, downloads `<name>.workflow.json`, or imports pasted / uploaded
JSON. Import calls `POST /workflows/validate` (tenant-scoped, no side effects); on success the canvas is replaced after
a confirm and **nothing is saved until Save**. A valid graph without positions comes back laid out left to right.

## Silo drafting (`draft_workflow` chat tool)

* The model receives the node registry and the organisation's employers (ids and names only) and returns
  `{name, description, graph}`. Code validates it with the same validator. Nothing is repaired silently: an invalid
  draft goes back to the model with the validator's errors, at most 2 retries, then the attempt fails with a clear
  message and nothing is saved.
* A valid draft is saved **disabled** with `drafted_by_agent: true`; the chat reply links to the canvas. Any `enabled`
  in the model output is ignored. Only owner/admin may draft.
* The agent has no tool to enable, run, approve or delete a workflow and no money capability.
* Without a model key a deterministic matcher handles "monthly payroll for <employer> on day N" (and a template by
  name); anything else says it needs the API key.

## Full example

Monthly payroll for one employer, day 3 at 09:00 Asia/Kolkata, moved to the previous business day on a weekend:

```json
{
  "name": "Monthly Payroll Day: Acme Manufacturing GmbH",
  "description": "Prepare Acme's payroll run on the 3rd and ask the approvers to decide.",
  "graph": {
    "layout": "lr",
    "nodes": [
      {"id": "trigger", "type": "trigger.schedule", "position": {"x": 40, "y": 60},
       "data": {"label": "Acme: monthly on day 3", "cadence": "monthly", "day": 3, "time": "09:00",
                "timezone": "Asia/Kolkata", "weekend_rule": "previous_business_day",
                "employer_id": "00000000-0000-0000-0000-000000000000"}},
      {"id": "prepare", "type": "action.prepare_payroll_run", "position": {"x": 340, "y": 60},
       "data": {"label": "Prepare payroll run"}},
      {"id": "check", "type": "condition.field_check", "position": {"x": 640, "y": 60},
       "data": {"label": "Wallet covers it?", "field": "shortfall", "operator": "eq", "value": "0"}},
      {"id": "email-approvers", "type": "action.send_email", "position": {"x": 940, "y": 60},
       "data": {"label": "Email approvers", "template": "approval_requested", "recipient": "decider"}},
      {"id": "gate", "type": "gate.wait_for_approval", "position": {"x": 1240, "y": 60},
       "data": {"label": "Wait for approval", "entity": "run"}},
      {"id": "notice-approved", "type": "action.post_chat_notice", "position": {"x": 1540, "y": 60},
       "data": {"label": "Approved notice", "message": "Payroll for {employer_name} was approved."}},
      {"id": "reminder", "type": "action.create_funding_reminder", "position": {"x": 940, "y": 220},
       "data": {"label": "Funding reminder", "note": "Top up {employer_name} before the run executes."}}
    ],
    "edges": [
      {"id": "e1", "source": "trigger", "target": "prepare"},
      {"id": "e2", "source": "prepare", "target": "check"},
      {"id": "e3", "source": "check", "target": "email-approvers", "sourceHandle": "true"},
      {"id": "e4", "source": "email-approvers", "target": "gate"},
      {"id": "e5", "source": "gate", "target": "notice-approved"},
      {"id": "e6", "source": "check", "target": "reminder", "sourceHandle": "false"}
    ]
  }
}
```

(Replace `employer_id` with a real employer id of your organisation; the server rejects ids it does not know.)
