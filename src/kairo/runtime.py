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
import traceback
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kairo import deploy
from kairo.actions import FAILED, Action, ActionResult, action_state, attempt_identity, failure_of
from kairo.chat import Chat, Message, Sender
from kairo.cognition import Cognition, CognitionProvider, Context, as_cognition
from kairo.directives import Directives
from kairo.environment import Environment
from kairo.memory import Memory
from kairo.redact import redact
from kairo.situation import LIMITS
from kairo.todo import Todo
from kairo.verification import Outcome, Verification, Verifier, verify
from kairo.work import ATTEMPT_SCAN, CLOSED, OPEN, WorkError, WorkLedger, WorkState

log = logging.getLogger("kairo")

STORED_STRING_LIMIT = 16_000  # characters per string in persisted action records
RECENT_DEPLOYMENTS = 5        # deployments shown in the code context
# Cognition failures that mean the provider answered but this code could not use
# the answer (Kairo's own request, parsing or adapter is broken). A just-deployed
# release that hits one before confirming itself exits so the supervisor can fall
# back; external failures (unavailable, timeout, auth, rate limit) do not count.
PROBATION_FAILURES = frozenset({"invalid_output", "invalid_decision", "provider_error"})


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
        cognition: Cognition | CognitionProvider | None = None,
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
        self._deadline_reason = "reassessment due"  # wake reason when the deadline passes
        self._since: float | None = None  # when the current state began
        self._process_started_at: float | None = None
        self._cycles = 0  # cycles completed by this process
        self._stop_reason = "stopped"
        # How the process should end: 0 normally, deploy.RESTART_EXIT after a
        # deployment switched releases, deploy.PROBATION_EXIT if a just-deployed
        # release proved unusable before confirming itself.
        self.exit_code = 0
        self._restart_for: str | None = None  # the deploy action that requested a restart
        self._awaiting_deploy: str | None = None  # our own deployment, not yet confirmed

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
            self._reconcile_deployments()

    def _start_reason(self) -> str:
        if self.state is State.STOPPED:
            return "restarted"
        previous = (self.previous or {}).get("state")
        if previous is None:
            return "first start"
        if previous == State.STOPPED:
            restart_for = (self.previous or {}).get("restart_for")
            if restart_for:
                return f"restarted after deployment {restart_for}"
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

    # -- deployment (kairo.deploy) --------------------------------------------

    def _deployment(self) -> Any:
        return getattr(self.environment, "deployment", None)

    def _reconcile_deployments(self) -> None:
        """At start: settle deployments the previous process left awaiting
        confirmation. Only this process can tell whether it is running the
        deployed revision. If it is, its confirmation is still to come (first
        usable cycle); if not, the candidate did not stay up (or was replaced),
        and the deployment failed."""
        deployment = self._deployment()
        if deployment is None:
            return
        pending = [r for r in self.memory.recent_where("action", "kind", deploy.KIND, 50)
                   if isinstance(r, dict) and deploy.awaiting_confirmation(r)]
        running = deployment.running_revision
        for record in pending:
            target = (record.get("result") or {}).get("output", {}).get("to")
            if record is pending[-1] and running is not None and running == target:
                self._awaiting_deploy = record["id"]
                self._note_deploy(record, "successor_started", {
                    "revision": running, "process_started_at": self._process_started_at,
                    "starts": self.identity.get("starts")})
                continue
            detail = (f"the restarted runtime is running {running or 'no release'}, not "
                      f"{target}: the candidate did not stay up or was replaced"
                      if record is pending[-1] else "superseded by a later deployment")
            self._settle_deploy(record, Outcome.FAILURE, detail,
                                {"stage": "confirmation", "running": running, "target": target})
            log.warning("deployment %s failed: %s", record.get("id"), detail)

    def _note_deploy(self, record: dict[str, Any], key: str, value: Any) -> None:
        verification = dict(record.get("verification") or {})
        evidence = dict(verification.get("evidence") or {})
        evidence[key] = value
        verification["evidence"] = evidence
        record["verification"] = verification
        self.memory.put("action", record["id"], redact(record, limit=STORED_STRING_LIMIT))

    def _settle_deploy(self, record: dict[str, Any], outcome: Any, detail: str,
                       evidence: dict[str, Any]) -> None:
        previous = (record.get("verification") or {}).get("evidence") or {}
        record["verification"] = {
            "outcome": outcome, "detail": detail,
            "evidence": {**{k: v for k, v in previous.items() if k != "awaiting"}, **evidence,
                         "settled_at": time.time()}}
        self.memory.put("action", record["id"], redact(record, limit=STORED_STRING_LIMIT))

    def _probation(self, cognition: dict[str, Any]) -> None:
        """After each cycle of a process that is the unconfirmed target of a
        deployment: confirm it once the full lifecycle path worked (a usable
        decision, or a completed cycle without cognition), or end the process if
        its own code made cognition unusable."""
        action_id = self._awaiting_deploy
        if action_id is None:
            return
        record = self.memory.get("action", action_id)
        if not isinstance(record, dict) or not deploy.awaiting_confirmation(record):
            self._awaiting_deploy = None
            return
        deployment = self._deployment()
        result = cognition.get("result")
        if result in ("decided", "none"):
            self._settle_deploy(record, Outcome.SUCCESS,
                                f"confirmed by the restarted runtime: running "
                                f"{deployment.running_revision}", {
                                    "stage": "confirmation",
                                    "revision": deployment.running_revision,
                                    "digest": deploy.content_digest(deployment.running),
                                    "process_started_at": self._process_started_at,
                                    "cycle_at": time.time(), "cognition": result})
            self._awaiting_deploy = None
            log.info("deployment %s confirmed", action_id)
        elif result == "failed" and cognition.get("failure") in PROBATION_FAILURES:
            self._note_deploy(record, "probation_failure", {
                "failure": cognition.get("failure"), "at": time.time(),
                "process_started_at": self._process_started_at})
            log.error("deployed release unusable (%s); exiting for the supervisor",
                      cognition.get("failure"))
            self.exit_code = deploy.PROBATION_EXIT
            self._stop_reason = f"probation failed: cognition {cognition.get('failure')}"
            self.request_stop()

    def _request_restart(self, action_id: str) -> None:
        """A deployment switched releases. Take on nothing new, let the cycle
        persist, stop, and exit with RESTART_EXIT so the supervisor starts it."""
        self._restart_for = action_id
        self.exit_code = deploy.RESTART_EXIT
        self._stop_reason = f"restart requested by deployment {action_id}"
        self.request_stop()

    def stop(self) -> None:
        """Stop the runtime. If ``run_forever`` is active this only requests
        the stop; the loop finishes its current step and stops cleanly."""
        with self._cond:
            if self._running:
                self.request_stop()
            else:
                self._transition({State.AWAKE, State.SLEEPING}, State.STOPPED, self._stop_reason,
                                 **self._stop_extra())

    def _stop_extra(self) -> dict[str, Any]:
        return {"restart_for": self._restart_for} if self._restart_for else {}

    def request_stop(self) -> None:
        """Ask a running loop to stop. Only sets a flag, so it is safe to call
        from any thread or a signal handler."""
        with self._cond:
            self._stop_requested = True
            self._cond.notify_all()

    def sleep(self, reason: str, wake_after: float | None = None) -> None:
        with self._cond:
            now = time.time()
            delay = wake_after if wake_after is not None else self.reassess_after
            at = now + delay if delay is not None else None
            self._deadline_reason = "reassessment due"
            # Waiting work is reassessed when its wait runs out. Only future
            # deadlines count: an elapsed wait is already visible to cognition.
            waiting = self._next_wait(now)
            if waiting is not None and (at is None or waiting[0] < at):
                at = waiting[0]
                self._deadline_reason = f"wait elapsed for work {waiting[1]}"
            self._wake_deadline = time.monotonic() + (at - now) if at is not None else None
            self._wake_at = at
            self._transition({State.AWAKE}, State.SLEEPING, reason, wake_at=self._wake_at)

    def _next_wait(self, now: float) -> tuple[float, str] | None:
        try:
            waits = [(w.waiting_until, w.id) for w in self.work.open()
                     if w.state == WorkState.WAITING and isinstance(w.waiting_until, (int, float))
                     and not isinstance(w.waiting_until, bool) and w.waiting_until > now]
        except Exception:  # unreadable work must not prevent sleeping
            log.warning("could not read waiting work for the wake deadline")
            return None
        return min(waits) if waits else None

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
        cognition = as_cognition(self.cognition)
        last = (self.memory.get("runtime", "last_cycle") or {}).get("cognition") or {}
        return {
            **snapshot,
            "directives": len(self.directives.active()),
            "open_todo": len(self.todo.open()),
            # Configured implementations, derived from the filesystem (no guidance).
            "implementations": [
                {"id": i["id"], "state": i["state"], "reason": i["reason"],
                 "digest": (i.get("digest") or "")[:12] or None, "tools": len(i.get("tools") or [])}
                for i in self._implementations_view()],
            # The configured provider order (configuration, comma-separated).
            "cognition": ",".join(cognition.names) if cognition else None,
            # Which provider made the last cycle's decision, and why it was asked.
            "cognition_last": {"provider": last.get("provider"),
                               "selection": last.get("selection")} if last else None,
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
                    self._transition({self.state}, State.STOPPED, self._stop_reason,
                                     **self._stop_extra())

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
                    self.wake(self._deadline_reason)
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
                "verifiers": sorted(set(self.verifiers) | self._environment_verified()),
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
            implementations=read("implementations", self._implementations_view, []),
            code=read("code", self._gather_code, {}),
        )
        runtime["unreadable_records"] = unreadable
        return context

    def _gather_code(self) -> dict[str, Any]:
        """Runtime facts about Kairo's own code, when deployment is configured:
        the running release (authoritative), the release links, the development
        repository, and the latest deployments with their derived states."""
        code_facts = getattr(self.environment, "code_facts", None)
        facts = code_facts() if code_facts else {}
        if not facts:
            return {}
        deployments = []
        latest_for_running = None
        for r in self.memory.recent_where("action", "kind", deploy.KIND, RECENT_DEPLOYMENTS):
            output = (r.get("result") or {}).get("output") or {}
            evidence = (r.get("verification") or {}).get("evidence") or {}
            deployments.append({"action_id": r.get("id"), "from": output.get("from"),
                                "to": output.get("to"), "state": action_state(r),
                                "stage": evidence.get("stage") or output.get("stage"),
                                "at": r.get("finished_at") or r.get("started_at")})
            if output.get("to") == (facts.get("running") or {}).get("revision"):
                latest_for_running = deployments[-1]
        running = facts.get("running") or {}
        if running.get("revision"):
            running["since"] = self._process_started_at
            running["status"] = (
                "operator_selected" if latest_for_running is None
                else {"verified_successful": "confirmed"}.get(latest_for_running["state"],
                                                              latest_for_running["state"]))
        facts["deployments"] = deployments
        return facts

    def _implementations_view(self) -> list[dict[str, Any]]:
        view = getattr(self.environment, "implementations_view", None)
        return view() if view else []

    def _environment_verified(self) -> set[str]:
        kinds = getattr(self.environment, "verified_kinds", None)
        try:
            return set(kinds()) if kinds else set()
        except Exception:  # an unreadable catalog must not stop context gathering
            log.warning("could not read implementation verifiers")
            return set()

    def _gather_work(self) -> dict[str, Any]:
        records = [r for r in self.memory.all("work") if isinstance(r, dict)]
        open_work = sorted((r for r in records if r.get("state") in OPEN),
                           key=lambda r: r.get("updated_at") or 0)[-LIMITS.work_open:]
        closed = sorted((r for r in records if r.get("state") in CLOSED),
                        key=lambda r: r.get("state_since") or 0)[-LIMITS.work_closed:]
        attempts, logs = {}, {}
        for r in open_work:
            if not isinstance(r.get("id"), str):
                continue
            scanned = self.work.attempts(r["id"], ATTEMPT_SCAN)
            attempts[r["id"]] = scanned[-LIMITS.work_attempts:]
            logs[r["id"]] = [summary for a in scanned if (summary := _attempt_summary(a))]
        return {"open_work": open_work, "closed_work": closed, "work_attempts": attempts,
                "work_attempt_log": logs}

    def cycle(self) -> CycleReport:
        with self._cond:
            if self.state is not State.AWAKE:
                raise LifecycleError(f"cycle requires {State.AWAKE}, runtime is {self.state}")
            # Anything that asked for a wake before now is covered by this cycle.
            self._wake_pending = None

        context = self.context()
        steps, note, cognition, sleep_reason, wake_after = self._decide_and_act(context)
        if self._restart_for is not None:  # a deployment switched releases: no sleep, exit
            sleep_reason = None
        summary = {
            "at": time.time(),
            "wake_reason": context.wake_reason,
            "cognition": {"provider": None, **cognition},
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
        self._probation(cognition)
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

        # Which provider is asked, and fallback, belong to the cognition layer;
        # the runtime only receives at most one validated Decision.
        started = time.monotonic()
        try:
            result = as_cognition(self.cognition).decide(context)
        except Exception as exc:  # the cognition layer itself failed: still not fatal
            log.error("cognition failed: %s", redact(traceback.format_exc(), limit=4000))
            reason = redact(f"cognition error (provider_error): {exc!r}", limit=500)
            return [], reason, {"result": "failed", "failure": "provider_error",
                                "seconds": round(time.monotonic() - started, 3)}, reason, None
        provenance = result.summary()
        decision = result.decision
        if decision is None:  # every provider asked failed: a cycle-level cognition failure
            reason = redact(f"cognition error ({result.failure}): {result.attempts[-1].detail}",
                            limit=500)
            return [], reason, {"result": "failed", "failure": result.failure,
                                "seconds": round(time.monotonic() - started, 3),
                                **provenance}, reason, None

        summary = {
            "result": "decided",
            "seconds": round(time.monotonic() - started, 3),
            **provenance,
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
            if work_id is not None:
                # No blind repetition: an exact repeat (compared as it would be stored,
                # so redacted) of an attempt that failed or was interrupted since the
                # work's understanding last changed is refused, not executed.
                identity = attempt_identity(action.kind,
                                            redact(action.params, limit=STORED_STRING_LIMIT))
                earlier = self.work.unsettled_repeat(work_id, identity)
                if earlier is not None:
                    rejected.append({
                        "op": "action_refused", "target": work_id, "repeats": earlier["action_id"],
                        "reason": (f"identical to attempt {earlier['action_id']} "
                                   f"({earlier['state']}) made since this work's understanding "
                                   "last changed; update the understanding before repeating it")})
                    continue
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
        provenance = getattr(self.environment, "provenance", None)
        if provenance and (which := provenance(action)):  # implementation content to be run
            record["implementation"] = which
        self.memory.put("action", action.id, redact(record, limit=STORED_STRING_LIMIT))
        # Neither a bad action nor a faulty verifier may take the runtime down.
        try:
            result = self.environment.execute(action)
        except Exception as exc:
            log.exception("executing action %s failed", action.id)
            result = ActionResult(action.id, executed=False, error=f"executor raised: {exc!r}",
                                  failure="executor_error")
        try:
            verifier = self.verifiers.get(action.kind)
            if verifier is None and hasattr(self.environment, "verifier"):
                verifier = self.environment.verifier(action.kind)  # e.g. an implementation's
            verification = verify(action, result, verifier)
        except Exception as exc:
            log.exception("verifying action %s failed", action.id)
            verification = Verification(Outcome.UNVERIFIABLE, f"verifier raised: {exc!r}")
        result_record = dataclasses.asdict(result)
        if ran := result_record.pop("implementation", None):  # what actually ran
            record["implementation"] = ran
        record.update(status="finished", finished_at=time.time(), result=result_record,
                      verification=dataclasses.asdict(verification))
        self.memory.put("action", action.id, redact(record, limit=STORED_STRING_LIMIT))
        # Only after the record is durable: a restart can never orphan the deploy.
        if result.restart and action.kind == deploy.KIND:
            self._request_restart(action.id)
        return Step(action, result, verification)


def _attempt_summary(record: Any) -> dict[str, Any] | None:
    """A compact view of one attempt, for deriving recovery facts over many.
    A malformed record is skipped (None), never allowed to break the context."""
    try:
        return _summarise_attempt(record)
    except Exception:
        return None


def _summarise_attempt(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    state = action_state(record)
    detail = None
    if state in FAILED:
        text = result.get("error") or output.get("stderr") or ""
        detail = text[:300] if isinstance(text, str) else None
    return {"id": record.get("id"), "strategy_revision": record.get("strategy_revision"),
            "state": state, "failure": failure_of(record), "returncode": output.get("returncode"),
            "identity": attempt_identity(record.get("kind"), record.get("params")),
            "at": record.get("finished_at") or record.get("started_at"), "detail": detail}
