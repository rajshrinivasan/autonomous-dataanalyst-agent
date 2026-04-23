"""
Unit tests for mcp_server.py — list_tables, get_schema, run_query.

Monkeypatches mcp_server.DB_PATH to a tmp_path SQLite so tests never
touch the real data/sample.db.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import mcp_server
from mcp_server import get_schema, list_tables, run_query


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def small_db(tmp_path) -> Path:
    """Temp SQLite with 5 rows — for basic SELECT tests."""
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)"
    )
    con.executemany(
        "INSERT INTO products VALUES (?, ?, ?)",
        [(i, f"product_{i}", float(i) * 1.5) for i in range(1, 6)],
    )
    con.commit()
    con.close()
    return db


@pytest.fixture()
def large_db(tmp_path) -> Path:
    """Temp SQLite with 60 rows — for testing the 50-row cap."""
    db = tmp_path / "large.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE big_table (id INTEGER PRIMARY KEY, value TEXT)")
    con.executemany(
        "INSERT INTO big_table VALUES (?, ?)",
        [(i, f"val_{i}") for i in range(1, 61)],
    )
    con.commit()
    con.close()
    return db


@pytest.fixture()
def patch_db(monkeypatch, small_db):
    monkeypatch.setattr(mcp_server, "DB_PATH", small_db)


@pytest.fixture()
def patch_large_db(monkeypatch, large_db):
    monkeypatch.setattr(mcp_server, "DB_PATH", large_db)


# ---------------------------------------------------------------------------
# list_tables
# ---------------------------------------------------------------------------

def test_list_tables_returns_json_array(patch_db):
    result = json.loads(list_tables())
    assert isinstance(result, list)
    assert "products" in result


def test_list_tables_db_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "DB_PATH", tmp_path / "nonexistent.db")
    with pytest.raises(FileNotFoundError):
        list_tables()


# ---------------------------------------------------------------------------
# get_schema
# ---------------------------------------------------------------------------

def test_get_schema_existing_table(patch_db):
    result = json.loads(get_schema("products"))
    assert result["table"] == "products"
    assert "columns" in result
    assert "row_count" in result
    assert "create_sql" in result
    assert result["row_count"] == 5


def test_get_schema_includes_expected_columns(patch_db):
    result = json.loads(get_schema("products"))
    col_names = [c["name"] for c in result["columns"]]
    assert "id" in col_names
    assert "name" in col_names
    assert "price" in col_names


def test_get_schema_nonexistent_table(patch_db):
    result = json.loads(get_schema("nonexistent_xyz"))
    assert "error" in result
    assert "does not exist" in result["error"]


# ---------------------------------------------------------------------------
# run_query — basic functionality
# ---------------------------------------------------------------------------

def test_run_query_basic_select(patch_db):
    result = json.loads(run_query("SELECT id, name FROM products WHERE id <= 3"))
    assert result["row_count"] == 3
    assert "id" in result["columns"]
    assert "name" in result["columns"]
    assert len(result["rows"]) == 3


def test_run_query_returns_correct_values(patch_db):
    result = json.loads(run_query("SELECT id, name FROM products WHERE id = 1"))
    assert result["rows"][0]["name"] == "product_1"


# ---------------------------------------------------------------------------
# run_query — 50-row cap
# ---------------------------------------------------------------------------

def test_run_query_50_row_cap(patch_large_db):
    result = json.loads(run_query("SELECT * FROM big_table"))
    assert result["row_count"] == 50
    assert result["truncated"] is True


def test_run_query_not_truncated_when_fewer_than_50_rows(patch_db):
    result = json.loads(run_query("SELECT * FROM products"))
    assert result["row_count"] == 5
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# run_query — DML/DDL rejection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_sql", [
    "DELETE FROM products WHERE id = 1",
    "UPDATE products SET price = 0",
    "INSERT INTO products VALUES (99, 'x', 0.0)",
    "DROP TABLE products",
    "ALTER TABLE products ADD COLUMN foo TEXT",
    "CREATE TABLE tmp AS SELECT 1",
    "TRUNCATE products",
    "  DELETE FROM products",       # leading whitespace
    "\tUPDATE products SET name='x'",  # leading tab
    "\nDROP TABLE products",        # leading newline
])
def test_run_query_rejects_non_select(patch_db, bad_sql):
    result = json.loads(run_query(bad_sql))
    assert "error" in result, f"Expected error for: {bad_sql!r}"
    assert "Only SELECT" in result["error"]


# ---------------------------------------------------------------------------
# run_query — error handling
# ---------------------------------------------------------------------------

def test_run_query_sqlite_error_returns_json_error(patch_db):
    result = json.loads(run_query("SELECT * FROM nonexistent_table_xyz"))
    assert "error" in result
    assert "SQLite error" in result["error"]
