"""Cognition: the provider-agnostic interface to whatever does the thinking.

Kairo is not the model. A provider (Claude, OpenAI, Gemini, ...) receives the
runtime's ``Context`` and returns a structured ``Decision``.

    runtime state --Runtime.context()--> Context (raw, gathered state)
        --situation.build_situation()--> situation (structured, bounded, redacted)
        --provider--> model --JSON--> parse_decision() --> Decision

``Context`` is only a gathering of existing runtime state; it is never
persisted. Providers show cognition the situation built from it (see
``kairo.situation``) and turn the answer back into a Decision with
``parse_decision``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from kairo.actions import Action
from kairo.chat import Message
from kairo.directives import Directive
from kairo.todo import TodoItem


@dataclass(frozen=True)
class Context:
    """Runtime state gathered at the start of a cycle, before any projection.
    Record lists are the most recent ones, oldest first; ``counts`` holds the
    total number of records of each kind, so omissions can be reported."""

    environment: dict[str, Any]
    directives: list[Directive]
    todo: list[TodoItem]
    messages: list[Message]
    # Why the runtime is awake now: first start, recovery, message, timer, ...
    wake_reason: str = ""
    # Recently executed actions with their results and verification, so
    # cognition knows what has already been done (including before a restart).
    recent_actions: list[dict[str, Any]] = field(default_factory=list)
    # Who and where Kairo is: identity, lifecycle state, time, defaults.
    runtime: dict[str, Any] = field(default_factory=dict)
    # The structured actions the runtime can execute (kind -> description/params).
    available_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Recent cycle-log records: what cognition decided (or how it failed) before.
    recent_cycles: list[dict[str, Any]] = field(default_factory=list)
    # Recently completed to-do items.
    done_todo: list[TodoItem] = field(default_factory=list)
    # Total records per kind (directive, todo, message, action, cycle, ...).
    counts: dict[str, int] = field(default_factory=dict)
    # The observation from the previous cycle, with "observed_at", if any.
    previous_observation: dict[str, Any] | None = None
    # Knowledge retrieved for this cycle. The boundary for a future knowledge
    # store; nothing fills it yet.
    knowledge: list[dict[str, Any]] = field(default_factory=list)


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
    # Provider bookkeeping for observability (model, duration, cost, ...).
    meta: dict[str, Any] = field(default_factory=dict)


class CognitionProvider(Protocol):
    name: str

    def decide(self, context: Context) -> Decision: ...


class CognitionError(Exception):
    """A provider could not produce a valid decision. ``category`` is a short
    machine-readable kind: unavailable, timeout, process_failed, empty_output,
    invalid_output, model_error, invalid_decision."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# -- decisions from providers ------------------------------------------------

DECISION_FIELDS = {"reason", "actions", "replies", "sleep", "wake_after"}
ACTION_FIELDS = {"kind", "params", "reason"}


def decision_schema(available_actions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """JSON Schema for a provider's answer, matching ``parse_decision``."""
    action_variants = [
        {
            "type": "object",
            "properties": {
                "kind": {"const": kind},
                "params": spec["params"],
                "reason": {"type": "string"},
            },
            "required": sorted(ACTION_FIELDS),
            "additionalProperties": False,
        }
        for kind, spec in available_actions.items()
    ]
    return {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "actions": {"type": "array", "items": {"anyOf": action_variants}},
            "replies": {"type": "array", "items": {"type": "string"}},
            "sleep": {"type": "boolean"},
            "wake_after": {"type": ["number", "null"], "minimum": 0},
        },
        "required": sorted(DECISION_FIELDS),
        "additionalProperties": False,
    }


def parse_decision(data: Any, available_actions: dict[str, dict[str, Any]]) -> Decision:
    """Strictly validate a provider's JSON answer. Anything unexpected raises
    CognitionError("invalid_decision"); nothing is guessed or repaired."""

    def invalid(message: str) -> CognitionError:
        return CognitionError("invalid_decision", message)

    if not isinstance(data, dict):
        raise invalid(f"decision must be a JSON object, got {type(data).__name__}")
    if missing := DECISION_FIELDS - data.keys():
        raise invalid(f"decision is missing fields: {sorted(missing)}")
    if extra := data.keys() - DECISION_FIELDS:
        raise invalid(f"decision has unknown fields: {sorted(extra)}")

    reason, actions, replies, sleep, wake_after = (
        data["reason"], data["actions"], data["replies"], data["sleep"], data["wake_after"])
    if not isinstance(reason, str):
        raise invalid("'reason' must be a string")
    if not isinstance(sleep, bool):
        raise invalid("'sleep' must be a boolean")
    if not isinstance(replies, list) or not all(isinstance(r, str) for r in replies):
        raise invalid("'replies' must be a list of strings")
    if wake_after is not None and (
        isinstance(wake_after, bool) or not isinstance(wake_after, (int, float))
        or not math.isfinite(wake_after) or wake_after < 0
    ):
        raise invalid("'wake_after' must be null or a non-negative number")
    if not isinstance(actions, list):
        raise invalid("'actions' must be a list")

    parsed = []
    for i, a in enumerate(actions):
        if not isinstance(a, dict) or a.keys() != ACTION_FIELDS:
            raise invalid(f"action {i} must have exactly the fields {sorted(ACTION_FIELDS)}")
        if a["kind"] not in available_actions:
            raise invalid(f"action {i} has unsupported kind {a['kind']!r}")
        if not isinstance(a["params"], dict) or not isinstance(a["reason"], str):
            raise invalid(f"action {i}: 'params' must be an object and 'reason' a string")
        parsed.append(Action(a["kind"], a["params"], reason=a["reason"]))

    return Decision(
        actions=parsed,
        replies=[r for r in replies if r.strip()],
        sleep=sleep,
        reason=reason,
        wake_after=float(wake_after) if wake_after is not None else None,
    )
