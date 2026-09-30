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
from kairo.cognition import CognitionProvider, Context, Decision
from kairo.directives import Directives
from kairo.environment import Environment
from kairo.memory import Memory
from kairo.todo import Todo
from kairo.verification import Outcome, Verification, Verifier, verify

log = logging.getLogger("kairo")

CONTEXT_MESSAGES = 20
CONTEXT_ACTIONS = 20


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
        self.state = State.CREATED
        self.reason = ""
        # What the previous process left behind, if anything.
        self.previous: dict[str, Any] | None = memory.get("runtime", "lifecycle")
        self.identity: dict[str, Any] = memory.get("runtime", "identity") or {
            "id": uuid.uuid4().hex,
            "born_at": time.time(),
            "starts": 0,
        }

        # All lifecycle state changes happen under this condition, and every
        # change notifies it, so any thread can wait for or cause a transition.
        self._cond = threading.Condition(threading.RLock())
        self._running = False
        self._stop_requested = False
        self._wake_pending: str | None = None
        self._wake_deadline: float | None = None  # time.monotonic() value
        self._wake_at: float | None = None  # the same deadline as wall-clock time

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._cond:
            if self.state not in (State.CREATED, State.STOPPED):
                raise LifecycleError(f"cannot go from {self.state} to {State.AWAKE}")
            self._transition({self.state}, State.AWAKE, self._start_reason())
            self.identity["starts"] += 1
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
            if record["status"] == "started":
                record["status"] = "interrupted"
                self.memory.put("action", record["id"], record)
                log.warning("action %s (%s) was interrupted", record["id"], record["kind"])

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
                "identity": self.identity["id"],
                "starts": self.identity["starts"],
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
        return Context(
            environment=self.environment.observe(),
            directives=self.directives.active(),
            todo=self.todo.open(),
            messages=self.chat.recent(CONTEXT_MESSAGES),
            wake_reason=self.reason,
            recent_actions=self.memory.recent("action", CONTEXT_ACTIONS),
        )

    def cycle(self) -> CycleReport:
        with self._cond:
            if self.state is not State.AWAKE:
                raise LifecycleError(f"cycle requires {State.AWAKE}, runtime is {self.state}")
            # Anything that asked for a wake before now is covered by this cycle.
            self._wake_pending = None

        context = self.context()
        report = self._decide_and_act(context)
        self.memory.put("runtime", "last_cycle", {
            "at": time.time(),
            "wake_reason": context.wake_reason,
            "observation": context.environment,
            "cognition": getattr(self.cognition, "name", None),
            "actions": [s.action.id for s in report.steps],
            "state": report.state,
            "note": report.note,
        })
        return report

    def _decide_and_act(self, context: Context) -> CycleReport:
        if self.cognition is None:
            self.sleep("no cognition provider configured")
            return CycleReport(self.state, note=self.reason)

        try:
            decision = self.cognition.decide(context)
            # Provider output is untrusted: a malformed decision is a cognition
            # failure, not something to half-execute.
            if not isinstance(decision, Decision):
                raise TypeError(f"decide() returned {type(decision).__name__}, not Decision")
            if not all(isinstance(a, Action) for a in decision.actions):
                raise TypeError("Decision.actions must contain only Action instances")
        except Exception as exc:  # a failing provider must not take the runtime down
            log.exception("cognition provider failed")
            self.sleep(f"cognition error: {exc!r}")
            return CycleReport(self.state, note=self.reason)

        steps = []
        for action in decision.actions:
            if self._stop_requested:  # stopping: take on no new work
                break
            steps.append(self.act(action))
        for reply in decision.replies:
            self.chat.post(Sender.KAIRO, reply)
        if decision.sleep:
            self.sleep(decision.reason or "cognition chose to sleep", decision.wake_after)
        return CycleReport(self.state, steps, note=decision.reason)

    def act(self, action: Action) -> Step:
        # The action is logged as started before it runs, so a crash mid-action
        # is visible after restart instead of being silently forgotten or replayed.
        record: dict[str, Any] = {**dataclasses.asdict(action), "status": "started",
                                  "started_at": time.time()}
        self.memory.put("action", action.id, record)
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
        self.memory.put("action", action.id, record)
        return Step(action, result, verification)
