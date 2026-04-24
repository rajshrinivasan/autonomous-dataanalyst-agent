# Autonomous Data Analyst

Ask a business question in plain English. A team of AI agents queries a database, generates a chart, and writes a structured report — all streamed live to a web UI.

**Stack:** OpenAI `gpt-4o-mini` · LangGraph · LangChain MCP Adapters · FastMCP · FastAPI · Server-Sent Events · Docker sandbox · PostgreSQL · Clerk · JWT/RS256

---

## What it does

1. You type a question: *"What are the top 5 product categories by total revenue?"*
2. A **LangGraph StateGraph** routes the question through five specialist nodes in sequence.
3. A **governance check** validates and lints the generated SQL before execution, looping back to the SQL writer if needed.
4. Each node's progress streams live to the browser as it happens.
5. A chart (generated in a Docker sandbox) and a written report appear when analysis is complete.

---

## Architecture

```
Browser
  │  POST /analyze  (question + auth JWT)
  │  ← SSE stream of events
  ▼
┌─────────────────────────────────────────────────────────────────┐
│  FastAPI  (app.py)                                               │
│  GET  /                  → static/index.html                    │
│  POST /analyze           → StreamingResponse (SSE)              │
│  GET  /charts/{file}     → serve PNG files                      │
│  POST|GET|DELETE /datasources → connector registry (CRUD)       │
│  GET  /health            → healthcheck                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │  JWT (RS256) via auth/dependencies.py
                           │  DEV_AUTH_BYPASS=true skips in dev
                           │  async generator
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  orchestration.py  —  run_analysis(question, workspace, ds)     │
│  Drives workflow/graph.py → astream_events()                    │
│  → workflow/sse_adapter.py → SSE event dicts                    │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  LangGraph StateGraph  (workflow/)                               │
│                                                                  │
│  data_explorer ──► sql_writer ──► governance_check              │
│                                        │                         │
│                          lint errors ──┘ (retry ≤3×)            │
│                          lint passes ──► analyst                 │
│                                              │                   │
│                                           writer ──► END         │
│                                                                  │
│  State: AnalysisState TypedDict (workflow/state.py)             │
│  Checkpointer: AsyncPostgresSaver (or MemorySaver fallback)     │
└──────┬─────────────────────┬──────────────────┬─────────────────┘
       │ MCP stdio           │ MCP stdio        │ function tool
       ▼                     ▼                  ▼
┌──────────────────┐  ┌──────────────────┐  ┌───────────────────────┐
│ warehouse_       │  │ governance_      │  │ tools.py              │
│ server.py        │  │ server.py        │  │ run_python_code()     │
│                  │  │                  │  │ — exec matplotlib     │
│ list_tables()    │  │ lint_sql()       │  │   in Docker sandbox   │
│ get_schema()     │  │ estimate_cost()  │  │ — network disabled    │
│ run_query()      │  │ redact_pii()     │  │ — read-only FS        │
│                  │  │                  │  │ — 512 MB / 30 s       │
│ SQLite / PG /    │  └──────────────────┘  └───────────────────────┘
│ BigQuery /       │
│ Snowflake        │         ┌──────────────────────────────────┐
└──────────────────┘         │  PostgreSQL  (optional)          │
                             │  • Workspace / User / DataSource │
                             │  • LangGraph checkpoints         │
                             │  • Row-level security (RLS)      │
                             │  • Alembic migrations            │
                             └──────────────────────────────────┘
```

---

## LangGraph Pipeline

The orchestration is a `StateGraph` compiled with a Postgres (or in-memory) checkpointer. All state flows through a single `AnalysisState` TypedDict; each node returns only the keys it updates.

```
data_explorer_node     Calls list_tables() + get_schema() via warehouse MCP.
                       Returns: schema_summary

sql_writer_node        Writes a SELECT query from the schema.
                       Access to warehouse + governance MCP servers.
                       Returns: sql

governance_check_node  Calls lint_sql() directly (no LLM).
                       DML/DDL → error. SELECT * → warning. Missing LIMIT → warning.
                       Errors → loop back to sql_writer (max 3 retries).
                       Returns: lint_result, lint_revision_count

analyst_node           Calls run_query() + run_python_code() (Docker sandbox).
                       Extracts insights from query results.
                       Returns: query_results, chart_path, insights

writer_node            Produces the final markdown report (≤ 450 words).
                       Returns: report
```

The SSE adapter (`workflow/sse_adapter.py`) maps `astream_events()` callbacks onto the event format the frontend already understands — `static/index.html` is unchanged.

---

## Agents

| Node | Role | MCP Servers | Tools |
|---|---|---|---|
| **data_explorer** | Maps schema and relationships | warehouse | `list_tables()`, `get_schema()` |
| **sql_writer** | Writes correct SELECT queries | warehouse + governance | `lint_sql()`, `estimate_cost()` |
| **governance_check** | Enforces SQL safety rules (no LLM) | governance | `lint_sql()` (direct call) |
| **analyst** | Runs queries, generates charts | warehouse | `run_query()`, `run_python_code()` |
| **writer** | Produces structured narrative | — | — |

Instructions for each agent live in [`instructions/`](instructions/) as Markdown files, loaded at runtime. Edit them to tune behaviour without touching code.

---

## MCP Servers

### warehouse_server.py

Launched as a stdio subprocess per analysis session. Resolves the datasource from Postgres, reads the connection string from env, and creates a SQLAlchemy engine for the target dialect.

| Tool | Description |
|---|---|
| `list_tables()` | JSON array of table names |
| `get_schema(table_name)` | Columns, types, PKs, FKs, row count |
| `run_query(sql)` | SELECT-only; capped at datasource `row_limit` |

Supported engines: **SQLite**, **PostgreSQL**, **BigQuery**, **Snowflake**. Falls back to `mcp_server.py` (legacy SQLite) when no `datasource_id` is provided.

### governance_server.py

Stateless FastMCP server started alongside the warehouse server.

| Tool | Description |
|---|---|
| `lint_sql(sql, datasource_id)` | Blocks DML/DDL; warns on `SELECT *`, missing `LIMIT`, unfiltered fact tables |
| `estimate_cost(sql, datasource_id)` | SQLite: `EXPLAIN QUERY PLAN`; Postgres: `EXPLAIN (FORMAT JSON)`; BigQuery: dry-run bytes |
| `redact_pii(rows, catalog)` | Masks PII columns in result rows |

---

## Docker Sandbox

`run_python_code()` executes all model-generated matplotlib code inside a locked-down Docker container (`ada-sandbox:latest`):

- **Network disabled** — no outbound connections
- **Read-only filesystem** — only `/tmp` is writable (mounted from `output/charts/`)
- **512 MB RAM · 1 CPU · 30 s timeout**
- Blocked globals: `os`, `sys`, `subprocess`, `socket`, `importlib`, `open`

Build the image once:

```bash
docker build -t ada-sandbox:latest sandbox/
```

---

## Authentication & Multi-Tenancy

Auth is handled by `auth/dependencies.py` using RS256 JWT verification (Clerk-compatible JWKS endpoint).

**Token claims used:**
- `sub` — user ID
- `workspace_id` — multi-tenant workspace scope
- `role` — `admin | analyst | viewer` (defaults to `analyst`)

Set `DEV_AUTH_BYPASS=true` in `.env` to skip token validation in local development.

Workspace, User, DataSource, and WorkspaceMember ORM models live in `db/models.py` with Alembic-managed Postgres migrations that include row-level security (RLS) policies.

---

## Production Auth Setup (Clerk + real user)

This is a one-time setup to test or run the app with `DEV_AUTH_BYPASS=false` and a real Clerk identity.

### 1 — Disable the dev bypass

In `.env`:
```
DEV_AUTH_BYPASS=false
CLERK_PUBLISHABLE_KEY=pk_test_...       # from Clerk Dashboard → API Keys
CLERK_JWT_TEMPLATE=ada                  # name you give the template in step 3
JWT_JWKS_URL=https://<your-clerk-domain>/.well-known/jwks.json
JWT_AUDIENCE=<your-clerk-publishable-key>
```

### 2 — Start Postgres and run migrations

```bash
docker-compose -f docker-compose.dev.yml up -d
alembic upgrade head
```

### 3 — Configure a Clerk JWT template

In [Clerk Dashboard](https://dashboard.clerk.com) → **Configure → JWT Templates** → **New template**:

- Name it `ada` (must match `CLERK_JWT_TEMPLATE`)
- Add these custom claims:

```json
{
  "workspace_id": "{{user.public_metadata.workspace_id}}",
  "role": "{{user.public_metadata.role}}"
}
```

### 4 — Create the workspace and user in Postgres

Connect to Postgres (`psql postgresql://analyst:analyst_dev@localhost:5432/analyst_db` or pgAdmin at `http://localhost:5050`) and run:

```sql
-- Pick a UUID for the workspace and use it in step 5 too
INSERT INTO workspaces (id, name, created_at)
VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'My Workspace', NOW());

-- clerk_user_id comes from Clerk Dashboard → Users → select user
INSERT INTO users (id, clerk_user_id, email, created_at)
VALUES (gen_random_uuid(), 'user_2xxxxxxxxxxxx', 'you@example.com', NOW());

-- Add the user as admin so they can manage datasources
INSERT INTO workspace_members (id, workspace_id, user_id, role)
SELECT gen_random_uuid(),
       'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
       id,
       'admin'
FROM users WHERE clerk_user_id = 'user_2xxxxxxxxxxxx';
```

### 5 — Set public metadata on the Clerk user

In Clerk Dashboard → **Users** → select the user → **Public metadata**:

```json
{
  "workspace_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  "role": "admin"
}
```

This is what the JWT template reads to embed `workspace_id` and `role` into the token.

### 6 — Login via the UI

Start the app (`python run.py`), open `http://localhost:8000`.  
The login screen shows the **Sign in with Clerk** button (dev section is hidden since `DEV_AUTH_BYPASS=false`).  
Click it → Clerk modal opens → sign in → token is stored automatically → app loads.

### 7 — Register a datasource

As `admin`, the **Manage** button appears in the datasource bar:

1. Click **Manage** → modal opens
2. Fill in:
   - **Name**: `Sample SQLite`
   - **Type**: `sqlite`
   - **Connection secret ref**: `SECRET_SAMPLE_DB` (matches the env var name in `.env`)
3. Click **Add datasource**

The datasource is stored in Postgres scoped to your workspace (enforced by RLS).

### Notes

- **Token expiry**: Clerk JWTs expire after ~1 hour. The frontend automatically refreshes via `Clerk.session.getToken()` on any 401 response. If refresh fails, the user is signed out cleanly.
- **Analyst vs Viewer roles**: only `admin` and `analyst` can submit questions. `viewer` gets a read-only notice and the submit button is disabled.
- **CLERK_JWT_TEMPLATE is optional**: if omitted, the default Clerk session token is used, but it won't contain `workspace_id` or `role` — the backend will return 403. Always set the template for production use.

---

## Testing

The project uses a 5-layer testing pyramid, each layer adding progressively more confidence at higher cost:

### Layer 1 — Unit Tests (`tests/unit/`)
Pure function tests with no external dependencies: JWT auth logic, governance lint rules, MCP tool parsing, SSE event mapping, workflow state transitions.

### Layer 2 — Integration Tests (`tests/integration/`)
End-to-end LangGraph graph execution with mocked OpenAI responses; FastAPI endpoint tests against a real SQLite database.

### Layer 3 — Deterministic Invariants (`tests/invariants/`)
Three behavioral guarantees verified **without any LLM call** — these can never regress silently:
- **I1 — DML/DDL Rejection**: `run_query()` always rejects non-SELECT statements
- **I2 — Row Limit Enforcement**: queries are always capped at `datasource.row_limit`
- **I3 — Schema Consistency**: `data_explorer` output matches actual DB schema

### Layer 4 — Sandbox Tests (`tests/sandbox/`)
Docker container execution tests: code runs in the locked-down sandbox, chart files are produced, resource limits are enforced.

### Layer 5 — Browser E2E (`tests/e2e/`)
Playwright tests that drive the full UI flow: auth, question submission, activity feed, chart render, report panel.

### LLM-as-Judge Evals (`evals/`)
Golden test cases with two-layer scoring (separate from the test pyramid — run on a schedule, not in CI):
- **Deterministic** (free, always runs): SQL structural checks, execution, data properties, chart presence, report section checks
- **LLM judge** (optional, paid): semantic SQL quality, insight groundedness, report clarity

Eval results saved to `evals/results/<timestamp>.json`. An optional bootstrap optimizer (`evals/optimizer.py`) tunes few-shot examples from golden cases.

### CI/CD (`.github/workflows/`)

| Workflow | Trigger | What runs |
|---|---|---|
| `tests.yml` | push / PR to main | Unit + invariant tests on Python 3.11 & 3.12 |
| `evals.yml` | nightly cron | Full golden case suite with LLM judge; optional `--optimize` |

---

## SSE Event Stream

Every event from `POST /analyze` is a JSON line: `data: {...}\n\n`

| Type | Payload | UI effect |
|---|---|---|
| `agent_switch` | `agent` | Active agent highlighted in feed |
| `tool_call` | `tool` | Tool card appears with spinner |
| `tool_result` | `output` (first 400 chars) | Spinner removed, preview shown |
| `text_delta` | `delta` | Text streams character-by-character |
| `chart` | `path`, `url` | Thumbnail in feed; full image in report panel |
| `done` | `output` | Markdown report rendered in right panel |
| `error` | `message` | Error banner shown |

---

## File Structure

```
Autonomous Data Analyst/
├── app.py                     FastAPI server — SSE, chart serving, datasource CRUD
├── orchestration.py           run_analysis() generator — drives LangGraph graph
├── mcp_server.py              Legacy FastMCP SQLite server (fallback, no auth)
├── tools.py                   run_python_code() — Docker sandbox execution
├── run.py                     Launcher (auto-seeds DB, starts uvicorn)
│
├── workflow/                  LangGraph pipeline
│   ├── state.py               AnalysisState TypedDict
│   ├── nodes.py               Five async node functions
│   ├── graph.py               StateGraph + conditional governance edge
│   ├── checkpointer.py        AsyncPostgresSaver / MemorySaver factory
│   └── sse_adapter.py         astream_events() → SSE event dicts
│
├── mcp_servers/
│   ├── warehouse_server.py    Multi-engine MCP (SQLite / PG / BigQuery / Snowflake)
│   └── governance_server.py   lint_sql, estimate_cost, redact_pii
│
├── instructions/              Agent system prompts (editable at runtime)
│   ├── data_explorer.md
│   ├── sql_writer.md
│   ├── analyst.md
│   ├── writer.md
│   └── manager.md             (legacy reference)
│
├── auth/
│   └── dependencies.py        JWT (RS256) verification, RequireAnalyst dependency
│
├── db/
│   ├── models.py              SQLAlchemy ORM — Workspace, User, DataSource
│   ├── session.py             Async session factory with RLS context
│   └── migrations/            Alembic migrations (RLS policies included)
│
├── sandbox/
│   ├── Dockerfile             Locked-down Python 3.11 image
│   └── runner.py              JSON envelope stdin → exec() → CHART_SAVED
│
├── evals/                     LLM-as-judge evaluation suite
│   ├── test_evals.py          Pytest runner
│   ├── runner.py              EvalResult runner (executes golden cases)
│   ├── scorer.py              Two-layer scoring (deterministic + LLM)
│   ├── optimizer.py           Bootstrap optimizer for few-shot tuning
│   ├── judge_prompts.py       LLM judge prompt templates
│   └── cases/golden_cases.json  ~6 end-to-end test cases with expectations
│
├── tests/                     5-layer testing pyramid
│   ├── unit/                  No external deps — fast, always run
│   │   ├── test_auth.py       JWT verification
│   │   ├── test_governance.py SQL lint rules
│   │   ├── test_mcp_server.py MCP tool parsing
│   │   ├── test_sse_adapter.py Event stream mapping
│   │   └── test_workflow_state.py State transitions
│   ├── integration/           LangGraph + FastAPI + SQLite
│   │   ├── test_app.py        FastAPI endpoints
│   │   └── test_workflow_integration.py End-to-end graph run
│   ├── invariants/            3 behavioral guarantees (no LLM required)
│   │   └── test_invariants.py DML rejection, row limit, schema consistency
│   ├── sandbox/               Docker container execution
│   │   └── test_sandbox.py    Chart generation, resource limits
│   └── e2e/                   Browser-based E2E (Playwright)
│       └── test_playwright.py Auth flow, submit, activity feed, chart
│
├── data/
│   ├── seed_db.py             Creates sample.db (e-commerce dataset)
│   ├── seed_analyst_db.py     Creates analyst.db (SaaS metrics dataset)
│   └── sample.db              8 categories, 46 products, 300 customers,
│                              1 200 orders, 3 551 line items
│
├── static/
│   └── index.html             Single-page web UI (vanilla JS + marked.js)
│
├── output/
│   └── charts/                Generated PNG charts served at /charts/<file>
│
├── .github/workflows/
│   ├── tests.yml              CI: unit + invariant tests
│   └── evals.yml              Nightly: LLM-as-judge evals
│
├── docker-compose.dev.yml     Postgres 16 + pgAdmin for local dev
├── Makefile                   sandbox-build, dev-up, migrate targets
├── .env.example               Environment variable template
└── requirements.txt           Python dependencies
```

---

## Setup

**Prerequisites:** Python 3.11+, Docker Desktop

```bash
# 1. Start Postgres (optional — falls back to SQLite without it)
docker compose -f docker-compose.dev.yml up -d

# 2. Build the chart sandbox image
docker build -t ada-sandbox:latest sandbox/

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Required: OPENAI_API_KEY=sk-...
# Optional: POSTGRES_DSN, JWT_JWKS_URL, JWT_AUDIENCE

# 5. Run database migrations (only with Postgres)
alembic upgrade head

# 6. Seed the SQLite sample database (one-time)
python data/seed_db.py
```

---

## Running

### Web UI

```bash
python run.py
# open http://127.0.0.1:8000
```

`run.py` automatically seeds `data/sample.db` if it does not exist.

```bash
python run.py --host 0.0.0.0 --port 8080
```

### CLI (no web server)

```bash
python orchestration.py "What is the gross margin by category?"
python orchestration.py "Which month had the highest sales in 2024?"
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | Required. OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model for all nodes. Use `gpt-4o` for higher quality |
| `POSTGRES_DSN` | — | Async DSN (`postgresql+asyncpg://...`). Enables checkpointing + auth |
| `POSTGRES_DSN_SYNC` | — | Sync DSN (`postgresql://...`). Used by warehouse/governance servers and Alembic |
| `JWT_JWKS_URL` | — | JWKS endpoint for RS256 JWT verification (e.g. Clerk) |
| `JWT_AUDIENCE` | — | Expected `aud` claim in JWTs |
| `DEV_AUTH_BYPASS` | `false` | Skip JWT validation in development |
| `DEV_WORKSPACE_ID` | — | Fixed workspace UUID used when auth bypass is on |
| `DOCKER_SANDBOX_IMAGE` | `ada-sandbox:latest` | Docker image for chart execution |
| `SECRET_SAMPLE_DB` | `sqlite:///./data/sample.db` | Connection string for the sample datasource |
| `HOST` | `127.0.0.1` | Server bind address |
| `PORT` | `8000` | Server port |
| `CHARTS_DIR` | `output/charts` | Directory for generated chart PNGs |

---

## Sample Questions

- *What are the top 5 product categories by total revenue?*
- *What is the average order value by country?*
- *Which products have the highest gross margin percentage?*
- *How does revenue trend month-over-month across 2024?*
- *What percentage of orders were cancelled, by category?*
- *Who are the top 10 customers by lifetime spend?*

---

## Dependencies

| Package | Purpose |
|---|---|
| `langgraph` | StateGraph orchestration with Postgres checkpointing |
| `langchain-openai` | `ChatOpenAI` model wrapper |
| `langchain-mcp-adapters` | `MultiServerMCPClient` — bridges MCP tools into LangChain |
| `langchain-core` | Messages, tools, runnable config |
| `openai` | Underlying OpenAI API client |
| `mcp` | FastMCP server framework |
| `fastapi` + `uvicorn` | Web server and ASGI runner |
| `matplotlib` + `pandas` | Chart generation inside Docker sandbox |
| `sqlalchemy[asyncio]` + `asyncpg` | Async ORM and Postgres driver |
| `alembic` | Database migrations with RLS policies |
| `python-jose[cryptography]` | JWT RS256 verification |
| `docker` | docker-py SDK for sandbox container management |
| `psycopg2-binary` | Sync Postgres driver for warehouse / governance servers |
| `pytest` + `pytest-asyncio` | Test runner |
| `testcontainers[postgres]` | Ephemeral Postgres containers for integration tests |
| `playwright` | Browser-based E2E tests |
| `python-dotenv` | `.env` loading |
