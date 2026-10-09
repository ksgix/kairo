"""History: read-only views of the runtime's own records, for the operator.

The situation is what cognition is shown now: bounded, and built for deciding.
These two views are for whoever watches Kairo over a longer time:

    metrics    totals: cycles, model calls and their reported cost per day, the
               size of the last context sent, work by state, when the runtime was
               running, and the latest deployments
    activity   what happened, newest first, in pages: cycles, actions (with a
               deployment's stages) and process starts and stops

Both only read records the runtime already wrote; nothing here is persisted,
shown to cognition, or decided. Everything read out is redacted and bounded,
like every other read. Text keeps its origin: an assessment and a purpose are
cognition's words, output is untrusted program content, the rest are runtime facts.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import PurePath
from typing import Any

from kairo.actions import action_state, failure_of
from kairo.memory import Memory
from kairo.redact import head_tail, redact
from kairo.situation import _content, _external

DEPLOY_KIND = "runtime.deploy"  # kairo.deploy.KIND (not imported: deploy imports the runtime's parts)
PROCESS = "process"       # the record kind of process starts and stops (written by the runtime)
DAY = 86400
METRIC_DAYS = 14          # days of totals, the current (UTC) day included
METRIC_CYCLES = 20_000    # newest cycle records examined for the totals
PROCESS_EVENTS = 400      # newest process starts and stops examined for the timeline
DEPLOYMENTS = 20          # latest deployments listed
ACTIVITY_PAGE = 100       # items in one activity read
TEXT = 600                # characters of an assessment, purpose, request or error
OUTPUT = 1500             # characters of program output per stream
STAGE_SUMMARY = 300       # characters of one deployment stage's summary
ACTIVITY_KINDS = ("cycle", "action", PROCESS)


def metrics(memory: Memory, now: float, budget: int) -> dict[str, Any]:
    return redact({
        "now": now,
        **_days(memory, now),
        "last_context": _last_context(memory, budget),
        "work": _work(memory),
        "running": _running(memory),
        "deployments": _deployments(memory),
    })


def activity(memory: Memory, limit: int, before: int | None = None) -> dict[str, Any]:
    rows, more = memory.stream(ACTIVITY_KINDS, min(limit, ACTIVITY_PAGE), before)
    items = []
    for seq, kind, data in rows:
        try:
            items.append({"seq": seq, **_ITEM[kind](data)})
        except Exception:  # one malformed record must not hide the others
            items.append({"seq": seq, "type": kind, "unreadable": True})
    return redact({"items": items, "more_before": more,
                   "next_before": rows[-1][0] if rows and more else None})


# -- metrics ---------------------------------------------------------------------


def _days(memory: Memory, now: float) -> dict[str, Any]:
    """Per UTC day: cycles, those in which a model was asked (decided or failed),
    failures by kind, and the cost the provider reported for the decided ones."""
    first = int(now // DAY) - (METRIC_DAYS - 1)
    rows = memory.fields("cycle", ("at", "cognition.result", "cognition.failure",
                                   "cognition.meta.cost_usd"), METRIC_CYCLES)
    days: dict[int, dict[str, Any]] = {}
    covered = len(rows) < METRIC_CYCLES
    for at, result, failure, cost in rows:
        if not _is_number(at):
            continue
        index = int(at // DAY)
        if index < first:
            covered = True  # newest first: everything older is outside the window
            break
        day = days.setdefault(index, {"cycles": 0, "decided": 0, "failed": 0, "failures": {},
                                      "cost_usd": None, "costed": 0})
        day["cycles"] += 1
        if result == "decided":
            day["decided"] += 1
        elif result == "failed":
            day["failed"] += 1
            kind = failure if isinstance(failure, str) else "unrecorded"
            day["failures"][kind] = day["failures"].get(kind, 0) + 1
        if _is_number(cost):
            day["cost_usd"] = round((day["cost_usd"] or 0.0) + cost, 6)
            day["costed"] += 1
    today = int(now // DAY)
    start = min(days) if days else today
    empty = {"cycles": 0, "decided": 0, "failed": 0, "failures": {}, "cost_usd": None,
             "costed": 0}
    return {
        "days": [{"day": time.strftime("%Y-%m-%d", time.gmtime(i * DAY)),
                  **days.get(i, empty), "calls": days.get(i, empty)["decided"]
                  + days.get(i, empty)["failed"]} for i in range(start, today + 1)],
        # False: more cycles fall inside the window than were examined.
        "days_complete": covered,
        "days_note": ("UTC days; calls: cycles in which a model was asked; cost_usd: as "
                      "reported by the provider for the cycles that reported one (costed)"),
    }


def _last_context(memory: Memory, budget: int) -> dict[str, Any] | None:
    """The size of the last context a provider reported receiving."""
    for at, chars in memory.fields("cycle", ("at", "cognition.meta.situation_chars"), 50):
        if _is_number(chars):
            return {"at": at if _is_number(at) else None, "chars": chars, "budget": budget}
    return None


def _work(memory: Memory) -> dict[str, Any]:
    states: dict[str, int] = {}
    basis: dict[str, int] = {}
    for record in memory.all("work"):
        if not isinstance(record, dict):
            continue
        state = record.get("state") if isinstance(record.get("state"), str) else "unknown"
        states[state] = states.get(state, 0) + 1
        if state == "completed":
            b = record.get("completion_basis")
            b = b if b in ("verified", "checked", "unverified") else "unknown"
            basis[b] = basis.get(b, 0) + 1
    return {"by_state": states, "completed_by_basis": basis}


def _running(memory: Memory) -> dict[str, Any]:
    """When the runtime was running, from its recorded starts and stops (oldest
    first). A start that follows a start means the earlier process ended without
    recording a stop (killed, crashed, power lost): its end is unknown, and the
    last cycle or action it recorded is the latest moment it is known to have run."""
    rows, more = memory.stream((PROCESS,), PROCESS_EVENTS)
    spans: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for seq, _, event in reversed(rows):
        if not isinstance(event, dict) or not _is_number(event.get("at")):
            continue
        if event.get("event") == "started":
            if current is not None:
                spans.append({**current, "to": None, "end": "unrecorded",
                              "last_record_at": _last_record_before(memory, seq)})
            current = {"from": event["at"], "revision": event.get("revision"),
                       "start_reason": event.get("reason")}
        elif event.get("event") == "stopped" and current is not None:
            spans.append({**current, "to": event["at"], "end": "stopped",
                          "stop_reason": event.get("reason"),
                          "exit_code": event.get("exit_code")})
            current = None
    if current is not None:  # this process: metrics are computed by the live runtime
        spans.append({**current, "to": None, "end": "running"})
    return {"spans": spans, "recorded_since": spans[0]["from"] if spans else None,
            "omitted_older": more,
            "note": "starts and stops are recorded from the release that added this record on"}


def _last_record_before(memory: Memory, seq: int) -> float | None:
    rows, _ = memory.stream(("cycle", "action"), 1, before=seq)
    if not rows:
        return None
    record = rows[0][2]
    at = record.get("at") or record.get("finished_at") or record.get("started_at")
    return at if _is_number(at) else None


def _deployments(memory: Memory) -> list[dict[str, Any]]:
    out = []
    for r in memory.recent_where("action", "kind", DEPLOY_KIND, DEPLOYMENTS):
        if not isinstance(r, dict):
            continue
        output = (r.get("result") or {}).get("output") or {}
        out.append({"action_id": r.get("id"), "at": r.get("finished_at") or r.get("started_at"),
                    "from": output.get("from"), "to": output.get("to"),
                    "state": action_state(r)})
    return out


# -- activity --------------------------------------------------------------------


def _cycle(rec: dict[str, Any]) -> dict[str, Any]:
    cog = rec.get("cognition") if isinstance(rec.get("cognition"), dict) else {}
    meta = cog.get("meta") if isinstance(cog.get("meta"), dict) else {}
    work = cog.get("work") if isinstance(cog.get("work"), dict) else {}
    item: dict[str, Any] = {
        "type": "cycle", "at": rec.get("at"), "wake_reason": _cut(rec.get("wake_reason")),
        "result": cog.get("result"), "provider": cog.get("provider"),
        "ended_in_state": rec.get("state"), "seconds": cog.get("seconds"),
        "actions": [{"id": a.get("id"), "kind": a.get("kind"), "outcome": a.get("outcome")}
                    for a in rec.get("actions") or [] if isinstance(a, dict)],
    }
    if cog.get("result") == "failed":
        item.update(failure=cog.get("failure"), failure_detail=_cut(rec.get("note")),
                    consecutive_failures=cog.get("consecutive_failures"),
                    retry_after_seconds=cog.get("retry_after"))
    elif cog.get("result") == "decided":
        item.update(
            assessment=_cut(rec.get("note")),  # cognition's words
            replies=cog.get("replies"), chose_sleep=cog.get("sleep"),
            wake_after_seconds=cog.get("wake_after"),
            work_applied=len(work.get("applied") or []),
            work_rejected=len(work.get("rejected") or []),
            models=meta.get("models"), cost_usd=meta.get("cost_usd"),
            situation_chars=meta.get("situation_chars"),
            rested_by_runtime=cog.get("forced_rest"))
    return item


def _action(rec: dict[str, Any]) -> dict[str, Any]:
    result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    verification = rec.get("verification") if isinstance(rec.get("verification"), dict) else {}
    state = action_state(rec)
    item: dict[str, Any] = {
        "type": "action", "at": rec.get("started_at"), "finished_at": rec.get("finished_at"),
        "id": rec.get("id"), "kind": rec.get("kind"), "state": state,
        "failure": failure_of(rec), "returncode": output.get("returncode"),
        "purpose": _cut(rec.get("reason")),  # cognition's words
        "request": _request(rec.get("kind"), rec.get("params")),
        "work_id": rec.get("work_id"), **_external(rec, result),
        "output": _content(rec, output, OUTPUT),  # untrusted program content
        "error": _cut(result.get("error")),
    }
    if verification.get("outcome") in ("success", "failure") or state == "awaiting_confirmation":
        item["verification"] = {"outcome": verification.get("outcome"),
                                "detail": _cut(verification.get("detail"))}
    if rec.get("kind") == DEPLOY_KIND:
        item["deploy"] = _deploy(output, verification)
    return item


def _deploy(output: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    """A deployment's own account: each stage with its result. A stage's summary is
    the end of what the test run printed (untrusted program content)."""
    evidence = verification.get("evidence") if isinstance(verification.get("evidence"), dict) \
        else {}
    snapshot = output.get("snapshot")
    return {
        "from": output.get("from"), "to": output.get("to"),
        "switched": output.get("switched"), "stage": evidence.get("stage") or output.get("stage"),
        "error": _cut(output.get("error")),
        "files_changed": output.get("files_changed"),
        "trust_critical_changed": output.get("trust_critical_changed"),
        "snapshot": PurePath(snapshot).name if isinstance(snapshot, str) else None,
        "settled_at": evidence.get("settled_at"),
        "stages": [{"stage": s.get("stage"), "role": s.get("role"), "passed": s.get("passed"),
                    "tests_run": s.get("tests_run"), "skipped": s.get("skipped"),
                    "returncode": s.get("returncode"),
                    "summary": head_tail(s.get("summary"), STAGE_SUMMARY)}
                   for s in output.get("stages") or [] if isinstance(s, dict)],
    }


def _process(rec: dict[str, Any]) -> dict[str, Any]:
    return {"type": PROCESS, "at": rec.get("at"), "event": rec.get("event"),
            "reason": _cut(rec.get("reason")), "revision": rec.get("revision"),
            "starts": rec.get("starts"), "exit_code": rec.get("exit_code")}


_ITEM = {"cycle": _cycle, "action": _action, PROCESS: _process}


def _request(kind: Any, params: Any) -> str | None:
    """What was asked for, on one line: the command of a process.run, otherwise the
    parameters as JSON. Cut in the middle if long."""
    if params in (None, {}):
        return None
    argv = params.get("argv") if isinstance(params, dict) else None
    if kind == "process.run" and isinstance(argv, list) and all(isinstance(a, str) for a in argv):
        text = shlex.join(argv)
    else:
        text = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return head_tail(text, TEXT)


def _cut(text: Any) -> Any:
    return head_tail(text, TEXT) if isinstance(text, str) else text


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
