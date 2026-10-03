"""Ongoing work: meaningful pursuits that persist across cognition cycles.

Layers, from lasting to momentary:

    directive      a lasting area of responsibility ("keep the 1C environment healthy")
    work           a pursuit within (or discovered outside) a directive: an objective,
                   why it matters, the current strategy and understanding, a state
    todo           operational notes, maintained by the operator
    action         one concrete runtime operation; an attempt at a work item when linked
    verification   runtime evidence about an action's outcome

Cognition never writes work state. It sends work *requests* in its decision
(create / update / set_state); ``WorkLedger.apply`` validates each one against
the persisted state and applies it or rejects it with a reason. The runtime
alone assigns ids and timestamps.

The work record is the single authority for a work item's current state, and
keeps a short log of its own changes. Attempts are not copied into it: they are
the action records whose ``work_id`` points here, read from the action log.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kairo.actions import FAILED, action_state, attempt_identity
from kairo.memory import Collection, Memory, from_record
from kairo.redact import redact


class WorkState(StrEnum):
    ACTIVE = "active"      # Kairo intends to keep pursuing it
    WAITING = "waiting"    # paused until something external or a time
    BLOCKED = "blocked"    # a concrete obstacle prevents progress
    COMPLETED = "completed"  # the outcome was achieved, with evidence (terminal)
    ABANDONED = "abandoned"  # deliberately no longer pursued (terminal)


OPEN = frozenset({WorkState.ACTIVE, WorkState.WAITING, WorkState.BLOCKED})
CLOSED = frozenset({WorkState.COMPLETED, WorkState.ABANDONED})

# Closed work is history: it never changes again. A new reason means new work.
TRANSITIONS: dict[WorkState, frozenset[WorkState]] = {
    WorkState.ACTIVE: frozenset({WorkState.WAITING, WorkState.BLOCKED,
                                 WorkState.COMPLETED, WorkState.ABANDONED}),
    WorkState.WAITING: frozenset({WorkState.ACTIVE, WorkState.BLOCKED,
                                  WorkState.COMPLETED, WorkState.ABANDONED}),
    WorkState.BLOCKED: frozenset({WorkState.ACTIVE, WorkState.WAITING,
                                  WorkState.COMPLETED, WorkState.ABANDONED}),
    WorkState.COMPLETED: frozenset(),
    WorkState.ABANDONED: frozenset(),
}

# Maximum characters per cognition-written field. Longer requests are rejected,
# not silently cut, so cognition learns the bound.
TEXT_LIMITS = {"objective": 300, "why": 500, "strategy": 600, "understanding": 1000,
               "next_step": 300, "reason": 500, "ref": 40}
MAX_REQUESTS = 10      # work requests per decision
MAX_OPEN = 25          # open work items at once
MAX_EVIDENCE = 10      # action ids cited for a completion
MAX_WAIT = 30 * 86400  # longest wait_seconds accepted
HISTORY = 12           # change-log entries kept per work item
STRATEGY_LOG = 10      # strategy revisions remembered per work item
STRATEGY_TEXT = 200    # characters of each remembered strategy
ATTEMPT_SCAN = 200     # linked actions examined (evidence, recovery facts, repetition)

# A completion must cite at least one of this work's attempts that succeeded:
# verified by a runtime verifier, or, where no verifier exists, ran and exited 0.
EVIDENCE_STATES = frozenset({"verified_successful", "executed_unverified"})

# How a completion is grounded; always computed by the runtime, never taken from
# cognition. "verified": at least one cited attempt was verified successful.
# "unverified": the runtime could not check the outcome; the completion is
# cognition's judgment, resting on attempts that ran and exited 0.
VERIFIED, UNVERIFIED = "verified", "unverified"


@dataclass(frozen=True)
class Work:
    objective: str
    why: str
    state: str = WorkState.ACTIVE
    directive_id: str | None = None
    strategy: str = ""
    strategy_revision: int = 1
    understanding: str = ""
    next_step: str = ""
    # Waiting condition, blocker, completion note or abandonment reason.
    state_reason: str | None = None
    waiting_until: float | None = None
    # At completion: each cited action and its runtime verdict at that moment.
    evidence: list[dict[str, Any]] = field(default_factory=list)
    # At completion: VERIFIED or UNVERIFIED (None before completion, and on
    # records written before this field existed).
    completion_basis: str | None = None
    created_at: float | None = None
    updated_at: float | None = None
    state_since: float | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # When understanding last changed (runtime time), so the runtime can tell
    # whether cognition reassessed after a failure. None: never changed.
    understanding_at: float | None = None
    # The last STRATEGY_LOG strategies: {"revision", "text", "since"}.
    strategy_log: list[dict[str, Any]] = field(default_factory=list)


# Fields whose bad values the work code and the situation already treat as unknown
# (times, logs, completion data): a bad value there is shown, not a corrupt record.
LENIENT = frozenset({"understanding_at", "strategy_log", "waiting_until", "history", "evidence",
                     "completion_basis", "created_at", "updated_at", "state_since"})


def work_from_record(data: Any) -> Work:
    return from_record(Work, data, LENIENT)


class WorkError(ValueError):
    """A work request the runtime refuses. The message says why."""


@dataclass(frozen=True)
class WorkOutcome:
    """What happened to one decision's work requests."""

    refs: dict[str, str] = field(default_factory=dict)  # create ref -> new work id
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)


class WorkLedger(Collection[Work]):
    def __init__(self, memory: Memory) -> None:
        super().__init__(memory, "work", Work)
        self._memory = memory

    def get(self, id: str) -> Work | None:
        data = self._memory.get("work", id)
        return work_from_record(data) if data is not None else None

    def all(self) -> list[Work]:
        """Readable work records. A corrupt record is skipped here (the situation
        reports it); it must not stop other work from being created or changed."""
        items = []
        for data in self._memory.all("work"):
            try:
                items.append(work_from_record(data))
            except (TypeError, ValueError):
                continue
        return items

    def open(self) -> list[Work]:
        return [w for w in self.all() if w.state in OPEN]

    def closed(self) -> list[Work]:
        return [w for w in self.all() if w.state in CLOSED]

    def attempts(self, work_id: str, limit: int) -> list[dict[str, Any]]:
        """The most recent actions linked to a work item, oldest first."""
        return self._memory.recent_where("action", "work_id", work_id, limit)

    def resolve(self, name: str | None, refs: dict[str, str]) -> str | None:
        """The open work an action names (an id, or a ref created this decision)."""
        if name is None:
            return None
        work_id = refs.get(name, name)
        try:
            work = self.get(work_id)
        except (TypeError, ValueError):
            work = None
        if work is None or work.state not in OPEN:
            raise WorkError(f"no open work {name!r} to link the action to")
        return work_id

    # -- applying cognition's requests ----------------------------------------

    def apply(self, requests: list[dict[str, Any]], now: float | None = None) -> WorkOutcome:
        """Validate and apply each request in order. A rejected request changes
        nothing and does not stop the others."""
        now = time.time() if now is None else now
        outcome = WorkOutcome()
        for request in requests:
            try:
                if not isinstance(request, dict):
                    raise WorkError("a work request must be an object")
                applied = self._apply_one(request, outcome.refs, now)
            except Exception as exc:  # malformed input is a rejection, never a crash
                reason = str(exc) if isinstance(exc, WorkError) else \
                    f"malformed request ({type(exc).__name__})"
                outcome.rejected.append({
                    "op": request.get("op") if isinstance(request, dict) else None,
                    "target": _target(request) if isinstance(request, dict) else None,
                    "reason": reason})
            else:
                outcome.applied.append(applied)
        return outcome

    def _apply_one(self, req: dict[str, Any], refs: dict[str, str], now: float) -> dict[str, Any]:
        op = req.get("op")
        if op == "create":
            return self._create(req, refs, now)
        work = self._existing(req.get("work_id"))
        if op == "update":
            return self._update(work, req, now)
        if op == "set_state":
            return self._set_state(work, req, now)
        raise WorkError(f"unknown work request {op!r}")

    def _create(self, req: dict[str, Any], refs: dict[str, str], now: float) -> dict[str, Any]:
        objective = _text(req, "objective", required=True)
        ref = req["ref"]
        if ref in refs:
            raise WorkError(f"ref {ref!r} is already used in this decision")
        directive_id = req.get("directive_id")
        if directive_id is not None:
            directive = self._memory.get("directive", directive_id)
            if not isinstance(directive, dict) or not directive.get("active"):
                raise WorkError(f"no active directive {directive_id!r}")
        open_work = self.open()
        if len(open_work) >= MAX_OPEN:
            raise WorkError(f"already {len(open_work)} open work items; close or reuse one")
        wanted = _normalise(objective)
        for other in open_work:
            if _normalise(other.objective) == wanted:
                raise WorkError(f"open work {other.id} already has this objective")
        work = Work(
            objective=objective, why=_text(req, "why", required=True), directive_id=directive_id,
            strategy=_text(req, "strategy"), next_step=_text(req, "next_step"),
            created_at=now, updated_at=now, state_since=now,
            history=[{"at": now, "event": "created"}],
        )
        work = dataclasses.replace(work, strategy_log=[
            {"revision": 1, "text": work.strategy[:STRATEGY_TEXT], "since": now}])
        self.save(_clean(work))
        refs[ref] = work.id
        return {"op": "create", "ref": ref, "work_id": work.id}

    def _update(self, work: Work, req: dict[str, Any], now: float) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        events = []
        for name in ("understanding", "next_step"):
            if req.get(name) is not None:
                value = _text(req, name)
                if value != getattr(work, name):
                    changes[name] = value
                    events.append({"at": now, "event": f"{name}_updated"})
                    if name == "understanding":
                        changes["understanding_at"] = now
        if req.get("strategy") is not None:
            strategy = _text(req, "strategy", required=True)
            if strategy != work.strategy:
                revision = work.strategy_revision + 1
                changes["strategy"] = strategy
                changes["strategy_revision"] = revision
                changes["strategy_log"] = ([e for e in work.strategy_log if isinstance(e, dict)]
                                           + [{"revision": revision,
                                               "text": strategy[:STRATEGY_TEXT],
                                               "since": now}])[-STRATEGY_LOG:]
                events.append({"at": now, "event": "strategy_changed", "revision": revision})
        if not changes:
            raise WorkError("update changes nothing")
        self._save(work, changes, events, now)
        return {"op": "update", "work_id": work.id, "changed": sorted(changes)}

    def _set_state(self, work: Work, req: dict[str, Any], now: float) -> dict[str, Any]:
        try:
            target = WorkState(req.get("state"))
        except ValueError:
            raise WorkError(f"unknown work state {req.get('state')!r}") from None
        current = WorkState(work.state)
        if target not in TRANSITIONS[current]:
            raise WorkError(f"work cannot go from {current} to {target}")
        reason = _text(req, "reason", required=True)
        changes: dict[str, Any] = {"state": target, "state_reason": reason,
                                   "state_since": now, "waiting_until": None}
        wait = req.get("wait_seconds")
        if wait is not None:
            if target is not WorkState.WAITING:
                raise WorkError("wait_seconds only applies to waiting")
            if not 0 < wait <= MAX_WAIT:
                raise WorkError(f"wait_seconds must be in (0, {MAX_WAIT}]")
            changes["waiting_until"] = now + wait
        evidence_ids = req.get("evidence") or []
        if target is WorkState.COMPLETED:
            evidence = self._evidence(work, evidence_ids)
            changes["evidence"] = evidence
            changes["completion_basis"] = (
                VERIFIED if any(e["state"] == "verified_successful" for e in evidence)
                else UNVERIFIED)
        elif evidence_ids:
            raise WorkError("evidence only applies to completion")
        self._save(work, changes, [{"at": now, "event": "state_changed", "from": current,
                                    "to": target, "reason": reason[:200]}], now)
        return {"op": "set_state", "work_id": work.id, "from": current, "to": target}

    def _evidence(self, work: Work, ids: list[str]) -> list[dict[str, Any]]:
        """Completion must rest on this work's own attempts that actually ran."""
        if not ids:
            raise WorkError("completion needs evidence: ids of this work's attempts that "
                            "achieved the outcome")
        if len(ids) > MAX_EVIDENCE:
            raise WorkError(f"at most {MAX_EVIDENCE} evidence ids")
        linked = {a.get("id"): a for a in self.attempts(work.id, ATTEMPT_SCAN)}
        evidence = []
        for action_id in ids:
            record = linked.get(action_id)
            if record is None:
                raise WorkError(f"action {action_id!r} is not an attempt at this work")
            state = action_state(record)
            returncode = ((record.get("result") or {}).get("output") or {}).get("returncode")
            if state == "exited_nonzero":
                raise WorkError(f"action {action_id} cannot be evidence: it exited "
                                f"{returncode} and no verifier confirmed success")
            if state not in EVIDENCE_STATES:
                raise WorkError(f"action {action_id} cannot be evidence: it is {state}")
            # Unverified, the exit code is the only success signal the runtime has.
            # (A verifier may accept a non-zero exit; then the attempt is verified.)
            if state == "executed_unverified" and not (
                    isinstance(returncode, int) and not isinstance(returncode, bool)
                    and returncode == 0):
                raise WorkError(f"action {action_id} cannot be evidence: it exited "
                                f"{returncode} and no verifier confirmed success")
            evidence.append({"action_id": action_id, "state": state, "returncode": returncode})
        return evidence

    def unsettled_repeat(self, work_id: str, identity: str) -> dict[str, Any] | None:
        """An earlier attempt at this work, identical to ``identity``, that failed, was
        interrupted or has an unknown external outcome, after cognition last changed
        its understanding. Repeating it
        without reassessing is refused. No counting: a changed understanding clears it."""
        try:
            work = self.get(work_id)
        except (TypeError, ValueError):
            return None
        if work is None:
            return None
        since = work.understanding_at if isinstance(work.understanding_at, (int, float)) else None
        for record in reversed(self.attempts(work_id, ATTEMPT_SCAN)):
            state = action_state(record)
            if state not in FAILED and state not in ("interrupted", "outcome_unknown"):
                continue
            at = record.get("finished_at") or record.get("started_at")
            if since is not None and isinstance(at, (int, float)) and at <= since:
                continue  # reassessed after this one
            if attempt_identity(record.get("kind"), record.get("params")) == identity:
                return {"action_id": record.get("id"), "state": state}
        return None

    # -- helpers ---------------------------------------------------------------

    def _existing(self, work_id: Any) -> Work:
        if not isinstance(work_id, str):
            raise WorkError("work_id must be a string")
        try:
            work = self.get(work_id)
        except (TypeError, ValueError):
            raise WorkError(f"work {work_id!r} is unreadable (corrupt record)") from None
        if work is None:
            raise WorkError(f"no work {work_id!r}")
        if work.state in CLOSED:
            raise WorkError(f"work {work_id} is {work.state}; closed work does not change")
        return work

    def _save(self, work: Work, changes: dict[str, Any], events: list[dict[str, Any]],
              now: float) -> None:
        history = (work.history + events)[-HISTORY:]
        self.save(_clean(dataclasses.replace(work, **changes, updated_at=now, history=history)))


def _text(req: dict[str, Any], name: str, required: bool = False) -> str:
    value = req.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise WorkError(f"{name!r} must be a non-empty string")
    if len(value) > TEXT_LIMITS[name]:
        raise WorkError(f"{name!r} is longer than {TEXT_LIMITS[name]} characters")
    return value.strip()


def _clean(work: Work) -> Work:
    """Cognition's text is untrusted: redact secret values before persisting."""
    return work_from_record(redact(dataclasses.asdict(work)))


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _target(req: dict[str, Any]) -> Any:
    return req.get("work_id") or req.get("ref")
