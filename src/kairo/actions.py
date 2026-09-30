"""Actions: the structured boundary between deciding and executing.

Cognition produces ``Action`` values; the environment executes them and
returns an ``ActionResult``. An action is data, never raw shell text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


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
    return {"success": "verified_successful",
            "failure": "verified_failed"}.get(outcome, "executed_unverified")
