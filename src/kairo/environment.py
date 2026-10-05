"""Environment: the real Linux system Kairo lives in.

``observe`` describes the host; ``execute`` carries out structured actions.
There is intentionally no permission layer: the only reason an action is
refused is that no executor understands its kind yet.
"""

from __future__ import annotations

import dataclasses
import getpass
import json
import os
import platform
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from kairo import deploy
from kairo.actions import NOT_PERFORMED_EXIT, Action, ActionResult
from kairo.implementations import (
    PREFIX, ImplementationError, Implementations, Operation, Package, check_action_params,
    check_params,
)
from kairo.redact import env_owner, head_tail, protected_files, redact, scrubbed_env
from kairo.verification import Outcome, Verification

DEFAULT_TIMEOUT = 300.0  # seconds; an action must never block the runtime forever
MAX_CAPTURE = 1_000_000  # bytes of stdout / stderr kept per stream: its head and its tail
OUTPUT_LIMIT = 8_000_000  # bytes a program may write to one stream before it is stopped
PUMP_JOIN = 2.0  # seconds to wait for output still held open by an escaped descendant

# Probes: fixed commands the operator configures, which the runtime itself runs at
# every observation (no cognition involved). They are Kairo's senses: what a probe
# prints is part of the observation, so a change is visible without any action.
PROBE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_PROBES = 8
PROBE_TIMEOUT = 10.0  # seconds one probe may run
PROBE_OUTPUT = 400    # characters kept of what a probe printed (beginning and end)
PROBE_REUSE = 5.0     # seconds a result is reused: operator reads observe too

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


def parse_probe(spec: str) -> tuple[str, list[str]]:
    """One ``NAME=COMMAND`` probe setting as (name, argv). The command is split
    like a shell would split it, but it is run directly, without a shell."""
    name, sep, command = spec.partition("=")
    if not sep or not PROBE_NAME.match(name):
        raise ValueError(f"probe {spec!r}: expected NAME=COMMAND, NAME matching "
                         f"{PROBE_NAME.pattern}")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"probe {name!r}: {exc}") from None
    if not argv:
        raise ValueError(f"probe {name!r}: no command")
    return name, argv


class Environment:
    def __init__(self, implementations: Implementations | None = None,
                 deployment: deploy.Deployment | None = None,
                 probes: dict[str, list[str]] | None = None) -> None:
        self.implementations = implementations
        # Configured only for a supervised release layout (see kairo.deploy).
        self.deployment = deployment
        probes = dict(probes or {})
        if len(probes) > MAX_PROBES:
            raise ValueError(f"at most {MAX_PROBES} probes")
        for name, argv in probes.items():
            if not PROBE_NAME.match(name) or not argv or not all(
                    isinstance(a, str) and a for a in argv):
                raise ValueError(f"probe {name!r}: a name matching {PROBE_NAME.pattern} and "
                                 "a non-empty command")
        self.probes = probes
        self._probe_lock = threading.Lock()
        self._probed: tuple[float, dict[str, Any]] | None = None  # (monotonic time, results)

    def actions(self) -> dict[str, dict[str, Any]]:
        """The structured actions this environment can execute: the core ones
        (runtime.deploy only when deployment is configured), plus the tools and
        checks of available implementations."""
        core = {**ACTIONS, deploy.KIND: deploy.ACTION} \
            if getattr(self, "deployment", None) is not None else ACTIONS
        impls = getattr(self, "implementations", None)
        if impls is None:
            return core
        return {**core, **impls.actions(core)}

    def code_facts(self) -> dict[str, Any]:
        deployment = getattr(self, "deployment", None)
        return deployment.facts() if deployment is not None else {}

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

    def tool_profile(self, kind: str) -> dict[str, str | None] | None:
        """What an implementation tool declares about its effects outside this host
        (``effects``, ``idempotency``); None for anything that is not a tool."""
        try:
            _, tool = self._resolve(kind)
        except ImplementationError:
            return None
        if tool is None:
            return None
        return {"effects": tool.effects, "idempotency": tool.idempotency}

    def verifier(self, kind: str) -> Any:
        """The verifier this environment supplies for one of its action kinds:
        a tool's declared verify command, a check's own verdict, or, for a
        deployment, the wait for the restarted runtime's confirmation."""
        if kind == deploy.KIND and getattr(self, "deployment", None) is not None:
            return deploy.AwaitSuccessor()
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
            **{f"probe.{name}": result for name, result in self._probe().items()},
        }

    def _probe(self) -> dict[str, Any]:
        """Run every configured probe (a result younger than PROBE_REUSE is
        reused). A probe that cannot run is itself an observation, never an error."""
        probes = getattr(self, "probes", None)
        if not probes:
            return {}
        with self._probe_lock:
            now = time.monotonic()
            if self._probed is not None and now - self._probed[0] < PROBE_REUSE:
                return self._probed[1]
            results = {name: _run_probe(argv) for name, argv in sorted(probes.items())}
            self._probed = (time.monotonic(), results)
            return results

    def execute(self, action: Action) -> ActionResult:
        match action.kind:
            case "process.run":
                return self._run_process(action)
            case deploy.KIND if getattr(self, "deployment", None) is not None:
                return self.deployment.deploy(action)  # type: ignore[union-attr]
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
                          env=_operation_env(package, action), timeout=tool.timeout,
                          stdin=json.dumps(action.params).encode())
        result = dataclasses.replace(result, implementation=provenance)
        return _external_outcome(result) if tool.effects == "external" else result


def _run_probe(argv: list[str]) -> dict[str, Any]:
    """What one probe showed: its exit code and what it printed (stdout, or stderr
    when stdout is empty), redacted and bounded so that the same state always
    reads the same. ``exit`` is None when it did not exit by itself."""
    try:
        code, stdout, stderr = run_contained(argv, cwd=None, env=scrubbed_env(),
                                             timeout=PROBE_TIMEOUT)
    except FileNotFoundError:
        return {"exit": None, "failure": "not_found"}
    except PermissionError:
        return {"exit": None, "failure": "permission_denied"}
    except subprocess.TimeoutExpired:
        return {"exit": None, "failure": "timed_out"}
    except OutputLimitExceeded:
        return {"exit": None, "failure": "output_limit"}
    except (OSError, ValueError):
        return {"exit": None, "failure": "os_error"}
    text = stdout.strip() or stderr.strip()
    return {"exit": code, "output": head_tail(redact(text), PROBE_OUTPUT)}


def _execute(action_id: str, argv: list[str], *, cwd: str | None, env: dict[str, str],
             timeout: float, stdin: bytes | None = None) -> ActionResult:
    try:
        returncode, stdout, stderr = run_contained(argv, cwd=cwd, env=env, timeout=timeout,
                                                   stdin=stdin)
    except FileNotFoundError as exc:  # the program or cwd did not exist
        return ActionResult(action_id, executed=False, error=str(exc), failure="not_found")
    except PermissionError as exc:
        return ActionResult(action_id, executed=False, error=str(exc), failure="permission_denied")
    except subprocess.TimeoutExpired as exc:  # stopped: keep what it printed before
        return ActionResult(action_id, executed=False, error=str(exc), failure="timed_out",
                            output=_partial(exc.output, exc.stderr))
    except OutputLimitExceeded as exc:
        return ActionResult(action_id, executed=False, error=str(exc), failure="output_limit",
                            output=_partial(exc.stdout, exc.stderr))
    except (OSError, ValueError) as exc:
        return ActionResult(action_id, executed=False, error=str(exc), failure="os_error")
    return ActionResult(action_id, executed=True,
                        output={"returncode": returncode, "stdout": stdout, "stderr": stderr})


def _partial(stdout: Any, stderr: Any) -> dict[str, Any]:
    """What a stopped program had printed (it did not exit by itself)."""
    return {"returncode": None,
            "stdout": stdout if isinstance(stdout, str) else "",
            "stderr": stderr if isinstance(stderr, str) else ""}


def _external_outcome(result: ActionResult) -> ActionResult:
    """For a tool declaring external effects: what happened outside, as far as the
    runtime can tell from how the tool ended (never from what it printed).
    exit 0: performed; exit 3 or never started: not performed; anything else,
    including a timeout or a kill: unknown, and it did run."""
    if result.executed:
        code = result.output.get("returncode")
        outcome = ("performed" if code == 0 else
                   "not_performed" if code == NOT_PERFORMED_EXIT else "unknown")
        return dataclasses.replace(result, external_outcome=outcome)
    if result.failure in ("timed_out", "output_limit"):  # it ran and was stopped
        return dataclasses.replace(result, executed=True, external_outcome="unknown")
    return dataclasses.replace(result, external_outcome="not_performed")  # never started


def _operation_env(package: Package, action: Action) -> dict[str, str]:
    """A tool's (or its verify command's) environment: the package's own, plus the
    identity of this attempt and of the external operation it carries out."""
    return {**_package_env(package), "KAIRO_ACTION_ID": action.id,
            "KAIRO_OPERATION_KEY": action.operation_key or action.id}


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
                                            env=_operation_env(self.package, action),
                                            timeout=self.tool.timeout, stdin=payload)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return Verification(Outcome.UNVERIFIABLE, f"verify could not run: {exc}")
        outcome = {0: Outcome.SUCCESS, 1: Outcome.FAILURE}.get(code, Outcome.UNVERIFIABLE)
        return Verification(outcome, f"verify exited {code}",
                            {"verify_returncode": code, "verify_stderr": stderr[-300:]})


class OutputLimitExceeded(OSError):
    """A program wrote more than OUTPUT_LIMIT bytes to one stream and was stopped."""

    def __init__(self, stdout: str, stderr: str) -> None:
        super().__init__(f"stopped: wrote more than {OUTPUT_LIMIT} bytes of output")
        self.stdout, self.stderr = stdout, stderr


class _Capture:
    """Reads one output pipe to its end on a thread, keeping its first and last
    bytes (MAX_CAPTURE in all) and discarding the middle: memory is bounded and
    nothing is written to disk. Past OUTPUT_LIMIT bytes it calls ``on_limit``."""

    def __init__(self, fd: int, on_limit: Any) -> None:
        self.fd, self.on_limit = fd, on_limit
        self.head, self.tail = bytearray(), bytearray()
        self.total = 0
        self.exceeded = False
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        head_max = MAX_CAPTURE // 2
        tail_max = MAX_CAPTURE - head_max
        try:
            while chunk := os.read(self.fd, 65536):
                with self._lock:
                    self.total += len(chunk)
                    room = head_max - len(self.head)
                    if room > 0:
                        self.head += chunk[:room]
                        chunk = chunk[room:]
                    if chunk:
                        self.tail += chunk
                        if len(self.tail) > tail_max:
                            del self.tail[:len(self.tail) - tail_max]
                    exceeded = self.total > OUTPUT_LIMIT and not self.exceeded
                    if exceeded:
                        self.exceeded = True
                if exceeded:
                    self.on_limit()
        except OSError:
            pass
        finally:
            os.close(self.fd)

    def text(self) -> str:
        with self._lock:
            head, tail, total = bytes(self.head), bytes(self.tail), self.total
        omitted = total - len(head) - len(tail)
        if omitted <= 0:
            return (head + tail).decode("utf-8", errors="replace")
        return (head.decode("utf-8", errors="replace")
                + f"\n[truncated {omitted} bytes in the middle]\n"
                + tail.decode("utf-8", errors="replace"))


def run_contained(argv: list[str], *, cwd: str | None, env: dict[str, str], timeout: float,
                  stdin: bytes | None = None) -> tuple[int, str, str]:
    """Run one program in its own process group, and kill that whole group when
    the program exits or its timeout passes, so nothing it started outlives the
    action. stdin is closed unless ``stdin`` is given.

    Output is read from pipes as it is written: the first and last bytes are
    kept (MAX_CAPTURE per stream), the middle is discarded, and nothing goes to
    disk. A program writing more than OUTPUT_LIMIT bytes to a stream is stopped
    (OutputLimitExceeded). A descendant that escaped the group and still holds a
    pipe cannot keep the action open longer than PUMP_JOIN.

    Containment is by process group only: a descendant that deliberately leaves
    the group (setsid, double fork) can escape. Stronger containment (cgroups,
    a systemd scope, a separate OS user) belongs to production hardening."""
    with tempfile.TemporaryFile() as inp:  # stdin: the request, already bounded
        if stdin is not None:
            inp.write(stdin)
            inp.seek(0)
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        try:
            proc = subprocess.Popen(argv, stdin=inp if stdin is not None else subprocess.DEVNULL,
                                    stdout=out_w, stderr=err_w, cwd=cwd, env=env,
                                    start_new_session=True)
        except BaseException:
            for fd in (out_r, out_w, err_r, err_w):
                os.close(fd)
            raise
        os.close(out_w)
        os.close(err_w)

        def stop() -> None:  # from a capture thread: too much output
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        out, err = _Capture(out_r, stop), _Capture(err_r, stop)
        try:
            proc.wait(timeout=timeout)
        except BaseException as exc:  # timeout, or the runtime itself being interrupted
            _kill_group(proc.pid)
            proc.kill()  # the child itself, even if its group could not be signalled
            proc.wait()
            _drain(out, err)
            if isinstance(exc, subprocess.TimeoutExpired):
                exc.output, exc.stderr = out.text(), err.text()
            raise
        _kill_group(proc.pid)  # anything it left running in the background
        _drain(out, err)
        if out.exceeded or err.exceeded:
            raise OutputLimitExceeded(out.text(), err.text())
        return proc.returncode, out.text(), err.text()


def _drain(*captures: _Capture) -> None:
    """Let the capture threads reach the end of their pipes (bounded: a pipe held
    by an escaped descendant is left to its thread)."""
    deadline = time.monotonic() + PUMP_JOIN
    for capture in captures:
        capture.thread.join(max(deadline - time.monotonic(), 0))


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
