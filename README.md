# Autonomous Data Analyst

Ask a business question in plain English. A team of AI agents queries a database, generates a chart, and writes a structured report — all streamed live to a web UI.

**Stack:** OpenAI `gpt-4o-mini` · LangGraph · LangChain MCP Adapters · FastMCP · FastAPI · Server-Sent Events · Docker · PostgreSQL

---

## What it does

1. You type a question: *"What are the top 5 product categories by total revenue?"*
2. A **LangGraph pipeline** routes the question through four specialist nodes in sequence.
3. Each node's progress streams live to the browser as it happens.
4. A written report and a chart appear in the right panel when analysis is complete.

---

## Architecture

```
Browser
  │  POST /analyze  (question)
  │  ← SSE stream of events
  ▼
┌─────────────────────────────────────────────────────────┐
│  FastAPI  (app.py)                                       │
│  GET /              → static/index.html                 │
│  POST /analyze      → StreamingResponse (SSE)           │
│  GET /charts/       → serve PNG files                   │
│  POST|GET|DELETE /datasources → connector registry      │
└────────────────────────┬────────────────────────────────┘
                         │  async generator
                         ▼
┌─────────────────────────────────────────────────────────┐
│  orchestration.py  —  run_analysis()                    │
│  Drives workflow/graph.py via astream_events()          │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│  LangGraph StateGraph  (workflow/)                       │
│                                                          │
│  data_explorer ──► sql_writer ──► governance_check      │
│                                        │                 │
│                          errors ───────┘ (retry ≤3x)    │
│                          clean  ──────► analyst          │
│                                             │            │
│                                          writer ──► END  │
└──────┬───────────────────┬────────────────┬─────────────┘
       │ MCP (stdio)       │ MCP (stdio)    │ function tool
       ▼                   ▼                ▼
┌────────────────┐ ┌───────────────┐ ┌─────────────────────┐
│ warehouse_     │ │ governance_   │ │ tools.py             │
│ server.py      │ │ server.py     │ │ run_python_code()    │
│                │ │               │ │ — exec matplotlib    │
│ list_tables()  │ │ lint_sql()    │ │   code in Docker     │
│ get_schema()   │ │ estimate_     │ │ — sandbox: network   │
│ run_query()    │ │   cost()      │ │   disabled, read-    │
│                │ │ redact_pii()  │ │   only FS, 512 MB    │
│ multi-dialect: │ │               │ └─────────────────────┘
│ SQLite/PG/BQ/  │ └───────────────┘
│ Snowflake      │
└────────────────┘
```

---

## LangGraph Pipeline

The orchestration is a `StateGraph` compiled with a Postgres (or in-memory) checkpointer. All state flows through a single `AnalysisState` TypedDict; each node returns only the keys it updates.

```
data_explorer_node   Calls list_tables() + get_schema() via MCP.
                     Returns: schema_summary

sql_writer_node      Writes a SELECT query using the schema.
                     Has access to both warehouse and governance MCP servers.
                     Returns: sql

governance_check_node  Calls lint_sql() directly (no LLM).
                       Errors → loop back to sql_writer (max 3 retries).
                       Returns: lint_result, lint_revision_count

analyst_node         Calls run_query() + run_python_code() (Docker sandbox).
                     Returns: insights, chart_path

writer_node          Produces the final markdown report.
                     Returns: report
```

The SSE adapter (`workflow/sse_adapter.py`) maps `astream_events()` callbacks to the same event format the frontend already understands — `static/index.html` is unchanged.

---

## Agents

| Node | Role | MCP Servers | Tools |
|---|---|---|---|
| **data_explorer** | Maps schema and relationships | warehouse | `list_tables()`, `get_schema()` |
| **sql_writer** | Writes correct SELECT queries | warehouse + governance | `lint_sql()`, `estimate_cost()` |
| **governance_check** | Enforces SQL safety rules | governance | `lint_sql()` (direct call) |
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

Supported engines: **SQLite**, **PostgreSQL**, **BigQuery**, **Snowflake**. Falls back to the original `mcp_server.py` (hardcoded SQLite) when no `datasource_id` is provided.

### governance_server.py

Stateless FastMCP server started alongside the warehouse server.

| Tool | Description |
|---|---|
| `lint_sql(sql, datasource_id)` | Blocks DML/DDL; warns on `SELECT *`, missing `LIMIT`, unfiltered fact tables |
| `estimate_cost(sql, datasource_id)` | SQLite: `EXPLAIN QUERY PLAN`; Postgres: `EXPLAIN (FORMAT JSON)`; BigQuery: dry-run bytes |
| `redact_pii(rows, catalog)` | Masks PII columns in result rows (placeholder for M2 catalog integration) |

---

## Docker Sandbox

`run_python_code()` executes all model-generated matplotlib code inside a locked-down Docker container (`ada-sandbox:latest`):

- **Network disabled** — no outbound connections
- **Read-only filesystem** — only `/tmp` is writable (mounted from `output/charts/`)
- **512 MB RAM · 1 CPU · 30 s timeout**
- Blocked globals: `os.system`, `subprocess`, `socket`, `importlib`, `open`

Build the image once:

```bash
docker build -t ada-sandbox:latest sandbox/
```

---

## SSE Event Stream

Every event from `POST /analyze` is a JSON line: `data: {...}\n\n`

| Type | Payload | UI effect |
|---|---|---|
| `agent_switch` | `agent` | Active agent highlighted in feed |
| `tool_call` | `tool` | Tool card appears with spinner |
| `tool_result` | `output` (first 400 chars) | Spinner removed, output preview shown |
| `text_delta` | `delta` | Text streams character-by-character |
| `chart` | `path`, `url` | Thumbnail in feed; full image in report panel |
| `done` | `output` | Markdown report rendered in right panel |
| `error` | `message` | Error banner shown |

---

## File Structure

```
Autonomous Data Analyst/
├── app.py                  FastAPI server — SSE, chart serving, datasource CRUD
├── orchestration.py        run_analysis() generator — drives LangGraph graph
├── mcp_server.py           Legacy FastMCP SQLite server (fallback, no auth)
├── tools.py                run_python_code() — Docker sandbox execution
├── run.py                  Launcher (auto-seeds DB, starts uvicorn)
│
├── workflow/               LangGraph pipeline
│   ├── state.py            AnalysisState TypedDict
│   ├── nodes.py            Five async node functions
│   ├── graph.py            StateGraph + conditional governance edge
│   ├── checkpointer.py     AsyncPostgresSaver / MemorySaver factory
│   └── sse_adapter.py      astream_events() → SSE event dicts
│
├── mcp_servers/
│   ├── warehouse_server.py Multi-engine MCP (SQLite / PG / BQ / Snowflake)
│   └── governance_server.py lint_sql, estimate_cost, redact_pii
│
├── instructions/
│   ├── manager.md          (legacy — kept for reference)
│   ├── data_explorer.md    Schema discovery instructions
│   ├── sql_writer.md       SQL writing rules (SQLite-specific)
│   ├── analyst.md          Query execution + matplotlib chart rules
│   └── writer.md           Report structure and style guide
│
├── auth/
│   └── dependencies.py     JWT (RS256) verification, RequireAnalyst dependency
│
├── db/
│   ├── models.py           SQLAlchemy ORM — Workspace, User, DataSource
│   ├── session.py          Async session factory with RLS context
│   └── migrations/         Alembic migrations (RLS policies included)
│
├── sandbox/
│   ├── Dockerfile          Locked-down Python image for chart execution
│   └── runner.py           JSON envelope stdin → exec() → CHART_SAVED
│
├── data/
│   ├── seed_db.py          Creates sample.db
│   └── sample.db           8 categories, 46 products, 300 customers,
│                           1200 orders, 3551 line items
│
├── static/
│   └── index.html          Single-page web UI (vanilla JS + marked.js)
│
├── output/
│   └── charts/             Generated PNG charts served at /charts/<file>
│
├── docker-compose.dev.yml  Postgres 16 + pgAdmin for local dev
├── Makefile                sandbox-build, dev-up, migrate targets
├── .env.example            Environment variable template
└── requirements.txt        Python dependencies
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
# Optional: POSTGRES_DSN, JWT_JWKS_URL, JWT_AUDIENCE, DOCKER_SANDBOX_IMAGE

# 5. Run database migrations (only needed with Postgres)
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
| `POSTGRES_DSN` | — | Async DSN (`postgresql+asyncpg://...`). Enables LangGraph checkpointing and auth |
| `POSTGRES_DSN_SYNC` | — | Sync DSN (`postgresql://...`). Used by warehouse_server and Alembic |
| `JWT_JWKS_URL` | — | JWKS endpoint for RS256 JWT verification (e.g. Clerk) |
| `JWT_AUDIENCE` | — | Expected `aud` claim in JWTs |
| `DOCKER_SANDBOX_IMAGE` | `ada-sandbox:latest` | Docker image used for chart execution |
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
| `langgraph` | StateGraph orchestration with checkpointing |
| `langchain-openai` | `ChatOpenAI` model wrapper |
| `langchain-mcp-adapters` | `MultiServerMCPClient` — bridges MCP tools into LangChain |
| `langchain-core` | Messages, tools, runnable config |
| `openai` | Underlying OpenAI API client |
| `mcp` | FastMCP server framework |
| `fastapi` + `uvicorn` | Web server and ASGI runner |
| `matplotlib` + `pandas` | Chart generation inside Docker sandbox |
| `sqlalchemy[asyncio]` + `asyncpg` | Async ORM and Postgres driver |
| `alembic` | Database migrations |
| `python-jose[cryptography]` | JWT verification |
| `docker` | docker-py SDK for sandbox container management |
| `psycopg2-binary` | Sync Postgres driver for warehouse / governance servers |
| `python-dotenv` | `.env` loading |
