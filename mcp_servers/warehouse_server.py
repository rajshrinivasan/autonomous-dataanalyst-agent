"""
Multi-Engine MCP Warehouse Server
----------------------------------
Launched as: python mcp_servers/warehouse_server.py --datasource-id <uuid>

Startup:
  1. Look up datasource record in Postgres via POSTGRES_DSN_SYNC
  2. Read SECRET_<connection_secret_ref> from env → connection string
  3. Create a SQLAlchemy engine for the target dialect

Exposes the same three tools as mcp_server.py:
  list_tables()
  get_schema(table_name)
  run_query(sql)

stdout is reserved for the stdio MCP transport; all logging goes to stderr.
"""

import argparse
import json
import logging
import os
import sys
from typing import Annotated

import psycopg2
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Inspector, Engine

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [warehouse_server] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_POSTGRES_DSN_SYNC = os.getenv("POSTGRES_DSN_SYNC", "")

# Module-level state filled in by _init()
_engine: Engine | None = None
_inspector: Inspector | None = None
_row_limit: int = 50
_default_schema: str | None = None


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("warehouse-analyst")


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------
def _load_datasource(datasource_id: str, workspace_id: str) -> dict:
    """Fetch datasource record from Postgres synchronously.

    Filters by both id and workspace_id so a client cannot query a datasource
    that belongs to a different tenant by guessing its UUID.
    """
    if not _POSTGRES_DSN_SYNC:
        raise ValueError("POSTGRES_DSN_SYNC is not set")

    conn = psycopg2.connect(_POSTGRES_DSN_SYNC)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT type, connection_secret_ref, default_schema, row_limit "
            "FROM datasources WHERE id = %s AND workspace_id = %s",
            (datasource_id, workspace_id),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise ValueError(
            f"Datasource '{datasource_id}' not found or does not belong to workspace '{workspace_id}'"
        )

    return {
        "type": row[0],
        "connection_secret_ref": row[1],
        "default_schema": row[2],
        "row_limit": row[3] or 50,
    }


def _build_engine(ds_type: str, conn_str: str) -> Engine:
    """Create a SQLAlchemy engine appropriate for the target dialect."""
    if ds_type == "sqlite":
        if not conn_str.startswith("sqlite"):
            conn_str = f"sqlite:///{conn_str}"
    elif ds_type == "postgres":
        # Ensure we use psycopg2 (sync) not asyncpg
        if conn_str.startswith("postgresql+asyncpg://"):
            conn_str = conn_str.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        elif conn_str.startswith("postgresql://"):
            conn_str = conn_str.replace("postgresql://", "postgresql+psycopg2://", 1)
    # bigquery and snowflake: use conn_str as-is (caller installs dialect)
    return create_engine(conn_str)


def _init(datasource_id: str, workspace_id: str) -> None:
    global _engine, _inspector, _row_limit, _default_schema

    ds = _load_datasource(datasource_id, workspace_id)
    secret_ref = ds["connection_secret_ref"]
    conn_str = os.getenv(f"SECRET_{secret_ref}")
    if not conn_str:
        raise ValueError(
            f"Environment variable 'SECRET_{secret_ref}' is not set. "
            "Set it to the connection string for this datasource."
        )

    _engine = _build_engine(ds["type"], conn_str)
    _inspector = inspect(_engine)
    _row_limit = ds["row_limit"]
    _default_schema = ds["default_schema"] or None

    logger.info(
        "Warehouse server ready: type=%s schema=%s row_limit=%d",
        ds["type"], _default_schema, _row_limit,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def list_tables() -> str:
    """
    Return a JSON array of every table name in the datasource.

    Use this first to discover what data is available before writing queries.
    """
    logger.info("list_tables called")
    tables = _inspector.get_table_names(schema=_default_schema)
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
      - table:       table name
      - row_count:   approximate number of rows
      - columns:     list of {name, type, nullable, default}
      - primary_key: list of PK column names
      - foreign_keys: list of {column, references}

    Always call this before writing SQL for a table you haven't seen yet.
    """
    logger.info("get_schema called: table=%s", table_name)

    all_tables = _inspector.get_table_names(schema=_default_schema)
    if table_name not in all_tables:
        return json.dumps({"error": f"Table '{table_name}' does not exist."})

    columns = [
        {
            "name": col["name"],
            "type": str(col["type"]),
            "nullable": col.get("nullable", True),
            "default": str(col.get("default", "")) or None,
        }
        for col in _inspector.get_columns(table_name, schema=_default_schema)
    ]

    pk = _inspector.get_pk_constraint(table_name, schema=_default_schema)
    foreign_keys = [
        {
            "columns": fk["constrained_columns"],
            "references": f"{fk['referred_table']}({fk['referred_columns']})",
        }
        for fk in _inspector.get_foreign_keys(table_name, schema=_default_schema)
    ]

    schema_prefix = f"{_default_schema}." if _default_schema else ""
    with _engine.connect() as conn:
        count_row = conn.execute(
            text(f"SELECT COUNT(*) FROM {schema_prefix}{table_name}")
        ).fetchone()
    row_count = count_row[0] if count_row else 0

    return json.dumps(
        {
            "table": table_name,
            "row_count": row_count,
            "columns": columns,
            "primary_key": pk.get("constrained_columns", []),
            "foreign_keys": foreign_keys,
        },
        indent=2,
    )


@mcp.tool()
def run_query(
    sql: Annotated[
        str,
        Field(
            description=(
                "A well-formed SELECT statement. "
                "Only SELECT queries are permitted. "
                "Results are capped at the datasource row_limit."
            )
        ),
    ],
) -> str:
    """
    Execute a read-only SELECT query and return results as a JSON array of objects.

    Rules:
    - Only SELECT statements are allowed.
    - Results are limited to the datasource row_limit; include a note in your
      answer if the data may be truncated.
    - Use exact table/column names from get_schema — never invent names.
    """
    logger.info("run_query called: sql=%s", sql[:120])

    stripped = sql.strip()
    if not stripped.upper().startswith("SELECT"):
        return json.dumps({"error": "Only SELECT statements are permitted."})

    try:
        with _engine.connect() as conn:
            result = conn.execute(text(stripped))
            rows = result.fetchmany(_row_limit)
            columns = list(result.keys())
            data = [dict(zip(columns, row)) for row in rows]

        return json.dumps(
            {
                "row_count": len(data),
                "truncated": len(data) == _row_limit,
                "columns": columns,
                "rows": data,
            },
            default=str,
        )

    except Exception as exc:
        logger.error("Query error: %s", exc)
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-engine MCP warehouse server")
    parser.add_argument(
        "--datasource-id",
        required=True,
        help="UUID of the datasource record in Postgres",
    )
    parser.add_argument(
        "--workspace-id",
        required=True,
        help="UUID of the requesting workspace — used to verify datasource ownership",
    )
    args = parser.parse_args()

    try:
        _init(args.datasource_id, args.workspace_id)
    except Exception as exc:
        logger.error("Failed to initialize warehouse server: %s", exc)
        sys.exit(1)

    logger.info(
        "Starting warehouse MCP server (stdio) — datasource: %s workspace: %s",
        args.datasource_id, args.workspace_id,
    )
    mcp.run()
