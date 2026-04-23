"""
Layer 3: Deterministic Invariants

Behavioral guarantees the system must NEVER violate, regardless of inputs.
Each invariant is documented with what contract it enforces.
"""
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import mcp_server
from mcp_server import run_query
from mcp_servers.governance_server import lint_sql
from workflow.graph import _route_governance
from workflow.sse_adapter import stream_as_sse


# ---------------------------------------------------------------------------
# Fixtures shared across invariant groups
# ---------------------------------------------------------------------------

@pytest.fixture()
def small_db(tmp_path) -> Path:
    db = tmp_path / "inv.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
    con.executemany("INSERT INTO items VALUES (?, ?)", [(i, f"v{i}") for i in range(1, 6)])
    con.commit()
    con.close()
    return db


@pytest.fixture()
def large_db(tmp_path) -> Path:
    db = tmp_path / "large.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE big_table (id INTEGER PRIMARY KEY, val TEXT)")
    con.executemany("INSERT INTO big_table VALUES (?, ?)", [(i, f"v{i}") for i in range(1, 61)])
    con.commit()
    con.close()
    return db


@pytest.fixture()
def patch_small_db(monkeypatch, small_db):
    monkeypatch.setattr(mcp_server, "DB_PATH", small_db)


@pytest.fixture()
def patch_large_db(monkeypatch, large_db):
    monkeypatch.setattr(mcp_server, "DB_PATH", large_db)


# ---------------------------------------------------------------------------
# I1 — run_query() ALWAYS rejects non-SELECT DML/DDL
#
# Contract: no matter what SQL is passed, non-SELECT statements must never
# reach the database.  Enforced by the prefix check in run_query().
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_sql", [
    "DELETE FROM items WHERE id = 1",
    "UPDATE items SET val = 'x'",
    "INSERT INTO items VALUES (99, 'y')",
    "DROP TABLE items",
    "ALTER TABLE items ADD COLUMN foo TEXT",
    "CREATE TABLE tmp AS SELECT 1",
    "TRUNCATE items",
    "  DELETE FROM items",          # leading spaces must not bypass the check
    "\tUPDATE items SET val='x'",   # leading tab
    "\nDROP TABLE items",           # leading newline
])
def test_i1_run_query_always_rejects_dml(patch_small_db, bad_sql):
    result = json.loads(run_query(bad_sql))
    assert "error" in result, f"DML not rejected: {bad_sql!r}"
    assert "Only SELECT" in result["error"]


# ---------------------------------------------------------------------------
# I2 — run_query() ALWAYS caps results at 50 rows
#
# Contract: even when the underlying table has hundreds of rows, run_query
# returns at most 50 and sets truncated=True.
# ---------------------------------------------------------------------------

def test_i2_run_query_always_caps_at_50_rows(patch_large_db):
    result = json.loads(run_query("SELECT * FROM big_table"))
    assert result["row_count"] <= 50, (
        f"Row cap violated: got {result['row_count']} rows"
    )
    assert result["truncated"] is True


def test_i2_truncated_false_when_under_cap(patch_small_db):
    # 5 rows → under the cap → truncated must be False
    result = json.loads(run_query("SELECT * FROM items"))
    assert result["row_count"] == 5
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# I3 — /charts/{filename} ALWAYS rejects filenames containing ".."
#
# Contract: the serve_chart endpoint must return HTTP 400 for any filename
# that contains ".." (directory traversal indicator).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_filename", [
    "bad..file.png",
    "test..png",
    "a..b",
    "some..path..file.png",
])
def test_i3_charts_always_rejects_double_dot(bad_filename):
    from fastapi.testclient import TestClient
    from auth.dependencies import get_current_user, TokenData
    from app import app

    app.dependency_overrides[get_current_user] = lambda: TokenData(
        sub="u", workspace_id="11111111-1111-1111-1111-111111111111", role="analyst"
    )
    try:
        client = TestClient(app)
        resp = client.get(f"/charts/{bad_filename}")
        assert resp.status_code == 400, (
            f"Expected 400 for {bad_filename!r}, got {resp.status_code}"
        )
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# I5 — lint_revision_count NEVER exceeds 3
#
# Contract: _route_governance routes to analyst (not sql_writer) once
# lint_revision_count reaches 3, regardless of whether errors are present.
# ---------------------------------------------------------------------------

def test_i5_route_governance_routes_to_analyst_at_count_3():
    state = {
        "lint_result": {"errors": ["some error"], "passes": False, "warnings": []},
        "lint_revision_count": 3,
    }
    result = _route_governance(state)
    assert result == "analyst", (
        f"Expected 'analyst' at revision_count=3, got '{result}'"
    )


def test_i5_route_governance_routes_to_analyst_above_3():
    # Should never happen in practice, but the condition is `< 3`, not `== 3`
    state = {
        "lint_result": {"errors": ["some error"], "passes": False},
        "lint_revision_count": 99,
    }
    assert _route_governance(state) == "analyst"


@pytest.mark.parametrize("count", [0, 1, 2])
def test_i5_route_governance_retries_below_3(count):
    state = {
        "lint_result": {"errors": ["err"], "passes": False},
        "lint_revision_count": count,
    }
    assert _route_governance(state) == "sql_writer", (
        f"Expected 'sql_writer' at revision_count={count}"
    )


def test_i5_route_governance_routes_to_analyst_when_no_errors():
    state = {
        "lint_result": {"errors": [], "passes": True, "warnings": []},
        "lint_revision_count": 0,
    }
    assert _route_governance(state) == "analyst"


# ---------------------------------------------------------------------------
# I6 — lint_sql() ALWAYS puts DML keywords in errors, not warnings
#
# Contract: dangerous mutations must be a hard error (not a soft warning),
# so the governance feedback loop always rejects them.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keyword,sql", [
    ("DELETE", "DELETE FROM orders WHERE id = 1"),
    ("UPDATE", "UPDATE orders SET status = 'shipped'"),
    ("INSERT", "INSERT INTO orders (status) VALUES ('pending')"),
    ("DROP",   "DROP TABLE orders"),
    ("ALTER",  "ALTER TABLE orders ADD COLUMN x TEXT"),
    ("CREATE", "CREATE TABLE tmp AS SELECT 1"),
    ("TRUNCATE", "TRUNCATE orders"),
    ("MERGE",  "MERGE INTO orders USING src ON orders.id=src.id WHEN MATCHED THEN DELETE"),
])
def test_i6_lint_sql_always_produces_errors_for_dml(keyword, sql):
    result = json.loads(lint_sql(sql, "ds-test"))
    assert len(result["errors"]) >= 1, (
        f"{keyword} must produce errors (not warnings), got: {result}"
    )
    assert not result["passes"]
    # The keyword must NOT appear only in warnings
    assert not any(
        keyword.upper() in w.upper() for w in result.get("warnings", [])
        if keyword.upper() not in " ".join(result["errors"]).upper()
    )


# ---------------------------------------------------------------------------
# I7 — SSE stream ALWAYS ends with 'done' or 'error'
#
# Contract: stream_as_sse has a safety-net that emits a done event even
# when the upstream graph ends without an explicit on_chain_end.
# ---------------------------------------------------------------------------

async def _collect_sse(events: list[dict]) -> list[dict]:
    async def _gen():
        for e in events:
            yield e
    return [e async for e in stream_as_sse(_gen())]


async def test_i7_sse_ends_with_done_on_empty_stream():
    results = await _collect_sse([])
    assert results[-1]["type"] in ("done", "error"), (
        f"Last event type was '{results[-1]['type']}', expected done or error"
    )


async def test_i7_sse_ends_with_done_after_only_text_deltas():
    chunk = SimpleNamespace(content="some text")
    events = [
        {"event": "on_chat_model_stream", "name": "ChatOpenAI",
         "data": {"chunk": chunk}, "metadata": {"langgraph_node": "writer_node"}},
    ]
    results = await _collect_sse(events)
    assert results[-1]["type"] in ("done", "error")


async def test_i7_sse_ends_with_done_when_chain_end_has_no_report():
    events = [
        {"event": "on_chain_end", "name": "AnalysisGraph",
         "data": {"output": {}}, "metadata": {}},
    ]
    results = await _collect_sse(events)
    assert results[-1]["type"] == "done"


async def test_i7_done_output_matches_report_from_chain_end():
    events = [
        {"event": "on_chain_end", "name": "AnalysisGraph",
         "data": {"output": {"report": "the final report"}}, "metadata": {}},
    ]
    results = await _collect_sse(events)
    done = next(e for e in results if e["type"] == "done")
    assert done["output"] == "the final report"
