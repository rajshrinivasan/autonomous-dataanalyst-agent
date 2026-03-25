# Autonomous Data Analyst

Ask a business question in plain English. A team of AI agents queries a SQLite database, generates a chart, and writes a structured report — all streamed live to a web UI.

**Stack:** OpenAI `gpt-4o-mini` · openai-agents SDK · FastMCP (Model Context Protocol) · FastAPI · Server-Sent Events

---

## What it does

1. You type a question: *"What are the top 5 product categories by total revenue?"*
2. A **Manager agent** breaks the problem down and delegates to four specialists in sequence.
3. Each specialist's progress streams live to the browser as it happens.
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
│  GET /          → static/index.html                     │
│  POST /analyze  → StreamingResponse (text/event-stream) │
│  GET /charts/   → serve PNG files                       │
└────────────────────────┬────────────────────────────────┘
                         │  async generator
                         ▼
┌─────────────────────────────────────────────────────────┐
│  orchestration.py  —  Magentic delegation loop          │
│                                                          │
│  Manager Agent                                           │
│    ├── call_data_explorer()  ──► Data Explorer Agent    │
│    ├── call_sql_writer()     ──► SQL Writer Agent       │
│    ├── call_analyst()        ──► Analyst Agent          │
│    └── call_writer()         ──► Writer Agent           │
│                                                          │
│  Each specialist is wrapped with agent.as_tool() so     │
│  the manager calls them, receives their full output,    │
│  and continues orchestrating.                           │
└──────────┬──────────────────────┬───────────────────────┘
           │  MCP (stdio)         │  function tool
           ▼                      ▼
┌──────────────────┐   ┌─────────────────────────────────┐
│  mcp_server.py   │   │  tools.py                        │
│  (FastMCP/stdio) │   │  run_python_code()               │
│                  │   │  — executes matplotlib code      │
│  list_tables()   │   │    in a subprocess               │
│  get_schema()    │   │  — saves PNG to output/charts/   │
│  run_query()     │   │  — returns CHART_SAVED:<path>    │
│                  │   └─────────────────────────────────┘
│  SQLite          │
│  data/sample.db  │
└──────────────────┘
```

---

## Magentic Orchestration Pattern

The openai-agents SDK supports two delegation styles. `handoffs` transfer control *permanently* to the next agent — the originating agent does not receive results back. For a manager that needs to collect results from each specialist before continuing, **`agent.as_tool()`** is the right primitive: each specialist is wrapped as a callable tool that runs a full sub-agent and returns its output.

```
Simple handoff  (permanent transfer)
  Manager ──handoff──► Specialist          (manager loses control)

agent.as_tool() (round-trip delegation)
  Manager ──tool call──► Specialist
          ◄──result─────                   (manager stays in control)
  Manager ──tool call──► Next Specialist
          ◄──result─────
  ...
  Manager synthesises final answer
```

Each of the four specialist agents is defined normally with its own instructions and tools, then registered with the manager via:

```python
manager = Agent(
    name="manager",
    tools=[
        data_explorer.as_tool(tool_name="call_data_explorer", ...),
        sql_writer.as_tool(tool_name="call_sql_writer",    ...),
        analyst.as_tool(tool_name="call_analyst",          ...),
        writer.as_tool(tool_name="call_writer",            ...),
    ],
)
```

---

## Agents

| Agent | Role | Tools |
|---|---|---|
| **Manager** | Plans, delegates, synthesises | `call_data_explorer`, `call_sql_writer`, `call_analyst`, `call_writer` |
| **Data Explorer** | Maps schema and relationships | `list_tables()`, `get_schema()` via MCP |
| **SQL Writer** | Writes correct SQLite SELECT queries | `get_schema()` via MCP (verifies before writing) |
| **Analyst** | Runs queries, interprets data, generates charts | `run_query()` via MCP + `run_python_code()` |
| **Writer** | Produces structured narrative report | — |

Instructions for each agent live in [`instructions/`](instructions/) as Markdown files, loaded at runtime. Edit them to tune agent behaviour without touching code.

---

## MCP Server

[`mcp_server.py`](mcp_server.py) runs as a **stdio subprocess** — no extra port or HTTP server needed. The openai-agents SDK launches and manages it automatically when a run starts.

| Tool | Description |
|---|---|
| `list_tables()` | Returns a JSON array of all table names |
| `get_schema(table_name)` | Returns columns, types, foreign keys, row count, and CREATE SQL |
| `run_query(sql)` | Executes a SELECT (read-only URI), capped at 50 rows, returns JSON |

The connection is opened in **read-only mode** (`file:...?mode=ro`) so no write operations are possible regardless of what SQL the model generates.

---

## SSE Event Stream

Every event sent from `POST /analyze` is a JSON line: `data: {...}\n\n`

| Type | Payload | UI effect |
|---|---|---|
| `agent_switch` | `agent` | Marks the start of manager activity |
| `tool_call` | `tool` | Specialist card appears with spinner |
| `tool_result` | `output` (first 400 chars) | Spinner removed, content preview shown |
| `text_delta` | `delta` | Manager synthesis streams character-by-character |
| `chart` | `path`, `url` | Thumbnail in activity feed; full image in report panel |
| `done` | `output` | Markdown report rendered in right panel |
| `error` | `message` | Error banner shown |

---

## Traced Example

**Question:** *"What are the top 5 product categories by total revenue?"*

### Step 1 — Data Explorer (≈ 3 s)

The manager calls `call_data_explorer`. The specialist calls:

```
list_tables()
  → ["categories", "customers", "order_items", "orders", "products", "sqlite_sequence"]

get_schema("categories")    → id, name (8 rows)
get_schema("products")      → id, name, category_id, price, cost, stock_qty (46 rows)
get_schema("order_items")   → id, order_id, product_id, quantity, unit_price (3551 rows)
get_schema("orders")        → id, customer_id, status, created_at, shipped_at (1200 rows)
```

Returns a schema summary noting that `order_items.product_id → products.id → categories.id` is the join path for revenue.

### Step 2 — SQL Writer (≈ 8 s)

The manager passes the schema summary to `call_sql_writer`. The specialist also re-fetches schemas to verify column names before writing:

```sql
SELECT
    c.name          AS category_name,
    SUM(oi.quantity * oi.unit_price) AS total_revenue
FROM categories c
JOIN products    p  ON c.id = p.category_id
JOIN order_items oi ON p.id = oi.product_id
JOIN orders      o  ON oi.order_id = o.id
WHERE o.status != 'cancelled'
GROUP BY c.name
ORDER BY total_revenue DESC
LIMIT 5;
```

### Step 3 — Analyst (≈ 10 s)

The manager passes the SQL to `call_analyst`. The specialist:

1. Calls `run_query(sql)` → 5 rows returned:

   | Category | Revenue |
   |---|---|
   | Electronics | £94,570 |
   | Clothing | £50,450 |
   | Sports & Outdoors | £47,375 |
   | Books | £26,329 |
   | Home & Garden | £25,703 |

2. Calls `run_python_code(code)` with matplotlib code that:
   - Creates a horizontal bar chart sorted by revenue
   - Annotates each bar with the revenue value
   - Saves to `output/charts/chart_<id>.png`
   - Returns `CHART_SAVED:output/charts/chart_<id>.png`

3. Returns 4 key insights to the manager, including the chart filename.

### Step 4 — Writer (≈ 5 s)

The manager passes insights and chart filename to `call_writer`, which produces:

> **Executive Summary** — Electronics dominates revenue at ~£95K, accounting for ~38% of the top-5 total. Clothing and Sports & Outdoors together add another ~£98K.
>
> **Key Findings** — Electronics: £94,570 (38%) · Clothing: £50,450 (20%) · Sports & Outdoors: £47,375 (19%) · Books: £26,329 (10%) · Home & Garden: £25,703 (10%) ...

### Step 5 — Manager Synthesis

The manager streams its own final paragraph summarising the report, then yields `done`. Total wall-clock time: **~35–45 seconds**.

---

## File Structure

```
Autonomous Data Analyst/
├── app.py                  FastAPI server — SSE endpoint, chart serving
├── orchestration.py        Agent definitions + run_analysis() generator
├── mcp_server.py           FastMCP SQLite server (stdio transport)
├── tools.py                run_python_code() — chart generation tool
├── run.py                  Launcher (auto-seeds DB, starts uvicorn)
│
├── instructions/
│   ├── manager.md          Orchestration workflow and delegation rules
│   ├── data_explorer.md    Schema discovery instructions
│   ├── sql_writer.md       SQL writing rules (SQLite-specific)
│   ├── analyst.md          Query execution + matplotlib chart rules
│   └── writer.md           Report structure and style guide
│
├── data/
│   ├── seed_db.py          Creates sample.db (8 categories, 46 products,
│   └── sample.db           300 customers, 1200 orders, 3551 line items)
│
├── static/
│   └── index.html          Single-page web UI (vanilla JS + marked.js)
│
├── output/
│   └── charts/             Generated PNG charts served at /charts/<file>
│
├── .env                    OPENAI_API_KEY and config (not committed)
├── .env.example            Template for .env
└── requirements.txt        Python dependencies
```

---

## Setup

**Prerequisites:** Python 3.11+

```bash
# 1. Clone / copy the project
cd "Autonomous Data Analyst"

# 2. Create virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and set OPENAI_API_KEY=sk-...

# 4. Seed the database (only needed once)
python data/seed_db.py
```

---

## Running

### Web UI

```bash
python run.py
# open http://127.0.0.1:8000
```

`run.py` automatically seeds the database if `data/sample.db` does not exist.

Optional flags:

```bash
python run.py --host 0.0.0.0 --port 8080
```

### CLI (no web server)

```bash
python orchestration.py "What is the gross margin by category?"
python orchestration.py "Which month had the highest sales in 2024?"
```

---

## Configuration

All settings are read from `.env`:

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | Required. OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model for all agents. Use `gpt-4o` for higher quality |
| `HOST` | `127.0.0.1` | Server bind address |
| `PORT` | `8000` | Server port |
| `DB_PATH` | `data/sample.db` | SQLite database path |
| `CHARTS_DIR` | `output/charts` | Directory for generated chart PNGs |

---

## Sample Questions

The web UI includes five example prompts. Others that work well:

- *What is the average order value by country?*
- *Which products have the highest gross margin percentage?*
- *How does revenue trend month-over-month across 2024?*
- *What percentage of orders were cancelled, by category?*
- *Who are the top 10 customers by lifetime spend?*

---

## Dependencies

| Package | Purpose |
|---|---|
| `openai-agents` | Multi-agent orchestration SDK — `Agent`, `Runner`, `MCPServerStdio`, `agent.as_tool()` |
| `mcp` | FastMCP server framework for SQLite tools |
| `fastapi` + `uvicorn` | Web server and ASGI runner |
| `matplotlib` + `pandas` | Chart generation inside `run_python_code()` |
| `python-dotenv` | `.env` loading |
