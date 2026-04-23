"""
Unit tests for app.py — FastAPI endpoints.

Auth is bypassed via dependency_overrides.
DB is bypassed via patch("app.get_db_session") with an async context manager.
No real Postgres or OpenAI connection is needed.
"""
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app import app
from auth.dependencies import TokenData, get_current_user

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

FAKE_WORKSPACE = "11111111-1111-1111-1111-111111111111"
FAKE_TOKEN = TokenData(sub="user-1", workspace_id=FAKE_WORKSPACE, role="analyst")
FIXED_DT = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_mock_ds(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        workspace_id=uuid.UUID(FAKE_WORKSPACE),
        name="test-datasource",
        type="sqlite",
        connection_secret_ref="projects/my-project/secrets/conn",
        default_schema=None,
        row_limit=50,
        created_at=FIXED_DT,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def override_auth():
    app.dependency_overrides[get_current_user] = lambda: FAKE_TOKEN
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def mock_session():
    session = AsyncMock()
    # AsyncSession.add() is synchronous — override to avoid an unawaited-coroutine warning
    session.add = MagicMock()
    return session


@pytest.fixture()
def client_with_db(mock_session):
    """TestClient with get_db_session patched to yield mock_session."""
    @asynccontextmanager
    async def _fake_db(workspace_id=None):
        yield mock_session

    with patch("app.get_db_session", _fake_db):
        yield TestClient(app), mock_session


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "db_exists" in body


def test_health_no_auth_required(client):
    # Health endpoint has no dependency on RequireAnalyst; calling without
    # auth override still returns 200 (auth override is still active from
    # the autouse fixture, but the endpoint itself has no auth dep).
    app.dependency_overrides.clear()
    resp = client.get("/health")
    assert resp.status_code == 200
    # restore for other tests
    app.dependency_overrides[get_current_user] = lambda: FAKE_TOKEN


# ---------------------------------------------------------------------------
# GET /charts/{filename} — path traversal protection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_filename", [
    "bad..file.png",         # contains ".."
    "test..png",             # contains ".."
    "a..b",                  # embedded ".."
    "some..path..file.png",  # multiple ".."
])
def test_charts_rejects_path_traversal_double_dot(client, bad_filename):
    resp = client.get(f"/charts/{bad_filename}")
    assert resp.status_code == 400, (
        f"Expected 400 for {bad_filename!r}, got {resp.status_code}"
    )


def test_charts_returns_404_for_nonexistent_valid_filename(client):
    resp = client.get("/charts/nonexistent_chart_abc123.png")
    assert resp.status_code == 404


def test_charts_serves_file(client, tmp_path):
    # Create a minimal PNG in the charts dir
    chart_dir = Path("output/charts")
    chart_dir.mkdir(parents=True, exist_ok=True)
    chart_file = chart_dir / "test_chart_serve.png"
    chart_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    try:
        resp = client.get("/charts/test_chart_serve.png")
        assert resp.status_code == 200
        assert "image" in resp.headers.get("content-type", "")
    finally:
        chart_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# POST /datasources
# ---------------------------------------------------------------------------

def test_create_datasource_success(client_with_db):
    client, mock_session = client_with_db

    async def _refresh(obj):
        obj.created_at = FIXED_DT

    mock_session.refresh.side_effect = _refresh

    body = {
        "name": "My SQLite DB",
        "type": "sqlite",
        "connection_secret_ref": "projects/p/secrets/s",
        "default_schema": None,
        "row_limit": 50,
    }
    resp = client.post("/datasources", json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My SQLite DB"
    assert data["type"] == "sqlite"
    assert data["row_limit"] == 50
    assert "id" in data
    assert "created_at" in data


def test_create_datasource_requires_auth(client):
    app.dependency_overrides.clear()
    resp = client.post("/datasources", json={
        "name": "x", "type": "sqlite",
        "connection_secret_ref": "ref",
    })
    assert resp.status_code in (401, 403)
    app.dependency_overrides[get_current_user] = lambda: FAKE_TOKEN


# ---------------------------------------------------------------------------
# GET /datasources
# ---------------------------------------------------------------------------

def test_list_datasources_empty(client_with_db):
    client, mock_session = client_with_db
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    resp = client.get("/datasources")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_datasources_returns_results(client_with_db):
    client, mock_session = client_with_db
    ds1 = _make_mock_ds(name="db-one")
    ds2 = _make_mock_ds(name="db-two")

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [ds1, ds2]
    mock_session.execute = AsyncMock(return_value=mock_result)

    resp = client.get("/datasources")
    assert resp.status_code == 200
    names = [d["name"] for d in resp.json()]
    assert "db-one" in names
    assert "db-two" in names


def test_list_datasources_requires_auth(client):
    app.dependency_overrides.clear()
    resp = client.get("/datasources")
    assert resp.status_code in (401, 403)
    app.dependency_overrides[get_current_user] = lambda: FAKE_TOKEN


# ---------------------------------------------------------------------------
# DELETE /datasources/{id}
# ---------------------------------------------------------------------------

def test_delete_datasource_success(client_with_db):
    client, mock_session = client_with_db
    ds = _make_mock_ds()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = ds
    mock_session.execute = AsyncMock(return_value=mock_result)

    resp = client.delete(f"/datasources/{ds.id}")
    assert resp.status_code == 204


def test_delete_datasource_not_found(client_with_db):
    client, mock_session = client_with_db

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    resp = client.delete(f"/datasources/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_datasource_invalid_uuid(client_with_db):
    client, _ = client_with_db
    resp = client.delete("/datasources/not-a-valid-uuid")
    assert resp.status_code == 400
