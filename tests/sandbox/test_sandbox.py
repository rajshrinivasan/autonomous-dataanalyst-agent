"""
Sandbox integration tests — require the ada-sandbox Docker image.
Build it first: make sandbox-build
"""

import pytest

from tools import _execute_python_code

# ---------------------------------------------------------------------------
# Skip entire module if Docker or the sandbox image is unavailable
# ---------------------------------------------------------------------------
try:
    import docker as _docker
    _client = _docker.from_env()
    _client.images.get("ada-sandbox:latest")
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE,
    reason="ada-sandbox:latest image not found — run: make sandbox-build",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_normal_chart_produces_chart_saved():
    code = """
import matplotlib.pyplot as plt
plt.figure()
plt.plot([1, 2, 3], [4, 5, 6])
plt.savefig(_CHART_PATH)
"""
    output = _execute_python_code(code)
    assert "CHART_SAVED:" in output


def test_os_system_does_not_crash_and_produces_no_chart():
    code = "import os; os.system('rm -rf /')"
    output = _execute_python_code(code)
    assert "CHART_SAVED:" not in output


def test_network_access_blocked():
    code = "import socket; socket.socket().connect(('8.8.8.8', 53))"
    output = _execute_python_code(code)
    assert "CHART_SAVED:" not in output


def test_timeout_returns_error():
    code = "import time; time.sleep(60)"
    output = _execute_python_code(code)
    assert "Sandbox error" in output or "timeout" in output.lower()
