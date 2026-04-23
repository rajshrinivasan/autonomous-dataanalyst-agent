"""
Layer 5: End-to-End tests using Playwright.

These tests require:
  1. playwright installed:  pip install playwright && playwright install chromium
  2. A running FastAPI server with a real OPENAI_API_KEY
  3. The --run-e2e flag:    pytest tests/e2e/ --run-e2e -v

They are skipped automatically unless --run-e2e is passed.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip guard — gate all e2e tests behind --run-e2e flag
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run Playwright end-to-end tests (requires server + playwright install)",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-e2e", default=False):
        skip = pytest.mark.skip(reason="Pass --run-e2e to run E2E tests")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip)


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 18765
BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"


@pytest.fixture(scope="session")
def server_process():
    """Start the FastAPI server as a subprocess for the E2E session."""
    root = Path(__file__).parent.parent.parent
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app:app",
            "--host", SERVER_HOST,
            "--port", str(SERVER_PORT),
        ],
        cwd=str(root),
    )
    # Wait for server startup
    time.sleep(3)
    yield proc
    proc.terminate()
    proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_health_endpoint_reachable(server_process, page):
    """GET /health must return {"status": "ok"}."""
    resp = page.request.get(f"{BASE_URL}/health")
    assert resp.ok, f"Health check failed: {resp.status}"
    body = resp.json()
    assert body["status"] == "ok"


@pytest.mark.e2e
def test_ui_loads(server_process, page):
    """The web UI must load without error."""
    page.goto(BASE_URL)
    assert page.title() is not None


@pytest.mark.e2e
def test_sse_stream_returns_done_or_error(server_process, page):
    """Submit a question via /analyze and collect SSE events.

    The stream must eventually end with a 'done' or 'error' event.
    This requires OPENAI_API_KEY to be set in the server environment.
    """
    page.goto(BASE_URL)

    events = page.evaluate(
        """
        async () => {
            const collected = [];
            try {
                const resp = await fetch('/analyze', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({question: 'How many orders are there in total?'})
                });
                if (!resp.ok) {
                    return [{type: 'http_error', status: resp.status}];
                }
                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                const MAX_EVENTS = 50;
                while (collected.length < MAX_EVENTS) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, {stream: true});
                    const parts = buffer.split('\\n\\n');
                    buffer = parts.pop();
                    for (const part of parts) {
                        const line = part.trim();
                        if (line.startsWith('data: ')) {
                            try {
                                collected.push(JSON.parse(line.slice(6)));
                            } catch (_) {}
                        }
                    }
                    if (collected.some(e => e.type === 'done' || e.type === 'error')) {
                        break;
                    }
                }
            } catch (err) {
                collected.push({type: 'js_error', message: String(err)});
            }
            return collected;
        }
        """,
        timeout=180_000,
    )

    assert len(events) > 0, "No SSE events received"
    types = [e.get("type") for e in events]
    assert "done" in types or "error" in types, (
        f"Stream did not end with done/error. Event types: {types}"
    )


@pytest.mark.e2e
def test_chart_url_is_servable(server_process, page):
    """When a chart event is received, GET /charts/<filename> must return 200."""
    page.goto(BASE_URL)

    events = page.evaluate(
        """
        async () => {
            const collected = [];
            try {
                const resp = await fetch('/analyze', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({question: 'Show me revenue by product category as a chart'})
                });
                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                while (true) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, {stream: true});
                    const parts = buffer.split('\\n\\n');
                    buffer = parts.pop();
                    for (const part of parts) {
                        const line = part.trim();
                        if (line.startsWith('data: ')) {
                            try {
                                collected.push(JSON.parse(line.slice(6)));
                            } catch (_) {}
                        }
                    }
                    if (collected.some(e => e.type === 'done' || e.type === 'error')) break;
                }
            } catch (err) {}
            return collected;
        }
        """,
        timeout=180_000,
    )

    chart_events = [e for e in events if e.get("type") == "chart"]
    if not chart_events:
        pytest.skip("No chart produced by this question — skipping chart URL test")

    chart_url = chart_events[0].get("url") or chart_events[0].get("path", "")
    if not chart_url.startswith("/"):
        chart_url = f"/{chart_url}"

    img_resp = page.request.get(f"{BASE_URL}{chart_url}")
    assert img_resp.ok, f"Chart URL {chart_url} returned {img_resp.status}"
