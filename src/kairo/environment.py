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


class Environment:
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
                return ActionResult(
                    action.id, executed=False, error=f"unknown action kind: {action.kind}"
                )

    def _run_process(self, action: Action) -> ActionResult:
        argv = action.params.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            return ActionResult(
                action.id, executed=False, error="process.run requires 'argv': list[str]"
            )
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                cwd=action.params.get("cwd"),
                timeout=action.params.get("timeout"),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ActionResult(action.id, executed=False, error=str(exc))
        return ActionResult(
            action.id,
            executed=True,
            output={"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
        )
