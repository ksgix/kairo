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
from kairo.work import MAX_EVIDENCE, MAX_REQUESTS, WorkState


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
    # Ongoing work records (see kairo.work): open ones and recently closed ones,
    # and each open item's most recent attempts (linked action records).
    open_work: list[dict[str, Any]] = field(default_factory=list)
    closed_work: list[dict[str, Any]] = field(default_factory=list)
    work_attempts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Each open item's attempt log (compact summaries, oldest first, up to the
    # ledger's scan limit), from which recovery facts are derived.
    work_attempt_log: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


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
    # Requests to create or change ongoing work, validated and applied by the
    # runtime (see kairo.work). Plain dicts shaped as in decision_schema.
    work: list[dict[str, Any]] = field(default_factory=list)
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

DECISION_FIELDS = {"reason", "actions", "replies", "sleep", "wake_after", "work"}
ACTION_FIELDS = {"kind", "params", "reason", "work"}
WORK_FIELDS = {
    "create": {"op", "ref", "objective", "why", "directive_id", "strategy", "next_step"},
    "update": {"op", "work_id", "understanding", "strategy", "next_step"},
    "set_state": {"op", "work_id", "state", "reason", "wait_seconds", "evidence"},
}
# Fields that may be null, per request kind (all other string fields must be strings).
WORK_NULLABLE = {"create": {"directive_id"},
                 "update": {"understanding", "strategy", "next_step"},
                 "set_state": set()}
_STR = {"type": "string"}
_OPT_STR = {"type": ["string", "null"]}


def decision_schema(available_actions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """JSON Schema for a provider's answer, matching ``parse_decision``."""
    action_variants = [
        {
            "type": "object",
            "properties": {
                "kind": {"const": kind},
                "params": spec["params"],
                "reason": {"type": "string"},
                # The work this action is an attempt at: a work id, a ref created in
                # this decision, or null.
                "work": _OPT_STR,
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
            "work": {"type": "array", "maxItems": MAX_REQUESTS,
                     "items": {"anyOf": _work_variants()}},
        },
        "required": sorted(DECISION_FIELDS),
        "additionalProperties": False,
    }


def _work_variants() -> list[dict[str, Any]]:
    props = {
        "create": {"op": {"const": "create"}, "ref": _STR, "objective": _STR, "why": _STR,
                   "directive_id": _OPT_STR, "strategy": _STR, "next_step": _STR},
        "update": {"op": {"const": "update"}, "work_id": _STR, "understanding": _OPT_STR,
                   "strategy": _OPT_STR, "next_step": _OPT_STR},
        "set_state": {"op": {"const": "set_state"}, "work_id": _STR,
                      "state": {"enum": [s.value for s in WorkState]}, "reason": _STR,
                      "wait_seconds": {"type": ["number", "null"]},
                      "evidence": {"type": "array", "items": _STR, "maxItems": MAX_EVIDENCE}},
    }
    return [{"type": "object", "properties": props[op], "required": sorted(WORK_FIELDS[op]),
             "additionalProperties": False} for op in WORK_FIELDS]


def _parse_work(requests: Any) -> list[dict[str, Any]]:
    """Structural checks only; whether a request makes sense is decided by the
    runtime against persisted state (kairo.work.WorkLedger)."""

    def invalid(message: str) -> CognitionError:
        return CognitionError("invalid_decision", message)

    if not isinstance(requests, list):
        raise invalid("'work' must be a list")
    if len(requests) > MAX_REQUESTS:
        raise invalid(f"at most {MAX_REQUESTS} work requests per decision")
    refs = set()
    for i, r in enumerate(requests):
        if not isinstance(r, dict) or r.get("op") not in WORK_FIELDS:
            raise invalid(f"work request {i} must be an object with op create/update/set_state")
        if r.keys() != WORK_FIELDS[r["op"]]:
            raise invalid(f"work request {i} ({r['op']}) must have exactly the fields "
                          f"{sorted(WORK_FIELDS[r['op']])}")
        for key, value in r.items():
            if key in ("op", "evidence", "wait_seconds"):
                continue
            nullable = key in WORK_NULLABLE[r["op"]]
            if not (isinstance(value, str) or (nullable and value is None)):
                raise invalid(f"work request {i}: '{key}' must be a string"
                              + (" or null" if nullable else ""))
        if r["op"] == "create":
            if not r["ref"] or r["ref"] in refs:
                raise invalid(f"work request {i}: 'ref' must be non-empty and unique")
            refs.add(r["ref"])
        if r["op"] == "set_state":
            wait = r["wait_seconds"]
            if wait is not None and (isinstance(wait, bool) or not isinstance(wait, (int, float))
                                     or not math.isfinite(wait)):
                raise invalid(f"work request {i}: 'wait_seconds' must be null or a number")
            ev = r["evidence"]
            if not isinstance(ev, list) or len(ev) > MAX_EVIDENCE or \
                    not all(isinstance(x, str) for x in ev):
                raise invalid(f"work request {i}: 'evidence' must be a list of at most "
                              f"{MAX_EVIDENCE} action ids")
    return requests


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
    work = _parse_work(data["work"])
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
        if a["work"] is not None and not isinstance(a["work"], str):
            raise invalid(f"action {i}: 'work' must be a work id, a ref, or null")
        # work_id holds the name cognition used; the runtime resolves and validates it.
        parsed.append(Action(a["kind"], a["params"], reason=a["reason"], work_id=a["work"]))

    return Decision(
        actions=parsed,
        replies=[r for r in replies if r.strip()],
        sleep=sleep,
        reason=reason,
        wake_after=float(wake_after) if wake_after is not None else None,
        work=work,
    )
