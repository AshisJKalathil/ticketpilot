"""Entry point for process-isolated tool calls (see ``sandbox.py``).

Reads one JSON request on stdin, applies resource limits *before* touching
any input, runs the tool and prints one JSON result line on stdout.
"""

from __future__ import annotations

import json
import resource
import sys


def _limit(kind: int, value: int) -> None:
    try:
        resource.setrlimit(kind, (value, value))  # hard == soft: cannot be raised again
    except (ValueError, OSError):
        pass  # not supported on this platform


def main() -> None:
    request = json.loads(sys.stdin.read())
    timeout = int(request.get("timeout_s", 10))
    _limit(resource.RLIMIT_CPU, max(5, timeout * 2))
    _limit(resource.RLIMIT_FSIZE, 50 * 1024 * 1024)
    _limit(resource.RLIMIT_NOFILE, 1024)

    from ticketpilot.retry import TransientError
    from ticketpilot.tools.base import ToolContext
    from ticketpilot.tools.catalog import CATALOG

    try:
        spec = CATALOG[request["tool"]]
        ctx = ToolContext.from_wire(request["ctx"])
        output = spec.handler(ctx, spec.input_model.model_validate(request["args"]))
        print(json.dumps({"ok": True, "output": output}))
    except TransientError as exc:
        print(json.dumps({"ok": False, "error_type": "TransientError", "message": str(exc),
                          "retry_after": exc.retry_after}))
    except Exception as exc:  # report every failure as data, never a traceback
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "message": str(exc)[:1000]}))


if __name__ == "__main__":
    main()
