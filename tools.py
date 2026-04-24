"""
Agent tools
-----------
run_python_code  — executes matplotlib/pandas code inside a Docker sandbox and
                   saves any generated figure to output/charts/.  Used by the
                   analyst agent.
"""

import json
import os
import socket
import uuid
from pathlib import Path

from agents import function_tool
from dotenv import load_dotenv

load_dotenv()

CHARTS_DIR = Path(os.getenv("CHARTS_DIR", "output/charts"))
CHARTS_DIR.mkdir(parents=True, exist_ok=True)

SANDBOX_IMAGE = os.getenv("DOCKER_SANDBOX_IMAGE", "ada-sandbox:latest")


def _get_docker_client():
    try:
        import docker  # noqa: PLC0415
        client = docker.from_env()
        client.ping()
        return client
    except Exception as exc:
        raise RuntimeError(
            f"Docker is not available — ensure the Docker daemon is running: {exc}"
        ) from exc


def _execute_python_code(code: str) -> str:
    """Core implementation — importable for testing without the FunctionTool wrapper."""
    client = _get_docker_client()
    chart_id = uuid.uuid4().hex[:10]
    container_chart = f"/tmp/chart_{chart_id}.png"
    host_chart = CHARTS_DIR / f"chart_{chart_id}.png"

    envelope = json.dumps({"code": code, "output_path": container_chart})
    volumes = {str(CHARTS_DIR.resolve()): {"bind": "/tmp", "mode": "rw"}}

    container = client.containers.create(
        image=SANDBOX_IMAGE,
        stdin_open=True,
        network_disabled=True,
        mem_limit="512m",
        nano_cpus=int(1e9),
        read_only=True,
        volumes=volumes,
    )
    try:
        sock = container.attach_socket(params={"stdin": 1, "stream": 1})
        container.start()
        data = envelope.encode()
        # SocketIO (Linux/Mac) wraps the real socket in ._sock;
        # NpipeSocket (Windows Docker Desktop) IS the socket — use it directly.
        raw = getattr(sock, "_sock", sock)
        raw.sendall(data)
        raw.shutdown(socket.SHUT_WR)
        sock.close()
        container.wait(timeout=30)
        output = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace").strip()
        return output.replace(container_chart, str(host_chart)) or "(no output)"
    except Exception as exc:
        return f"Sandbox error: {exc}"
    finally:
        container.remove(force=True)


@function_tool
def run_python_code(code: str) -> str:
    """
    Execute Python code (matplotlib / pandas) for data analysis and chart generation.

    The variable `_CHART_PATH` is pre-defined — pass it to plt.savefig() to persist the chart.
    Results are capped at 30 seconds of execution time.

    Returns stdout + stderr from the execution, including a 'CHART_SAVED:<path>' line
    if a chart was produced.
    """
    return _execute_python_code(code)
