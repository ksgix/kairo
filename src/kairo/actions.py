"""Actions: the structured boundary between deciding and executing.

Cognition produces ``Action`` values; the environment executes them and
returns an ``ActionResult``. An action is data, never raw shell text.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

# Why an action could not be executed, as established by the runtime from the
# actual exception, never inferred from text or exit codes. Closed vocabulary.
FAILURE_KINDS = frozenset({
    "not_found",          # a required file or directory (the program, or cwd) did not exist
    "permission_denied",  # the OS refused access
    "timed_out",          # killed after its timeout
    "invalid_params",     # the runtime refused the request before running anything
    "os_error",           # any other OS-level error
    "executor_error",     # the runtime's own executor raised
})


@dataclass(frozen=True)
class Action:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # The ongoing work this action is an attempt at, if any. Set by the runtime
    # after it has validated the link; cognition only names the work it means.
    work_id: str | None = None


@dataclass(frozen=True)
class ActionResult:
    """Whether execution itself completed. Says nothing about the world state;
    that is what verification is for."""

    action_id: str
    executed: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # When not executed: one of FAILURE_KINDS. Set by the runtime only.
    failure: str | None = None


# Derived action states, grouped by what they establish.
SUCCEEDED = frozenset({"verified_successful", "executed_unverified"})
FAILED = frozenset({"failed_to_execute", "verified_failed", "exited_nonzero"})
# The runtime cannot tell whether these completed or what side effects occurred.
INDETERMINATE = frozenset({"interrupted", "in_progress"})


def _returncode(record: dict[str, Any]) -> Any:
    return ((record.get("result") or {}).get("output") or {}).get("returncode")


def action_state(record: dict[str, Any]) -> str:
    """The runtime's own verdict on a persisted action record."""
    status = record.get("status")
    if status == "interrupted":
        return "interrupted"
    if status == "started":
        return "in_progress"
    if not (record.get("result") or {}).get("executed"):
        return "failed_to_execute"
    outcome = (record.get("verification") or {}).get("outcome")
    if outcome == "success":
        return "verified_successful"
    if outcome == "failure":
        return "verified_failed"
    code = _returncode(record)
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return "exited_nonzero"  # ran, unverified, and signalled failure itself
    return "executed_unverified"


def failure_of(record: dict[str, Any]) -> str | None:
    """How an action failed, as a runtime fact; None if it did not fail. An
    interrupted action is not a failure: its outcome is unknown."""
    state = action_state(record)
    if state == "failed_to_execute":
        kind = (record.get("result") or {}).get("failure")
        return kind if kind in FAILURE_KINDS else "unrecorded"
    if state == "verified_failed":
        return "verification_failed"
    if state == "exited_nonzero":
        return "exited_nonzero"
    return None


def attempt_identity(kind: Any, params: Any) -> str:
    """Exact identity of an attempt (kind + params, as stored), for spotting
    repetition. No notion of semantic similarity."""
    canonical = json.dumps({"kind": kind, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
