"""Environment: the real Linux system Kairo lives in.

``observe`` describes the host; ``execute`` carries out structured actions.
There is intentionally no permission layer: the only reason an action is
refused is that no executor understands its kind yet.
"""

from __future__ import annotations

import getpass
import os
import platform
import socket
import subprocess
import sys
from typing import Any

from kairo.actions import Action, ActionResult

DEFAULT_TIMEOUT = 300.0  # seconds; an action must never block the runtime forever

# The action interface, described for cognition. Keys are Action.kind values;
# "params" is a JSON Schema for Action.params.
ACTIONS: dict[str, dict[str, Any]] = {
    "process.run": {
        "description": (
            "Run one program directly (no shell: no pipes, globbing, redirection or "
            "variable expansion unless you invoke a shell such as sh -c yourself). "
            "stdin is closed. Returns returncode, stdout and stderr. "
            f"Killed after 'timeout' seconds (default {DEFAULT_TIMEOUT:g})."
        ),
        "params": {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "cwd": {"type": "string"},
                "timeout": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
    },
}


class Environment:
    def actions(self) -> dict[str, dict[str, Any]]:
        """The structured actions this environment can execute."""
        return ACTIONS

    def observe(self) -> dict[str, Any]:
        return {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "user": getpass.getuser(),
            "uid": os.getuid(),
            "cwd": os.getcwd(),
            "python": sys.version.split()[0],
        }

    def execute(self, action: Action) -> ActionResult:
        match action.kind:
            case "process.run":
                return self._run_process(action)
            case _:
                return ActionResult(action.id, executed=False, failure="invalid_params",
                                    error=f"unknown action kind: {action.kind}")

    def _run_process(self, action: Action) -> ActionResult:
        params = action.params
        argv = params.get("argv")
        cwd = params.get("cwd")
        timeout = params.get("timeout", DEFAULT_TIMEOUT)
        error = None
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            error = "process.run requires 'argv': list[str]"
        elif cwd is not None and not isinstance(cwd, str):
            error = "process.run 'cwd' must be a string"
        elif isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            error = "process.run 'timeout' must be a positive number"
        elif unknown := set(params) - {"argv", "cwd", "timeout"}:
            error = f"process.run got unknown params: {sorted(unknown)}"
        if error:
            return ActionResult(action.id, executed=False, error=error, failure="invalid_params")
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                cwd=cwd,
                timeout=timeout,
            )
        except FileNotFoundError as exc:  # the program or cwd did not exist
            return ActionResult(action.id, executed=False, error=str(exc), failure="not_found")
        except PermissionError as exc:
            return ActionResult(action.id, executed=False, error=str(exc),
                                failure="permission_denied")
        except subprocess.TimeoutExpired as exc:
            return ActionResult(action.id, executed=False, error=str(exc), failure="timed_out")
        except (OSError, ValueError) as exc:
            return ActionResult(action.id, executed=False, error=str(exc), failure="os_error")
        return ActionResult(
            action.id,
            executed=True,
            output={"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
        )
