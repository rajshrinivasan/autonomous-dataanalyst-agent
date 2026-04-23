"""
Sandbox runner — reads a JSON envelope from stdin, executes chart code in a
restricted namespace, and prints CHART_SAVED:<path> on success.

Envelope: {"code": "<python source>", "output_path": "/tmp/chart_X.png"}
"""

import builtins as _builtins_module
import json
import os
import os.path as _os_path
import sys

os.environ.setdefault("MPLCONFIGDIR", "/tmp")

BLOCKED_MODULES = frozenset({
    "os", "sys", "subprocess", "socket", "importlib",
    "shutil", "pathlib", "ctypes", "cffi", "pty",
    "code", "codeop", "pdb", "pickle", "shelve",
    "multiprocessing", "threading", "signal",
})

_original_import = _builtins_module.__import__


def _safe_import(name, *args, **kwargs):
    if name.split(".")[0] in BLOCKED_MODULES:
        raise ImportError(f"import of '{name}' is blocked in the sandbox")
    return _original_import(name, *args, **kwargs)


def _build_namespace(output_path: str, plt, pd, np, matplotlib) -> dict:
    safe_builtins = vars(_builtins_module).copy()
    safe_builtins["__import__"] = _safe_import
    safe_builtins.pop("open", None)
    safe_builtins.pop("__loader__", None)
    safe_builtins.pop("__spec__", None)
    return {
        "__builtins__": safe_builtins,
        "matplotlib": matplotlib,
        "plt": plt,
        "pd": pd,
        "np": np,
        "json": json,
        "_CHART_PATH": output_path,
        "_os_path": _os_path,  # pre-injected for postamble; not user-accessible by convention
    }


def _postamble(output_path: str) -> str:
    p = repr(output_path)
    return (
        "\n_figs = plt.get_fignums()\n"
        "if _figs and not _os_path.exists(" + p + "):\n"
        "    plt.savefig(" + p + ", dpi=150, bbox_inches='tight')\n"
        "    plt.close('all')\n"
        "if _os_path.exists(" + p + "):\n"
        "    print('CHART_SAVED:' + " + p + ")\n"
    )


def main() -> None:
    try:
        envelope = json.loads(sys.stdin.read())
    except Exception as exc:
        print(f"Error: invalid envelope: {exc}", file=sys.stderr)
        sys.exit(1)

    code = envelope.get("code", "")
    output_path = envelope.get("output_path", "")

    if not output_path.startswith("/tmp/"):
        print("Error: output_path must start with /tmp/", file=sys.stderr)
        sys.exit(1)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
    import numpy as np

    namespace = _build_namespace(output_path, plt, pd, np, matplotlib)
    full_code = code + _postamble(output_path)

    try:
        exec(compile(full_code, "<sandbox>", "exec"), namespace)  # noqa: S102
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
