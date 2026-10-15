"""Run generated code in a subprocess with a scrubbed environment and a timeout.

This isolates the orchestrator process from generated code (crashes, hangs, global
state). It is NOT a security sandbox: the child still has the same user and
filesystem access. Run the whole orchestrator in a disposable container if agents
are not trusted.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SANDBOX_INI = """[pytest]
addopts = -p no:cacheprovider
filterwarnings = ignore::DeprecationWarning
pythonpath = .
"""


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False


def _env(pythonpath: list[Path], extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join(str(p) for p in pythonpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "LANG": "C.UTF-8",
    }
    env.update(extra or {})
    return env


def run_python(
    args: list[str],
    cwd: Path,
    pythonpath: list[Path],
    timeout: float = 120.0,
    extra_env: dict[str, str] | None = None,
) -> RunResult:
    start = time.monotonic()
    try:
        p = subprocess.run(
            [sys.executable, *args],
            cwd=str(cwd),
            env=_env(pythonpath, extra_env),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return RunResult(p.returncode, p.stdout, p.stderr, time.monotonic() - start)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return RunResult(124, out, err + f"\ntimed out after {timeout}s", time.monotonic() - start, True)


def ensure_sandbox_ini(run_dir: Path) -> Path:
    path = run_dir / "pytest-sandbox.ini"
    if not path.exists():
        path.write_text(SANDBOX_INI)
    return path
