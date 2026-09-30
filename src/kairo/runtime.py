"""The Kairo runtime: persistent lifecycle around an observe/decide/act/verify cycle.

The runtime owns no intelligence. Each ``cycle`` observes the environment,
hands the context to the cognition provider (if any), executes the actions it
decided on, verifies them, and sleeps when cognition says nothing worthwhile
remains or when there is no cognition at all.

``run_forever`` keeps the runtime alive: it cycles while awake and, while
sleeping, blocks on a condition variable until something wakes it (a message,
an explicit wake request, its own reassessment deadline) or it is stopped.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kairo.actions import Action, ActionResult
from kairo.chat import Chat, Message, Sender
from kairo.cognition import CognitionError, CognitionProvider, Context, Decision
from kairo.directives import Directives
from kairo.environment import Environment
from kairo.memory import Memory
from kairo.redact import redact
from kairo.situation import LIMITS
from kairo.todo import Todo
from kairo.verification import Outcome, Verification, Verifier, verify
from kairo.work import CLOSED, OPEN, WorkError, WorkLedger

log = logging.getLogger("kairo")

STORED_STRING_LIMIT = 16_000  # characters per string in persisted action records


class State(StrEnum):
    CREATED = "created"
    AWAKE = "awake"
    SLEEPING = "sleeping"
    STOPPED = "stopped"


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    action: Action
    result: ActionResult
    verification: Verification


@dataclass(frozen=True)
class CycleReport:
    state: State
    steps: list[Step] = field(default_factory=list)
    note: str = ""
    # Small summary of what cognition did this cycle, for the cycle log.
    cognition: dict[str, Any] = field(default_factory=dict)


class Runtime:
    def __init__(
        self,
        memory: Memory,
        environment: Environment | None = None,
        cognition: CognitionProvider | None = None,
        verifiers: dict[str, Verifier] | None = None,
        reassess_after: float | None = None,
    ) -> None:
        """``reassess_after``: default seconds a sleeping runtime waits before
        waking itself to reassess. None means sleep until explicitly woken."""
        self.memory = memory
        self.environment = environment or Environment()
        self.cognition = cognition
        self.verifiers = verifiers or {}
        self.reassess_after = reassess_after
        self.directives = Directives(memory)
        self.todo = Todo(memory)
        self.chat = Chat(memory)
        self.work = WorkLedger(memory)
        self.state = State.CREATED
        self.reason = ""
        # What the previous process left behind, if anything.
        previous = memory.get("runtime", "lifecycle")
        self.previous: dict[str, Any] | None = previous if isinstance(previous, dict) else None
        identity = memory.get("runtime", "identity")
        if not isinstance(identity, dict) or not identity.get("id"):
            identity = {"id": uuid.uuid4().hex, "born_at": time.time(), "starts": 0}
        self.identity: dict[str, Any] = identity

        # All lifecycle state changes happen under this condition, and every
        # change notifies it, so any thread can wait for or cause a transition.
        self._cond = threading.Condition(threading.RLock())
        self._running = False
        self._stop_requested = False
        self._wake_pending: str | None = None
        self._wake_deadline: float | None = None  # time.monotonic() value
        self._wake_at: float | None = None  # the same deadline as wall-clock time
        self._since: float | None = None  # when the current state began
        self._process_started_at: float | None = None
        self._cycles = 0  # cycles completed by this process

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._cond:
            if self.state not in (State.CREATED, State.STOPPED):
                raise LifecycleError(f"cannot go from {self.state} to {State.AWAKE}")
            if self._process_started_at is None:
                self._process_started_at = time.time()
            self._transition({self.state}, State.AWAKE, self._start_reason())
            starts = self.identity.get("starts")
            self.identity["starts"] = (starts if isinstance(starts, int) else 0) + 1
            self.memory.put("runtime", "identity", self.identity)
            self._recover_interrupted_actions()

    def _start_reason(self) -> str:
        if self.state is State.STOPPED:
            return "restarted"
        previous = (self.previous or {}).get("state")
        if previous is None:
            return "first start"
        if previous == State.STOPPED:
            return "started after clean stop"
        return f"recovered: previous process ended while {previous}"

    def _recover_interrupted_actions(self) -> None:
        # Actions that began but never recorded a result were cut off by the
        # previous process. They are marked, never re-executed: whether to try
        # again is a decision for cognition, with the world re-observed.
        for record in self.memory.all("action"):
            if isinstance(record, dict) and record.get("status") == "started" and record.get("id"):
                record["status"] = "interrupted"
                self.memory.put("action", record["id"], record)
                log.warning("action %s (%s) was interrupted", record["id"], record.get("kind"))

    def stop(self) -> None:
        """Stop the runtime. If ``run_forever`` is active this only requests
        the stop; the loop finishes its current step and stops cleanly."""
        with self._cond:
            if self._running:
                self.request_stop()
            else:
                self._transition({State.AWAKE, State.SLEEPING}, State.STOPPED, "stopped")

    def request_stop(self) -> None:
        """Ask a running loop to stop. Only sets a flag, so it is safe to call
        from any thread or a signal handler."""
        with self._cond:
            self._stop_requested = True
            self._cond.notify_all()

    def sleep(self, reason: str, wake_after: float | None = None) -> None:
        with self._cond:
            delay = wake_after if wake_after is not None else self.reassess_after
            self._wake_deadline = time.monotonic() + delay if delay is not None else None
            self._wake_at = time.time() + delay if delay is not None else None
            self._transition({State.AWAKE}, State.SLEEPING, reason, wake_at=self._wake_at)

    def wake(self, reason: str) -> None:
        with self._cond:
            self._wake_pending = None
            self._wake_deadline = None
            self._wake_at = None
            self._transition({State.SLEEPING}, State.AWAKE, reason)

    def request_wake(self, reason: str) -> bool:
        """Wake a sleeping runtime, or, if it is awake mid-cycle, make sure it
        reassesses once more instead of going to sleep. Thread-safe."""
        with self._cond:
            if self.state is State.SLEEPING:
                self.wake(reason)
                return True
            if self.state is State.AWAKE:
                self._wake_pending = reason
                return True
            return False

    def _transition(self, allowed: set[State], to: State, reason: str, **extra: Any) -> None:
        with self._cond:
            if self.state not in allowed:
                raise LifecycleError(f"cannot go from {self.state} to {to}")
            self.state = to
            self.reason = reason
            self._since = time.time()
            self.memory.put(
                "runtime", "lifecycle",
                {"state": to, "reason": reason, "at": time.time(), **extra},
            )
            log.info("%s: %s", to, reason)
            self._cond.notify_all()

    def wait_for(self, state: State, timeout: float | None = None) -> bool:
        """Block until the runtime is in ``state``. Returns False on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: self.state is state, timeout)

    def status(self) -> dict[str, Any]:
        """A snapshot of the runtime for local observers. No secrets."""
        with self._cond:
            snapshot = {
                "state": self.state,
                "reason": self.reason,
                "identity": self.identity.get("id"),
                "starts": self.identity.get("starts"),
                "running": self._running,
                "wake_at": self._wake_at if self.state is State.SLEEPING else None,
            }
        return {
            **snapshot,
            "directives": len(self.directives.active()),
            "open_todo": len(self.todo.open()),
            "cognition": getattr(self.cognition, "name", None),
        }

    # -- continuous operation ---------------------------------------------

    def run_forever(self) -> None:
        """Operate until stopped: cycle while awake, wait while asleep."""
        with self._cond:
            if self._running:
                raise LifecycleError("runtime is already running")
            self._running = True
        try:
            if self.state in (State.CREATED, State.STOPPED):
                self.start()
            elif self.state is State.SLEEPING:
                self.wake("run started")
            while not self._stop_requested:
                if self.state is State.AWAKE:
                    self.cycle()
                else:
                    self._sleep_until_woken()
        finally:
            with self._cond:
                self._running = False
                self._stop_requested = False
                if self.state in (State.AWAKE, State.SLEEPING):
                    self._transition({self.state}, State.STOPPED, "stopped")

    def _sleep_until_woken(self) -> None:
        with self._cond:
            if self._wake_pending is not None:
                self.wake(self._wake_pending)
                return
            while self.state is State.SLEEPING and not self._stop_requested:
                if self._wake_deadline is None:
                    self._cond.wait()
                    continue
                remaining = self._wake_deadline - time.monotonic()
                if remaining <= 0:
                    self.wake("reassessment due")
                    return
                self._cond.wait(remaining)

    # -- interaction -------------------------------------------------------

    def receive(self, text: str) -> Message:
        """A human message arrives. A sleeping runtime wakes to reassess."""
        message = self.chat.post(Sender.HUMAN, text)
        self.request_wake("message received")
        return message

    # -- cycle -------------------------------------------------------------

    def context(self) -> Context:
        """Gather the runtime state cognition's situation is built from. Only
        reads existing records; nothing here is persisted."""
        now = time.time()
        with self._cond:
            runtime: dict[str, Any] = {
                "identity": self.identity.get("id"),
                "born_at": self.identity.get("born_at"),  # None if never recorded
                "starts": self.identity.get("starts"),
                "state": self.state,
                "now": now,
                "state_since": self._since,
                "process_started_at": self._process_started_at,
                "cycles_this_process": self._cycles,
                "previous_process": self.previous,
                "default_reassess_after": self.reassess_after,
                "verifiers": sorted(self.verifiers),
            }
        try:
            observation = self.environment.observe()
        except Exception as exc:  # an unobservable host is a fact to report, not a crash
            log.warning("environment observation failed: %r", exc)
            observation = {}
            runtime["observation_error"] = f"observation failed: {type(exc).__name__}"
        unreadable: list[str] = []

        def read(name: str, fn: Any, default: Any) -> Any:
            # A corrupt record must not stop the cycle; the gap is reported instead.
            try:
                return fn()
            except Exception as exc:
                log.warning("could not read %s for context: %r", name, exc)
                unreadable.append(name)
                return default

        context = Context(
            environment=observation,
            directives=read("directives", self.directives.active, []),
            todo=read("todo", self.todo.open, []),
            messages=read("chat", lambda: self.chat.recent(LIMITS.messages), []),
            wake_reason=self.reason,
            recent_actions=read("actions",
                                lambda: self.memory.recent("action", LIMITS.actions), []),
            runtime=runtime,
            available_actions=self.environment.actions(),
            recent_cycles=read("cycles", lambda: self.memory.recent("cycle", LIMITS.cycles), []),
            done_todo=read("done_todo", lambda: sorted(
                self.todo.done(), key=lambda t: t.done_at or 0)[-LIMITS.done_todo:], []),
            counts={kind: self.memory.count(kind)
                    for kind in ("directive", "todo", "message", "action", "cycle", "work")},
            previous_observation=self.memory.get("runtime", "last_cycle"),
            **read("work", self._gather_work, {}),
        )
        runtime["unreadable_records"] = unreadable
        return context

    def _gather_work(self) -> dict[str, Any]:
        records = [r for r in self.memory.all("work") if isinstance(r, dict)]
        open_work = sorted((r for r in records if r.get("state") in OPEN),
                           key=lambda r: r.get("updated_at") or 0)[-LIMITS.work_open:]
        closed = sorted((r for r in records if r.get("state") in CLOSED),
                        key=lambda r: r.get("state_since") or 0)[-LIMITS.work_closed:]
        attempts = {r["id"]: self.work.attempts(r["id"], LIMITS.work_attempts)
                    for r in open_work if isinstance(r.get("id"), str)}
        return {"open_work": open_work, "closed_work": closed, "work_attempts": attempts}

    def cycle(self) -> CycleReport:
        with self._cond:
            if self.state is not State.AWAKE:
                raise LifecycleError(f"cycle requires {State.AWAKE}, runtime is {self.state}")
            # Anything that asked for a wake before now is covered by this cycle.
            self._wake_pending = None

        context = self.context()
        steps, note, cognition, sleep_reason, wake_after = self._decide_and_act(context)
        summary = {
            "at": time.time(),
            "wake_reason": context.wake_reason,
            "cognition": {"provider": getattr(self.cognition, "name", None), **cognition},
            "actions": [{"id": s.action.id, "kind": s.action.kind,
                         "outcome": s.verification.outcome} for s in steps],
            "state": State.SLEEPING if sleep_reason is not None else State.AWAKE,
            "note": note,
        }
        # The cycle is recorded before any sleep transition, so whoever sees the
        # runtime asleep also sees the cycle that led there. One small record per
        # cycle (the cognition log), plus the latest cycle with its observation.
        self.memory.put("cycle", uuid.uuid4().hex, redact(summary, limit=1000))
        self.memory.put("runtime", "last_cycle", redact({
            **summary, "observation": context.environment,
            "observed_at": context.runtime["now"]}, limit=1000))
        self._cycles += 1
        if sleep_reason is not None:
            self.sleep(sleep_reason, wake_after)
        return CycleReport(self.state, steps, note=note, cognition=cognition)

    def _decide_and_act(
        self, context: Context,
    ) -> tuple[list[Step], str, dict[str, Any], str | None, float | None]:
        """Returns (steps, note, cognition summary, sleep reason or None, wake_after)."""
        if self.cognition is None:
            reason = "no cognition provider configured"
            return [], reason, {"result": "none"}, reason, None

        started = time.monotonic()
        try:
            decision = self.cognition.decide(context)
            # Provider output is untrusted: a malformed decision is a cognition
            # failure, not something to half-execute.
            if not isinstance(decision, Decision):
                raise CognitionError("invalid_decision",
                                     f"decide() returned {type(decision).__name__}, not Decision")
            if not all(isinstance(a, Action) for a in decision.actions):
                raise CognitionError("invalid_decision",
                                     "Decision.actions must contain only Action instances")
            if not isinstance(decision.work, list):
                raise CognitionError("invalid_decision", "Decision.work must be a list")
        except Exception as exc:  # a failing provider must not take the runtime down
            category = getattr(exc, "category", "provider_error")
            if isinstance(exc, CognitionError):
                log.error("cognition failed (%s): %s", category, exc)
                detail = str(exc)
            else:
                log.exception("cognition provider failed")
                detail = repr(exc)
            reason = redact(f"cognition error ({category}): {detail}", limit=500)
            return [], reason, {"result": "failed", "failure": category,
                                "seconds": round(time.monotonic() - started, 3)}, reason, None

        summary = {
            "result": "decided",
            "seconds": round(time.monotonic() - started, 3),
            "sleep": decision.sleep,
            "wake_after": decision.wake_after,
            "requested": [a.kind for a in decision.actions],
            "replies": len(decision.replies),
            "meta": decision.meta,
        }
        log.info("cognition decided: %d action(s), %d reply(ies), %d work request(s), sleep=%s",
                 len(decision.actions), len(decision.replies), len(decision.work), decision.sleep)

        # Work requests first: new work can then be linked by this decision's
        # actions, and a completion can only cite results cognition has seen.
        work = self.work.apply(decision.work)
        rejected = list(work.rejected)
        steps = []
        for action in decision.actions:
            if self._stop_requested:  # stopping: take on no new work
                break
            try:
                work_id = self.work.resolve(action.work_id, work.refs)
            except WorkError as exc:  # still run it, but unlinked, and say so
                rejected.append({"op": "link", "target": action.work_id, "reason": str(exc)})
                work_id = None
            steps.append(self.act(dataclasses.replace(action, work_id=work_id)))
        if work.applied or rejected:
            summary["work"] = {"applied": work.applied, "rejected": rejected}
            if rejected:
                log.warning("work requests rejected: %s", [r["reason"] for r in rejected])
        for reply in decision.replies:
            self.chat.post(Sender.KAIRO, reply)
        sleep_reason = (redact(decision.reason or "cognition chose to sleep", limit=1000)
                        if decision.sleep else None)
        return steps, decision.reason, summary, sleep_reason, decision.wake_after

    def act(self, action: Action) -> Step:
        # The action is logged as started before it runs, so a crash mid-action
        # is visible after restart instead of being silently forgotten or replayed.
        # Records are redacted and bounded: output may contain secrets or be huge.
        record: dict[str, Any] = {**dataclasses.asdict(action), "status": "started",
                                  "started_at": time.time()}
        if action.work_id is not None:  # which strategy of that work this attempt belongs to
            work = self.work.get(action.work_id)
            record["strategy_revision"] = work.strategy_revision if work else None
        self.memory.put("action", action.id, redact(record, limit=STORED_STRING_LIMIT))
        # Neither a bad action nor a faulty verifier may take the runtime down.
        try:
            result = self.environment.execute(action)
        except Exception as exc:
            log.exception("executing action %s failed", action.id)
            result = ActionResult(action.id, executed=False, error=f"executor raised: {exc!r}")
        try:
            verification = verify(action, result, self.verifiers.get(action.kind))
        except Exception as exc:
            log.exception("verifying action %s failed", action.id)
            verification = Verification(Outcome.UNVERIFIABLE, f"verifier raised: {exc!r}")
        record.update(status="finished", finished_at=time.time(),
                      result=dataclasses.asdict(result),
                      verification=dataclasses.asdict(verification))
        self.memory.put("action", action.id, redact(record, limit=STORED_STRING_LIMIT))
        return Step(action, result, verification)
