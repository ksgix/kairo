"""Cognition: the provider-agnostic interface to whatever does the thinking.

Kairo is not the model. A provider (Claude, OpenAI, Gemini, ...) receives the
runtime's ``Context`` and returns a structured ``Decision``. No provider is
built in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from kairo.actions import Action
from kairo.chat import Message
from kairo.directives import Directive
from kairo.todo import TodoItem


@dataclass(frozen=True)
class Context:
    """What the runtime shows cognition at the start of a cycle."""

    environment: dict[str, Any]
    directives: list[Directive]
    todo: list[TodoItem]
    messages: list[Message]
    # Why the runtime is awake now: first start, recovery, message, timer, ...
    wake_reason: str = ""
    # Recently executed actions with their results and verification, so
    # cognition knows what has already been done (including before a restart).
    recent_actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    actions: list[Action] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    # Cognition, not the runtime, judges whether worthwhile work remains.
    sleep: bool = False
    reason: str = ""
    # When sleeping: reassess after this many seconds. None means use the
    # runtime's default reassessment interval (which may be "until woken").
    wake_after: float | None = None


class CognitionProvider(Protocol):
    name: str

    def decide(self, context: Context) -> Decision: ...
