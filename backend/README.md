# PaySilo backend

FastAPI + LangGraph. Python 3.12, managed with [uv](https://docs.astral.sh/uv/).

```bash
cd backend
uv sync                                   # creates .venv from uv.lock
uv run pytest                             # 53 tests: policy, ledger, TAP, graph safety, API
uv run uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs. With no `.env` the backend runs in demo mode:
local SQLite (`backend/paysilo.db`), the Airwallex simulator, and a seeded demo
organization (`GET /auth/demo-credentials`). Delete `paysilo.db` to start fresh.

## Layout

```
app/
  policy.py            fee-first affordability (payroll + payout fees + run fee + reserve floor)
  ledger.py            in-memory double-entry ledger (one employer per transaction)
  agents/              LangGraph graph: intake -> price -> assess -> approval/escalate -> execute -> fee -> reconcile -> attest
  airwallex/           REST client, simulator (same interface), call recorder
  partners/            Visa TAP signing, Metal attestation, AgentCore entrypoint
  db/                  SQLAlchemy models, engine (Supabase/Postgres or SQLite), Postgres guards, schema export
  services/runtime.py  runs the graph and persists every step
  services/seed.py     demo organization
  api/                 FastAPI app, auth, tenant-scoped routes
db/schema.sql          generated Postgres DDL: `uv run python -m app.db.export_schema > db/schema.sql`
```

## Supabase

Put the connection string in `DATABASE_URL` (root `.env`). On startup the
backend creates the tables, the ledger triggers and enables RLS on every table
(so Supabase's anon REST role can read nothing). Alternatively paste
`db/schema.sql` into the Supabase SQL editor and set `AUTO_CREATE_SCHEMA=false`.

Run a single worker: paused runs (waiting for approval) live in process memory.
