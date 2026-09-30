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


@dataclass(frozen=True)
class ActionResult:
    """Whether execution itself completed. Says nothing about the world state;
    that is what verification is for."""

    action_id: str
    executed: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
