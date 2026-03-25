"""
Agent tools
-----------
run_python_code  — executes matplotlib/pandas code in a subprocess and saves
                   any generated figure to output/charts/.  Used by the analyst agent.
"""

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from agents import function_tool
from dotenv import load_dotenv

load_dotenv()

CHARTS_DIR = Path(os.getenv("CHARTS_DIR", "output/charts"))
CHARTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Preamble injected before every code block
# ---------------------------------------------------------------------------
_PREAMBLE = """\
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import json, sys, os

# Pre-defined by the tool runner — use this path to save your figure.
_CHART_PATH = {chart_path!r}
"""

# Postamble: auto-save any figure the model forgot to save explicitly.
_POSTAMBLE = """\

# ---- auto-save (tool runner) ----
_open_figs = plt.get_fignums()
if _open_figs and not os.path.exists(_CHART_PATH):
    plt.savefig(_CHART_PATH, dpi=150, bbox_inches='tight')
    plt.close('all')

if os.path.exists(_CHART_PATH):
    print(f"CHART_SAVED:{_CHART_PATH}")
"""


def _execute_python_code(code: str) -> str:
    """Core implementation — importable for testing without the FunctionTool wrapper."""
    chart_id   = uuid.uuid4().hex[:10]
    chart_path = CHARTS_DIR / f"chart_{chart_id}.png"

    full_code = _PREAMBLE.format(chart_path=str(chart_path)) + "\n" + code + "\n" + _POSTAMBLE

    # Write to a temp file so tracebacks show line numbers
    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(full_code)
        tmp_path = tmp.name

    try:
        proc = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).parent),
        )
        output = proc.stdout
        if proc.stderr:
            output += "\n[stderr]\n" + proc.stderr
        if proc.returncode != 0:
            output += f"\n[exit code {proc.returncode}]"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: code execution timed out after 30 seconds."
    finally:
        Path(tmp_path).unlink(missing_ok=True)


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
