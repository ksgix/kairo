"""Verification: did the world actually end up the way it was meant to?

Execution succeeding is not the same as the intended outcome being achieved.
A verifier inspects the action, its result and (as needed) the environment,
and reports one of three outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from kairo.actions import Action, ActionResult


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Verification:
    outcome: Outcome
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


class Verifier(Protocol):
    def verify(self, action: Action, result: ActionResult) -> Verification: ...


def verify(
    action: Action, result: ActionResult, verifier: Verifier | None = None
) -> Verification:
    """Verify an executed action. Without a verifier the outcome is
    UNVERIFIABLE, never assumed successful."""
    if not result.executed:
        return Verification(Outcome.FAILURE, f"execution failed: {result.error}")
    if verifier is None:
        return Verification(Outcome.UNVERIFIABLE, "no verifier for this action")
    return verifier.verify(action, result)
