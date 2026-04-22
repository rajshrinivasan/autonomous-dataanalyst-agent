"""
Unit tests for mcp_servers/governance_server.py.

Tests cover:
  - lint_sql: all error rules (DML/DDL keywords) and warning rules
  - estimate_cost: dialect branching (no-datasource, SQLite, Postgres, BigQuery, Snowflake)
  - redact_pii: column masking and empty catalog
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_servers.governance_server import (
    estimate_cost,
    lint_sql,
    redact_pii,
)

# ---------------------------------------------------------------------------
# lint_sql — error rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keyword,sql", [
    ("DELETE", "DELETE FROM orders WHERE id = 1"),
    ("UPDATE", "UPDATE orders SET status = 'shipped' WHERE id = 1"),
    ("INSERT", "INSERT INTO orders (status) VALUES ('pending')"),
    ("DROP",   "DROP TABLE orders"),
    ("ALTER",  "ALTER TABLE orders ADD COLUMN foo TEXT"),
    ("CREATE", "CREATE TABLE tmp AS SELECT * FROM orders"),
    ("TRUNCATE","TRUNCATE orders"),
    ("MERGE",  "MERGE INTO orders USING src ON orders.id = src.id WHEN MATCHED THEN DELETE"),
])
def test_lint_sql_rejects_dml_ddl(keyword, sql):
    result = json.loads(lint_sql(sql, "ds-test"))
    assert not result["passes"], f"Expected {keyword} to be rejected"
    assert any(keyword in e for e in result["errors"])


def test_lint_sql_rejects_case_insensitive():
    result = json.loads(lint_sql("delete from orders", "ds-test"))
    assert not result["passes"]
    assert result["errors"]


# ---------------------------------------------------------------------------
# lint_sql — warning rules
# ---------------------------------------------------------------------------

def test_lint_sql_warns_select_star():
    result = json.loads(lint_sql("SELECT * FROM products LIMIT 10", "ds-test"))
    assert result["passes"]
    assert any("SELECT *" in w for w in result["warnings"])


def test_lint_sql_count_star_no_warning():
    # COUNT(*) should not trigger SELECT * warning
    result = json.loads(
        lint_sql("SELECT COUNT(*) FROM orders WHERE status = 'shipped'", "ds-test")
    )
    assert result["passes"]
    assert not any("SELECT *" in w for w in result["warnings"])


def test_lint_sql_warns_missing_limit():
    result = json.loads(lint_sql("SELECT id FROM products", "ds-test"))
    assert result["passes"]
    assert any("LIMIT" in w for w in result["warnings"])


def test_lint_sql_no_limit_warning_when_limit_present():
    result = json.loads(lint_sql("SELECT id FROM products LIMIT 25", "ds-test"))
    assert not any("LIMIT" in w for w in result["warnings"])


@pytest.mark.parametrize("table", ["orders", "order_items", "events", "transactions"])
def test_lint_sql_warns_fact_table_without_where(table):
    sql = f"SELECT id FROM {table} LIMIT 10"
    result = json.loads(lint_sql(sql, "ds-test"))
    assert result["passes"]
    assert any(table in w for w in result["warnings"])


def test_lint_sql_no_fact_table_warning_when_where_present():
    result = json.loads(
        lint_sql("SELECT id FROM orders WHERE status = 'shipped' LIMIT 10", "ds-test")
    )
    assert not any("orders" in w for w in result["warnings"])


def test_lint_sql_clean_query_passes_no_warnings():
    result = json.loads(
        lint_sql(
            "SELECT p.name, SUM(oi.quantity) AS qty "
            "FROM products p "
            "JOIN order_items oi ON p.id = oi.product_id "
            "WHERE p.category_id = 3 "
            "GROUP BY p.name "
            "ORDER BY qty DESC "
            "LIMIT 10",
            "ds-test",
        )
    )
    assert result["passes"]
    assert result["errors"] == []
    assert result["warnings"] == []


# ---------------------------------------------------------------------------
# estimate_cost — dialect branching
# ---------------------------------------------------------------------------

def test_estimate_cost_no_datasource_id():
    result = json.loads(estimate_cost("SELECT 1", ""))
    assert result["estimated_rows"] is None
    assert result["estimated_bytes"] is None
    assert "skipped" in result["note"].lower()


def test_estimate_cost_none_string_datasource_id():
    result = json.loads(estimate_cost("SELECT 1", "none"))
    assert "skipped" in result["note"].lower()


def test_estimate_cost_datasource_not_found():
    with patch("mcp_servers.governance_server._load_datasource_for_cost") as mock_load:
        mock_load.side_effect = ValueError("Datasource 'bad-id' not found")
        result = json.loads(estimate_cost("SELECT 1", "bad-id"))
    assert result["estimated_rows"] is None
    assert "Could not load datasource" in result["note"]


def test_estimate_cost_sqlite(tmp_path):
    import sqlite3
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()

    ds = {"type": "sqlite", "conn_str": f"sqlite:///{db}"}
    with patch("mcp_servers.governance_server._load_datasource_for_cost", return_value=ds):
        result = json.loads(estimate_cost("SELECT id FROM t", "ds-sqlite"))

    assert "EXPLAIN QUERY PLAN" in result["note"]
    assert result["estimated_bytes"] is None


def test_estimate_cost_sqlite_bad_sql(tmp_path):
    import sqlite3
    db = tmp_path / "test.db"
    sqlite3.connect(str(db)).close()

    ds = {"type": "sqlite", "conn_str": str(db)}
    with patch("mcp_servers.governance_server._load_datasource_for_cost", return_value=ds):
        result = json.loads(estimate_cost("NOT VALID SQL %%", "ds-sqlite"))

    assert result["estimated_rows"] is None
    assert "Could not run EXPLAIN" in result["note"]


def test_estimate_cost_postgres():
    plan_json = [{"Plan": {"Plan Rows": 500, "Plan Width": 128, "Node Type": "Seq Scan"}}]
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = (plan_json,)
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    ds = {"type": "postgres", "conn_str": "postgresql://user:pass@localhost/db"}
    with patch("mcp_servers.governance_server._load_datasource_for_cost", return_value=ds):
        with patch("psycopg2.connect", return_value=mock_conn):
            result = json.loads(
                estimate_cost("SELECT id FROM orders WHERE status='shipped'", "ds-pg")
            )

    assert result["estimated_rows"] == 500
    assert result["estimated_bytes"] == 500 * 128


def test_estimate_cost_snowflake():
    ds = {"type": "snowflake", "conn_str": "snowflake://..."}
    with patch("mcp_servers.governance_server._load_datasource_for_cost", return_value=ds):
        result = json.loads(estimate_cost("SELECT 1", "ds-snow"))
    assert "not supported" in result["note"].lower()
    assert result["estimated_rows"] is None


def test_estimate_cost_bigquery_missing_package():
    ds = {"type": "bigquery", "conn_str": "bigquery://project/dataset"}
    with patch("mcp_servers.governance_server._load_datasource_for_cost", return_value=ds):
        with patch.dict("sys.modules", {"google.cloud.bigquery": None}):
            result = json.loads(estimate_cost("SELECT 1", "ds-bq"))
    # Either not installed or some error — note must explain
    assert result["estimated_rows"] is None
    assert result["note"]


def test_estimate_cost_unknown_dialect():
    ds = {"type": "mysql", "conn_str": "mysql://..."}
    with patch("mcp_servers.governance_server._load_datasource_for_cost", return_value=ds):
        result = json.loads(estimate_cost("SELECT 1", "ds-mysql"))
    assert "not supported" in result["note"].lower()


# ---------------------------------------------------------------------------
# redact_pii
# ---------------------------------------------------------------------------

def test_redact_pii_replaces_pii_columns():
    rows = json.dumps([{"email": "user@example.com", "name": "Alice", "total": 99.99}])
    catalog = json.dumps({"email": "pii", "name": "pii", "total": "safe"})
    result = json.loads(redact_pii(rows, catalog))
    assert result[0]["email"] == "***REDACTED***"
    assert result[0]["name"] == "***REDACTED***"
    assert result[0]["total"] == 99.99


def test_redact_pii_empty_catalog_leaves_all_safe():
    rows = json.dumps([{"email": "user@example.com"}])
    result = json.loads(redact_pii(rows, "{}"))
    assert result[0]["email"] == "user@example.com"


def test_redact_pii_column_not_in_catalog_is_safe():
    rows = json.dumps([{"email": "x@y.com", "unknown_col": "value"}])
    catalog = json.dumps({"email": "pii"})
    result = json.loads(redact_pii(rows, catalog))
    assert result[0]["email"] == "***REDACTED***"
    assert result[0]["unknown_col"] == "value"


def test_redact_pii_multiple_rows():
    rows = json.dumps([
        {"email": "a@b.com", "amount": 10},
        {"email": "c@d.com", "amount": 20},
    ])
    catalog = json.dumps({"email": "pii"})
    result = json.loads(redact_pii(rows, catalog))
    assert all(r["email"] == "***REDACTED***" for r in result)
    assert result[0]["amount"] == 10


def test_redact_pii_invalid_json_rows():
    result = json.loads(redact_pii("not json", "{}"))
    assert "error" in result


def test_redact_pii_invalid_json_catalog():
    rows = json.dumps([{"a": 1}])
    result = json.loads(redact_pii(rows, "not json"))
    assert "error" in result
