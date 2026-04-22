"""
SQL Governance MCP Server
--------------------------
Stateless FastMCP stdio server.
Started once per analysis session alongside the warehouse server.

Tools:
  lint_sql(sql, datasource_id)        → {passes, warnings, errors}
  estimate_cost(sql, datasource_id)   → {estimated_rows, estimated_bytes, note}
  redact_pii(rows, catalog)           → masked rows

stdout is reserved for the stdio MCP transport; all logging goes to stderr.
"""

import json
import logging
import os
import re
import sys
from typing import Annotated

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

load_dotenv()

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [governance_server] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_POSTGRES_DSN_SYNC = os.getenv("POSTGRES_DSN_SYNC", "")

# Fact tables that should always have a WHERE clause to avoid full scans
_FACT_TABLES = {"orders", "order_items", "events", "transactions"}

# DML/DDL keywords forbidden in analyst queries
_FORBIDDEN_PATTERN = re.compile(
    r"\b(DELETE|UPDATE|INSERT|DROP|ALTER|CREATE|TRUNCATE|MERGE)\b",
    re.IGNORECASE,
)

mcp = FastMCP("sql-governance")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def lint_sql(
    sql: Annotated[str, Field(description="The SQL query to lint.")],
    datasource_id: Annotated[
        str,
        Field(description="ID of the datasource this query targets (used for context logging)."),
    ],
) -> str:
    """
    Lint a SQL query for errors and warnings.

    Returns a JSON object: {passes: bool, warnings: [str], errors: [str]}

    Errors (must fix before executing):
      - Any DML or DDL keyword: DELETE, UPDATE, INSERT, DROP, ALTER, CREATE, TRUNCATE, MERGE

    Warnings (should address if possible):
      - SELECT * without specifying columns
      - Missing LIMIT clause
      - Querying a fact table (orders, order_items, events, transactions) without a WHERE clause
    """
    logger.info("lint_sql called: datasource_id=%s sql=%.120s", datasource_id, sql)

    errors: list[str] = []
    warnings: list[str] = []

    for match in _FORBIDDEN_PATTERN.finditer(sql):
        errors.append(
            f"Forbidden keyword '{match.group().upper()}' at position {match.start()}. "
            "Only SELECT statements are permitted."
        )

    # SELECT * — match SELECT immediately followed by * (not SELECT COUNT(*))
    if re.search(r"\bSELECT\s+\*", sql, re.IGNORECASE):
        warnings.append(
            "SELECT * is discouraged; enumerate only the columns you need."
        )

    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        warnings.append(
            "Query has no LIMIT clause; large result sets may be slow or truncated unexpectedly."
        )

    # Fact table without WHERE
    for table in _FACT_TABLES:
        if re.search(rf"\b{re.escape(table)}\b", sql, re.IGNORECASE):
            if not re.search(r"\bWHERE\b", sql, re.IGNORECASE):
                warnings.append(
                    f"Query scans fact table '{table}' without a WHERE clause; "
                    "consider adding a filter to reduce scan cost."
                )
            break

    return json.dumps({"passes": len(errors) == 0, "warnings": warnings, "errors": errors})


@mcp.tool()
def estimate_cost(
    sql: Annotated[str, Field(description="The SELECT query to estimate cost for.")],
    datasource_id: Annotated[
        str,
        Field(description="ID of the datasource to run the estimate against."),
    ],
) -> str:
    """
    Estimate the execution cost of a SQL query without running it.

    Returns a JSON object: {estimated_rows, estimated_bytes, note}

    Behaviour by dialect:
      - SQLite:    EXPLAIN QUERY PLAN — returns raw plan text; row/byte counts are None
      - Postgres:  EXPLAIN (FORMAT JSON) — extracts Plan Rows × Plan Width
      - BigQuery:  dry-run → total_bytes_processed (requires google-cloud-bigquery)
      - Snowflake: unsupported — returns a descriptive note
    """
    logger.info("estimate_cost called: datasource_id=%s sql=%.120s", datasource_id, sql)

    if not datasource_id or datasource_id.strip().lower() in ("", "none"):
        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": None,
                "note": "No datasource_id provided; cost estimation skipped.",
            }
        )

    try:
        ds = _load_datasource_for_cost(datasource_id)
    except Exception as exc:
        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": None,
                "note": f"Could not load datasource: {exc}",
            }
        )

    ds_type = ds["type"]
    conn_str = ds["conn_str"]

    if ds_type == "sqlite":
        return _estimate_sqlite(sql, conn_str)
    elif ds_type in ("postgres", "postgresql"):
        return _estimate_postgres(sql, conn_str)
    elif ds_type == "bigquery":
        return _estimate_bigquery(sql, conn_str)
    elif ds_type == "snowflake":
        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": None,
                "note": "Cost estimation is not supported for Snowflake datasources.",
            }
        )
    else:
        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": None,
                "note": f"Cost estimation is not supported for dialect '{ds_type}'.",
            }
        )


@mcp.tool()
def redact_pii(
    rows: Annotated[
        str,
        Field(description="JSON array of row objects (e.g. output from run_query)."),
    ],
    catalog: Annotated[
        str,
        Field(
            description=(
                'JSON object mapping column names to "pii" or "safe". '
                'Example: {"email": "pii", "name": "pii", "total": "safe"}. '
                "Columns absent from the catalog are treated as safe."
            )
        ),
    ],
) -> str:
    """
    Replace PII column values in a list of rows with "***REDACTED***".

    Returns the same JSON array with PII values masked.

    Note: this is a placeholder — full PII catalog integration is scheduled for P2.
    """
    logger.info("redact_pii called")

    try:
        row_list = json.loads(rows)
        col_catalog: dict = json.loads(catalog)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"Invalid JSON input: {exc}"})

    pii_columns = {col for col, label in col_catalog.items() if label == "pii"}

    redacted = [
        {k: ("***REDACTED***" if k in pii_columns else v) for k, v in row.items()}
        for row in row_list
    ]
    return json.dumps(redacted)


# ---------------------------------------------------------------------------
# Helpers for estimate_cost
# ---------------------------------------------------------------------------

def _load_datasource_for_cost(datasource_id: str) -> dict:
    """Load datasource type and resolved connection string from Postgres."""
    import psycopg2

    if not _POSTGRES_DSN_SYNC:
        raise ValueError("POSTGRES_DSN_SYNC is not set")

    conn = psycopg2.connect(_POSTGRES_DSN_SYNC)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT type, connection_secret_ref FROM datasources WHERE id = %s",
            (datasource_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise ValueError(f"Datasource '{datasource_id}' not found")

    ds_type, secret_ref = row
    conn_str = os.getenv(f"SECRET_{secret_ref}")
    if not conn_str:
        raise ValueError(
            f"Environment variable 'SECRET_{secret_ref}' is not set. "
            "Set it to the connection string for this datasource."
        )

    return {"type": ds_type, "conn_str": conn_str}


def _estimate_sqlite(sql: str, conn_str: str) -> str:
    import sqlite3

    db_path = conn_str
    for prefix in ("sqlite:///", "sqlite://"):
        if db_path.startswith(prefix):
            db_path = db_path[len(prefix):]

    try:
        con = sqlite3.connect(db_path)
        cur = con.execute(f"EXPLAIN QUERY PLAN {sql}")
        plan_rows = cur.fetchall()
        con.close()
        plan_text = "\n".join(
            f"  {r[0]} {r[1]} {r[2]} {r[3]}" for r in plan_rows
        )
        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": None,
                "note": f"SQLite EXPLAIN QUERY PLAN:\n{plan_text}",
            }
        )
    except Exception as exc:
        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": None,
                "note": f"Could not run EXPLAIN QUERY PLAN: {exc}",
            }
        )


def _estimate_postgres(sql: str, conn_str: str) -> str:
    import psycopg2

    # Normalise to a psycopg2-compatible DSN
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://"):
        if conn_str.startswith(prefix):
            conn_str = "postgresql://" + conn_str[len(prefix):]

    try:
        conn = psycopg2.connect(conn_str)
        cur = conn.cursor()
        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
        plan = cur.fetchone()[0]
        conn.close()

        top_node = plan[0]["Plan"]
        estimated_rows = int(top_node.get("Plan Rows", 0))
        plan_width = int(top_node.get("Plan Width", 0))
        estimated_bytes = estimated_rows * plan_width

        return json.dumps(
            {
                "estimated_rows": estimated_rows,
                "estimated_bytes": estimated_bytes,
                "note": (
                    f"Postgres EXPLAIN: ~{estimated_rows:,} rows × "
                    f"{plan_width} bytes/row = ~{estimated_bytes:,} bytes"
                ),
            }
        )
    except Exception as exc:
        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": None,
                "note": f"Could not run EXPLAIN: {exc}",
            }
        )


def _estimate_bigquery(sql: str, conn_str: str) -> str:
    try:
        from google.cloud import bigquery

        client = bigquery.Client()
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(sql, job_config=job_config)
        bytes_processed = job.total_bytes_processed

        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": bytes_processed,
                "note": f"BigQuery dry-run: {bytes_processed:,} bytes will be processed.",
            }
        )
    except ImportError:
        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": None,
                "note": "google-cloud-bigquery is not installed; cannot estimate BigQuery cost.",
            }
        )
    except Exception as exc:
        return json.dumps(
            {
                "estimated_rows": None,
                "estimated_bytes": None,
                "note": f"BigQuery dry-run failed: {exc}",
            }
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting governance MCP server (stdio)")
    mcp.run()
