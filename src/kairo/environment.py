"""Environment: the real Linux system Kairo lives in.

``observe`` describes the host; ``execute`` carries out structured actions.
There is intentionally no permission layer: the only reason an action is
refused is that no executor understands its kind yet.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

from kairo.actions import Action, ActionResult
from kairo.implementations import (
    PREFIX, ImplementationError, Implementations, Operation, Package, check_action_params,
    check_params,
)
from kairo.redact import env_owner, protected_files, scrubbed_env
from kairo.verification import Outcome, Verification

DEFAULT_TIMEOUT = 300.0  # seconds; an action must never block the runtime forever
MAX_CAPTURE = 1_000_000  # bytes of stdout / stderr read back (records keep far less)

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
    def __init__(self, implementations: Implementations | None = None) -> None:
        self.implementations = implementations

    def actions(self) -> dict[str, dict[str, Any]]:
        """The structured actions this environment can execute: the core ones,
        plus the tools and checks of available implementations."""
        impls = getattr(self, "implementations", None)
        if impls is None:
            return ACTIONS
        return {**ACTIONS, **impls.actions(ACTIONS)}

    def implementations_view(self) -> list[dict[str, Any]]:
        impls = getattr(self, "implementations", None)
        return impls.view() if impls is not None else []

    def provenance(self, action: Action) -> dict[str, str] | None:
        """Which implementation content an action would run, if any."""
        try:
            package, _ = self._resolve(action.kind)
        except ImplementationError:
            return None
        return {"id": package.id, "digest": package.digest}

    def verifier(self, kind: str) -> Any:
        """The verifier this environment supplies for one of its action kinds:
        a tool's declared verify command, or a check's own verdict."""
        try:
            package, tool = self._resolve(kind)
        except ImplementationError:
            return None
        if tool is None:
            return _CheckVerdict()
        return _VerifyCommand(package, tool) if tool.verify else None

    def verified_kinds(self) -> set[str]:
        return {kind for kind in self.actions() if self.verifier(kind) is not None}

    def _resolve(self, kind: str) -> tuple[Package, Operation | None]:
        impls = getattr(self, "implementations", None)
        if impls is None or not kind.startswith(PREFIX):
            raise ImplementationError(f"not an implementation action: {kind!r}")
        return impls.resolve(kind)

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
            case kind if kind.startswith(PREFIX):
                return self._run_implementation(action)
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
        # Actions never get providers' or implementations' secrets.
        return _execute(action.id, argv, cwd=cwd, env=scrubbed_env(), timeout=timeout)

    def _run_implementation(self, action: Action) -> ActionResult:
        """An implementation tool or check: the same contained execution, from the
        package directory, with the parameters as JSON on stdin (never on the
        command line) and only this package's own secrets."""
        try:
            package, tool = self._resolve(action.kind)
            if tool is None:  # the check action: run the declared check it names
                check_params(check_action_params(package), action.params)
                tool = next(c for c in package.checks if c.name == action.params["name"])
            else:
                check_params(tool.params, action.params)
        except ImplementationError as exc:  # refused before anything runs
            return ActionResult(action.id, executed=False, error=str(exc), failure="invalid_params")
        provenance = {"id": package.id, "digest": package.digest}
        result = _execute(action.id, _argv(package, tool.run), cwd=str(package.path),
                          env=_package_env(package), timeout=tool.timeout,
                          stdin=json.dumps(action.params).encode())
        return ActionResult(result.action_id, result.executed, result.output, result.error,
                            result.failure, implementation=provenance)


def _execute(action_id: str, argv: list[str], *, cwd: str | None, env: dict[str, str],
             timeout: float, stdin: bytes | None = None) -> ActionResult:
    try:
        returncode, stdout, stderr = run_contained(argv, cwd=cwd, env=env, timeout=timeout,
                                                   stdin=stdin)
    except FileNotFoundError as exc:  # the program or cwd did not exist
        return ActionResult(action_id, executed=False, error=str(exc), failure="not_found")
    except PermissionError as exc:
        return ActionResult(action_id, executed=False, error=str(exc), failure="permission_denied")
    except subprocess.TimeoutExpired as exc:
        return ActionResult(action_id, executed=False, error=str(exc), failure="timed_out")
    except (OSError, ValueError) as exc:
        return ActionResult(action_id, executed=False, error=str(exc), failure="os_error")
    return ActionResult(action_id, executed=True,
                        output={"returncode": returncode, "stdout": stdout, "stderr": stderr})


def _argv(package: Package, run: tuple[str, ...]) -> list[str]:
    """Package-relative program paths made absolute; bare commands left to PATH."""
    first = run[0]
    return [str(package.path / first) if "/" in first else first, *run[1:]]


def _package_env(package: Package) -> dict[str, str]:
    """The scrubbed environment plus this package's own declared secrets (those
    it actually owns: never a provider's or another package's)."""
    owner = f"implementation:{package.id}"
    return scrubbed_env(keep=[n for n in package.secrets if env_owner(n) == owner])


class _CheckVerdict:
    """A check's exit code is its verdict: 0 passed, anything else failed."""

    def verify(self, action: Action, result: ActionResult) -> Verification:
        code = (result.output or {}).get("returncode")
        return Verification(Outcome.SUCCESS if code == 0 else Outcome.FAILURE,
                            f"check exited {code}", {"returncode": code})


class _VerifyCommand:
    """A tool's declared verify command, run like the tool itself. It reads
    {"params", "result"} as JSON on stdin; exit 0 = success, 1 = failure,
    anything else (or failing to run) = unverifiable."""

    def __init__(self, package: Package, tool: Operation) -> None:
        self.package, self.tool = package, tool

    def verify(self, action: Action, result: ActionResult) -> Verification:
        payload = json.dumps({"params": action.params, "result": result.output}).encode()
        try:
            code, _, stderr = run_contained(_argv(self.package, self.tool.verify or ()),
                                            cwd=str(self.package.path),
                                            env=_package_env(self.package),
                                            timeout=self.tool.timeout, stdin=payload)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return Verification(Outcome.UNVERIFIABLE, f"verify could not run: {exc}")
        outcome = {0: Outcome.SUCCESS, 1: Outcome.FAILURE}.get(code, Outcome.UNVERIFIABLE)
        return Verification(outcome, f"verify exited {code}",
                            {"verify_returncode": code, "verify_stderr": stderr[-300:]})


def run_contained(argv: list[str], *, cwd: str | None, env: dict[str, str], timeout: float,
                  stdin: bytes | None = None) -> tuple[int, str, str]:
    """Run one program in its own process group, and kill that whole group when
    the program exits or its timeout passes, so nothing it started outlives the
    action. Output goes to temporary files (not pipes), so a lingering child
    cannot hold the action open. stdin is closed unless ``stdin`` is given.

    Containment is by process group only: a descendant that deliberately leaves
    the group (setsid, double fork) can escape. Stronger containment (cgroups,
    a systemd scope, a separate OS user) belongs to production hardening."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, \
            tempfile.TemporaryFile() as inp:
        if stdin is not None:
            inp.write(stdin)
            inp.seek(0)
        proc = subprocess.Popen(argv, stdin=inp if stdin is not None else subprocess.DEVNULL,
                                stdout=out, stderr=err, cwd=cwd, env=env,
                                start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except BaseException:  # timeout, or the runtime itself being interrupted
            _kill_group(proc.pid)
            proc.kill()  # the child itself, even if its group could not be signalled
            proc.wait()
            raise
        _kill_group(proc.pid)  # anything it left running in the background
        return proc.returncode, _read(out), _read(err)


GROUP_EXIT_WAIT = 1.0  # seconds to wait for a killed group to be gone


def _kill_group(pgid: int) -> None:
    """SIGKILL the group, then wait (briefly, bounded) until it is gone, so the
    action does not return while something it started is still dying."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return  # nothing left in the group
    deadline = time.monotonic() + GROUP_EXIT_WAIT
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.01)


def _read(handle: Any) -> str:
    handle.seek(0)
    return handle.read(MAX_CAPTURE).decode("utf-8", errors="replace")


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
