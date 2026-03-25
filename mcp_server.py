"""
SQLite MCP Server
-----------------
Exposes three tools to any MCP client (e.g. the openai-agents SDK):

  list_tables()            — returns all table names in the database
  get_schema(table_name)   — returns column info + CREATE SQL for a table
  run_query(sql)           — executes a read-only SELECT and returns JSON rows

Transport: stdio  (launched as a subprocess by the agent runner)

Run directly for testing:
  python mcp_server.py
"""

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / "data" / "sample.db"

# Log to stderr only — stdout is reserved for the stdio MCP transport
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [mcp_server] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
mcp = FastMCP("sqlite-analyst")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _connect_readonly() -> sqlite3.Connection:
    """Open the SQLite database in read-only mode via URI."""
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {DB_PATH}. Run 'python data/seed_db.py' first."
        )
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_tables() -> str:
    """
    Return a JSON array of every table name in the database.

    Use this first to discover what data is available before writing queries.
    """
    logger.info("list_tables called")
    with _connect_readonly() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    tables = [row["name"] for row in rows]
    return json.dumps(tables)


@mcp.tool()
def get_schema(
    table_name: Annotated[
        str,
        Field(description="Exact name of the table whose schema you want to inspect."),
    ],
) -> str:
    """
    Return the schema for a single table as a JSON object containing:
      - table:      table name
      - create_sql: the original CREATE TABLE statement
      - columns:    list of {cid, name, type, notnull, default_value, pk}
      - row_count:  approximate number of rows

    Always call this before writing SQL for a table you haven't seen yet.
    """
    logger.info("get_schema called: table=%s", table_name)

    with _connect_readonly() as conn:
        # Validate table exists
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not exists:
            return json.dumps({"error": f"Table '{table_name}' does not exist."})

        # Column metadata
        pragma_rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        columns = [
            {
                "cid":           row["cid"],
                "name":          row["name"],
                "type":          row["type"],
                "notnull":       bool(row["notnull"]),
                "default_value": row["dflt_value"],
                "pk":            bool(row["pk"]),
            }
            for row in pragma_rows
        ]

        # Foreign keys
        fk_rows = conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
        foreign_keys = [
            {
                "column":     row["from"],
                "references": f"{row['table']}({row['to']})",
            }
            for row in fk_rows
        ]

        # CREATE SQL
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        create_sql = sql_row["sql"] if sql_row else ""

        # Row count
        count_row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
        row_count = count_row["n"] if count_row else 0

    result = {
        "table":        table_name,
        "row_count":    row_count,
        "create_sql":   create_sql,
        "columns":      columns,
        "foreign_keys": foreign_keys,
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def run_query(
    sql: Annotated[
        str,
        Field(
            description=(
                "A well-formed SQLite SELECT statement. "
                "Only SELECT queries are permitted. "
                "Results are capped at 50 rows."
            )
        ),
    ],
) -> str:
    """
    Execute a read-only SELECT query and return results as a JSON array of objects.

    Rules:
    - Only SELECT statements are allowed.
    - Results are limited to 50 rows; include a note in your answer if the
      data may be truncated.
    - Use exact table/column names from get_schema — never invent names.
    - Aggregate and JOIN where appropriate to produce concise, meaningful results.
    """
    logger.info("run_query called: sql=%s", sql[:120])

    stripped = sql.strip()
    if not stripped.upper().startswith("SELECT"):
        return json.dumps({"error": "Only SELECT statements are permitted."})

    try:
        with _connect_readonly() as conn:
            cur = conn.execute(stripped)
            rows = cur.fetchmany(50)
            column_names = [desc[0] for desc in cur.description] if cur.description else []
            data = [dict(zip(column_names, row)) for row in rows]

        result = {
            "row_count": len(data),
            "truncated": len(data) == 50,
            "columns":   column_names,
            "rows":      data,
        }
        return json.dumps(result, default=str)

    except sqlite3.Error as exc:
        logger.error("Query error: %s", exc)
        return json.dumps({"error": f"SQLite error: {exc}"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not DB_PATH.exists():
        logger.error(
            "Database not found at %s. Run 'python data/seed_db.py' first.", DB_PATH
        )
        sys.exit(1)

    logger.info("Starting SQLite MCP server (stdio) — database: %s", DB_PATH)
    mcp.run()  # stdio transport by default
