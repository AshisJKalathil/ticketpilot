"""Run every tool call in a sandbox with least-privilege access.

Two isolation levels, chosen per tool:

* ``inproc`` - trusted, pure-database tools. Still scope-checked and
  time-limited.
* ``process`` - tools that touch third-party content or the network (the
  browser fallback). They run in a fresh Python subprocess with:
    - a scrubbed environment: no API keys, cloud credentials or tokens;
    - a throwaway HOME/TMPDIR as the working directory;
    - CPU-time, file-size and open-file limits set by the worker before it
      reads any input;
    - an egress allowlist enforced by the HTTP client / browser router;
    - a hard wall-clock timeout (the process is killed).

For production, run ``process`` tools in a container or microVM (gVisor,
Firecracker, nsjail) with no network except the allowlist; this module is the
application-level layer of that defence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from ticketpilot.retry import TransientError
from ticketpilot.tools.base import PermissionDenied, ToolContext, ToolError, ToolSpec

SRC_DIR = Path(__file__).resolve().parents[1]
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool")

_ERRORS: dict[str, type[Exception]] = {
    "TransientError": TransientError, "ToolError": ToolError, "PermissionDenied": PermissionDenied,
    "TimeoutError": TimeoutError,
}


def check_scopes(spec: ToolSpec, ctx: ToolContext) -> None:
    missing = spec.scopes - ctx.scopes
    if missing:
        raise PermissionDenied(f"tool '{spec.name}' needs scopes {sorted(missing)} which this run was not granted")


def _playwright_browsers_path() -> str | None:
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    default = Path.home() / ("Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright")
    return str(default) if default.exists() else None


def scrubbed_env(home: str) -> dict[str, str]:
    env = {"PATH": os.defpath, "PYTHONPATH": str(SRC_DIR), "HOME": home, "TMPDIR": home, "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    browsers = _playwright_browsers_path()
    if browsers:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    return env


class Sandbox:
    def run(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        check_scopes(spec, ctx)
        if spec.isolation == "process":
            return self._subprocess(spec, args, ctx)
        future = _pool.submit(spec.handler, ctx, spec.input_model.model_validate(args))
        try:
            return future.result(timeout=spec.timeout_s)
        except FutureTimeout as exc:
            raise TimeoutError(f"{spec.name} exceeded {spec.timeout_s}s") from exc

    def _subprocess(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        payload = json.dumps({"tool": spec.name, "args": args, "ctx": ctx.to_wire(), "timeout_s": spec.timeout_s})
        with tempfile.TemporaryDirectory(prefix="tp-sbx-") as home:
            try:
                proc = subprocess.run(
                    [sys.executable, "-I", "-m", "ticketpilot.sandbox_worker"],
                    input=payload, capture_output=True, text=True, cwd=home, env=scrubbed_env(home),
                    timeout=spec.timeout_s + 5,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"{spec.name} sandbox killed after {spec.timeout_s + 5}s") from exc
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        if not lines:
            raise TransientError(f"{spec.name} sandbox crashed (exit {proc.returncode}): {proc.stderr[-400:]}")
        result = json.loads(lines[-1])
        if result.get("ok"):
            return result["output"]
        exc_type = _ERRORS.get(result.get("error_type"), ToolError)
        if exc_type is TransientError:
            raise TransientError(result["message"], retry_after=result.get("retry_after"))
        raise exc_type(result["message"])
