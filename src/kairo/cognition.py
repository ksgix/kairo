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

import dataclasses
import logging
import math
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from kairo.actions import Action
from kairo.redact import protect_env, protect_files, redact
from kairo.chat import Message
from kairo.directives import Directive
from kairo.work import MAX_CHECK_ARGV, MAX_EVIDENCE, MAX_REQUESTS, WorkState


@dataclass(frozen=True)
class Context:
    """Runtime state gathered at the start of a cycle, before any projection.
    Record lists are the most recent ones, oldest first; ``counts`` holds the
    total number of records of each kind, so omissions can be reported."""

    environment: dict[str, Any]
    directives: list[Directive]
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
    # Total records per kind (directive, message, action, cycle, ...).
    counts: dict[str, int] = field(default_factory=dict)
    # The observation from the previous cycle, with "observed_at", if any.
    previous_observation: dict[str, Any] | None = None
    # Ongoing work records (see kairo.work): open ones and recently closed ones,
    # and each open item's most recent attempts (linked action records).
    open_work: list[dict[str, Any]] = field(default_factory=list)
    closed_work: list[dict[str, Any]] = field(default_factory=list)
    work_attempts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Each open item's attempt log (compact summaries, oldest first, up to the
    # ledger's scan limit), from which recovery facts are derived.
    work_attempt_log: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # The derived implementation catalog (kairo.implementations): metadata and
    # guidance of the packages the operator configured.
    implementations: list[dict[str, Any]] = field(default_factory=list)
    # Facts about Kairo's own code when deployment is configured (kairo.deploy):
    # the running release, the release links, the development repository and the
    # latest deployments. Empty otherwise.
    code: dict[str, Any] = field(default_factory=dict)


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
    """A cognition implementation (an adapter for one model service). It only
    turns a Context into a Decision; it knows nothing about other providers,
    fallback, or Kairo's state, and must have no side effects of its own (no
    tools): a failed or timed-out call cannot have changed anything.

    Optional attributes ``secret_env`` (environment variable names) and
    ``secret_files`` (paths) declare where this provider's credentials live, so
    Kairo can keep them out of its state, logs and actions (kairo.redact)."""

    name: str

    def decide(self, context: Context) -> Decision: ...


class CognitionError(Exception):
    """A provider could not produce a usable decision. ``category`` is one of
    OUTCOMES; anything else is treated as provider_error."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


log = logging.getLogger("kairo.cognition")

# Why a provider produced no usable decision. Closed vocabulary.
#
# Fallback is for technical failures only: the provider could not be used, or
# what came back is not a decision Kairo can use at all. It is never a judgment
# of quality: Kairo never compares two valid decisions, and never routes around
# a model that answered.
#
# invalid_decision falls back: an answer that breaks the decision contract is
# no decision (a runtime-checked, binary fact, like invalid_output), so trying
# the next provider replaces "nothing usable" with something usable, not one
# opinion with another. model_error does NOT fall back: the model ran and
# declined or failed to answer (a refusal, a safety block, a provider-reported
# model failure); sending the same request to another model to get around that
# would be shopping for an answer.
FALLBACK = frozenset({
    "unavailable",       # the provider could not be reached or started
    "timeout",           # no answer within the provider's timeout
    "process_failed",    # the provider's process or transport failed
    "auth_failed",       # credentials missing, expired or rejected
    "rate_limited",      # throttled or overloaded
    "empty_output",      # nothing came back
    "invalid_output",    # what came back is not a provider answer at all
    "invalid_decision",  # an answer, but not a valid Kairo decision
    "provider_error",    # the adapter itself failed unexpectedly
})
TERMINAL = frozenset({
    "model_error",       # the model declined or failed to answer
})
OUTCOMES = FALLBACK | TERMINAL
DETAIL_LIMIT = 300  # characters of failure detail kept per attempt


def validate_decision(decision: Any) -> Decision:
    """What every provider's answer must be, whatever the provider."""
    if not isinstance(decision, Decision):
        raise CognitionError("invalid_decision",
                             f"decide() returned {type(decision).__name__}, not Decision")
    if not all(isinstance(a, Action) for a in decision.actions):
        raise CognitionError("invalid_decision",
                             "Decision.actions must contain only Action instances")
    if not isinstance(decision.work, list):
        raise CognitionError("invalid_decision", "Decision.work must be a list")
    return decision


@dataclass(frozen=True)
class Attempt:
    """One provider call within a cycle. Runtime facts only."""

    provider: str
    outcome: str          # "decided", or one of OUTCOMES
    fallback: bool        # whether this outcome let the next provider be tried
    seconds: float
    detail: str | None = None  # bounded, redacted failure message


@dataclass(frozen=True)
class CognitionResult:
    """What Kairo's cognition produced this cycle: at most one Decision, from at
    most one provider, and how it came to be that one."""

    decision: Decision | None
    provider: str | None                 # the provider whose Decision is used
    selection: dict[str, Any] | None     # {"position", "reason"} of that provider
    attempts: list[Attempt]
    failure: str | None = None           # outcome of the last attempt, if none decided

    def summary(self) -> dict[str, Any]:
        """The provenance recorded with the cycle."""
        return {"provider": self.provider, "selection": self.selection,
                "attempts": [dataclasses.asdict(a) for a in self.attempts]}


class Cognition:
    """Kairo's cognition: asks providers, in order, for this cycle's decision.

    Not a provider and not an agent: it reasons about nothing and changes no
    state. It owns which providers are asked, in what order, whether a failure
    lets the next one be asked, and the record of what happened. Each provider is
    asked at most once per cycle, every cycle starts from the beginning of the
    order, and the first usable Decision is the one used."""

    def __init__(self, providers: Sequence[CognitionProvider]) -> None:
        names = [getattr(p, "name", None) for p in providers]
        if not providers:
            raise ValueError("cognition needs at least one provider")
        if not all(isinstance(n, str) and n for n in names) or len(set(names)) != len(names):
            raise ValueError(f"provider names must be non-empty and unique: {names}")
        self.providers = list(providers)
        # Providers' credentials: always redacted, never given to actions.
        protect_env(self.secret_env())
        protect_files({path for p in self.providers for path in getattr(p, "secret_files", ())})

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    def secret_env(self) -> set[str]:
        return {name for p in self.providers for name in getattr(p, "secret_env", ())}

    def order(self, context: Context) -> list[CognitionProvider]:
        """Which providers to ask, in what order. Phase 7: the configured order,
        unchanged. A dynamic policy would replace only this method."""
        return list(self.providers)

    def decide(self, context: Context) -> CognitionResult:
        attempts: list[Attempt] = []
        for position, provider in enumerate(self.order(context)):
            previous = attempts[-1] if attempts else None
            reason = ("first_in_order" if previous is None
                      else f"fallback_after:{previous.provider}:{previous.outcome}")
            # The same gathered Context for every attempt; only the runtime fact
            # of who is being asked, and why, differs.
            asked = dataclasses.replace(context, runtime={
                **context.runtime, "cognition": {"provider": provider.name, "selected": reason}})
            started = time.monotonic()
            try:
                decision = validate_decision(provider.decide(asked))
            except Exception as exc:
                category = getattr(exc, "category", None)
                if category not in OUTCOMES:
                    category = "provider_error"
                if isinstance(exc, CognitionError):
                    log.error("cognition provider %s failed (%s): %s",
                              provider.name, category, redact(str(exc), limit=DETAIL_LIMIT))
                    detail = str(exc)
                else:  # an adapter bug: keep the traceback, but redacted
                    log.error("cognition provider %s failed: %s", provider.name,
                              redact(traceback.format_exc(), limit=4000))
                    detail = repr(exc)
                attempts.append(Attempt(provider.name, category, category in FALLBACK,
                                        round(time.monotonic() - started, 3),
                                        redact(detail, limit=DETAIL_LIMIT)))
                if category not in FALLBACK:
                    break
                continue
            attempts.append(Attempt(provider.name, "decided", False,
                                    round(time.monotonic() - started, 3)))
            return CognitionResult(decision, provider.name,
                                   {"position": position, "reason": reason}, attempts)
        last = attempts[-1]
        return CognitionResult(None, None, None, attempts, failure=last.outcome)


def as_cognition(cognition: Any) -> Cognition | None:
    """A Cognition for whatever was configured: itself, a single provider (asked
    alone), or None."""
    if cognition is None or isinstance(cognition, Cognition):
        return cognition
    return Cognition([cognition])


# -- decisions from providers ------------------------------------------------

DECISION_FIELDS = {"reason", "actions", "replies", "sleep", "wake_after", "work"}
ACTION_FIELDS = {"kind", "params", "reason", "work"}
# Optional per action: "resumes", an earlier action whose external outcome is
# unresolved (validated by the runtime; see Runtime._resume_key).
OPTIONAL_ACTION_FIELDS = {"resumes"}
WORK_FIELDS = {
    "create": {"op", "ref", "objective", "why", "directive_id", "strategy", "next_step"},
    "update": {"op", "work_id", "understanding", "strategy", "next_step"},
    "set_state": {"op", "work_id", "state", "reason", "wait_seconds", "evidence"},
}
# Optional per request kind. create "check": an argv the runtime itself runs when
# completion of that work is requested (validated by kairo.work.WorkLedger).
WORK_OPTIONAL = {"create": {"check"}, "update": set(), "set_state": set()}
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
                # Optional: the id of an earlier action whose external outcome is
                # unknown, continued under the same operation key.
                "resumes": _OPT_STR,
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
                   "directive_id": _OPT_STR, "strategy": _STR, "next_step": _STR,
                   "check": {"type": ["array", "null"], "items": _STR, "minItems": 1,
                             "maxItems": MAX_CHECK_ARGV}},
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
        optional = WORK_OPTIONAL[r["op"]]
        if not WORK_FIELDS[r["op"]] <= r.keys() or r.keys() - WORK_FIELDS[r["op"]] - optional:
            raise invalid(f"work request {i} ({r['op']}) must have exactly the fields "
                          f"{sorted(WORK_FIELDS[r['op']])}"
                          + (f" (optionally {sorted(optional)})" if optional else ""))
        check = r.get("check")
        if check is not None and not (isinstance(check, list) and check
                                      and all(isinstance(a, str) for a in check)):
            raise invalid(f"work request {i}: 'check' must be a non-empty list of strings "
                          "or null")
        for key, value in r.items():
            if key in ("op", "evidence", "wait_seconds", "check"):
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
        if not isinstance(a, dict) or not ACTION_FIELDS <= a.keys() \
                or a.keys() - ACTION_FIELDS - OPTIONAL_ACTION_FIELDS:
            raise invalid(f"action {i} must have the fields {sorted(ACTION_FIELDS)} "
                          f"(optionally {sorted(OPTIONAL_ACTION_FIELDS)})")
        if a["kind"] not in available_actions:
            raise invalid(f"action {i} has unsupported kind {a['kind']!r}")
        if not isinstance(a["params"], dict) or not isinstance(a["reason"], str):
            raise invalid(f"action {i}: 'params' must be an object and 'reason' a string")
        if a["work"] is not None and not isinstance(a["work"], str):
            raise invalid(f"action {i}: 'work' must be a work id, a ref, or null")
        if a.get("resumes") is not None and not isinstance(a["resumes"], str):
            raise invalid(f"action {i}: 'resumes' must be an action id or null")
        # work_id holds the name cognition used; the runtime resolves and validates it.
        parsed.append(Action(a["kind"], a["params"], reason=a["reason"], work_id=a["work"],
                             resumes=a.get("resumes")))

    return Decision(
        actions=parsed,
        replies=[r for r in replies if r.strip()],
        sleep=sleep,
        reason=reason,
        wake_after=float(wake_after) if wake_after is not None else None,
        work=work,
    )
