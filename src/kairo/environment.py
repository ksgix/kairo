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
from kairo.redact import protected_files, scrubbed_env

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
        elif refused := _names_credential_file(argv, cwd):
            error = f"process.run refused: it names a provider credential file ({refused})"
        if error:
            return ActionResult(action.id, executed=False, error=error, failure="invalid_params")
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                env=scrubbed_env(),  # actions never get cognition providers' credentials
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


def _names_credential_file(argv: list[str], cwd: str | None) -> str | None:
    """A narrow guard, not a sandbox: refuse an action that names a known
    provider credential file (its full path or its file name). A determined
    command can still reach it indirectly; the file's secret values are then
    still redacted from output (kairo.redact), but not if transformed (e.g.
    encoded). Real isolation needs actions to run as a different OS user."""
    texts = list(argv) + ([cwd] if cwd else [])
    for path in protected_files():
        for needle in {path, os.path.basename(path), path.replace(os.path.expanduser("~"), "~", 1)}:
            if needle and any(needle in text for text in texts):
                return os.path.basename(path)
    return None
