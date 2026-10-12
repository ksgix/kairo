"""A history shaped like Kairo's production database on 12 October 2026.

Measured there (read-only) on the situation of an idle timer wake: 120 actions, 330
cycles and 34 chat messages recorded; the situation showed the newest 15 actions
(commands of 700-1,200 characters, purposes of 300-480, long outputs), 10 cycles
whose assessments all reached the 600-character cap, and 8 messages (the longest
2,022, 1,575, 930, 384 and 375 characters) before the 60,000-character budget cut
the rest. One open work item and five closed ones. No content is copied from
production: only these shapes, with made-up text of the same sizes.
"""

import time
import uuid

from kairo import Memory

WORDS = ("check the release links and the dev tree before deciding whether anything needs "
         "doing; the survey covers upstream drift, CI status, services, journal and disk ")


def text(n, seed=""):
    """Made-up prose of exactly n characters."""
    s = (seed + " " + WORDS * (n // len(WORDS) + 2)).strip()
    return s[:n]


def script(n):
    """A shell command of about n characters, as Kairo writes them."""
    body = ("git -C /var/lib/kairo/dev fetch -q origin && git -C /var/lib/kairo/dev status -sb "
            "| head -3; systemctl is-active kairo kairo-dashboard; df -h / | tail -1; ")
    return ["sh", "-c", (body * (n // len(body) + 1))[:n]]


def seed(memory: Memory, now: float | None = None) -> None:
    now = now or time.time()
    t = now - 3 * 86400
    sizes = [1200, 1058, 701, 844, 743, 900, 760, 980, 820, 700, 1100, 760, 880, 940, 720]
    for i in range(120):
        t += 1800
        cmd = script(sizes[i % len(sizes)])
        memory.put("action", f"a{i:03d}", {
            "id": f"a{i:03d}", "kind": "process.run", "params": {"argv": cmd},
            "reason": text(300 + (i * 37) % 180, f"step {i}:"), "work_id": None,
            "status": "finished", "started_at": t, "finished_at": t + 3,
            "operation_key": f"a{i:03d}",
            "result": {"action_id": f"a{i:03d}", "executed": True, "error": None,
                       "failure": None, "external_outcome": None, "restart": False,
                       "output": {"returncode": 0, "stdout": text(3000 + i * 11, "out"),
                                  "stderr": text(200, "warn") if i % 4 == 0 else ""}},
            "verification": {"outcome": "unverifiable", "detail": "no verifier", "evidence": {}}})
        if i % 12 == 0:
            for j in range(30):  # cycles between actions: most are quiet timer wakes
                t += 600
                memory.put("cycle", f"c{i:03d}{j:02d}", {
                    "at": t, "wake_reason": "reassessment due", "state": "sleeping",
                    "actions": [], "note": text(700, f"assessment {i}.{j}:"),
                    "cognition": {"provider": "claude", "result": "decided", "sleep": True,
                                  "wake_after": 43200, "replies": 0, "requested": [],
                                  "seconds": 21.0, "meta": {"cost_usd": 0.25,
                                                            "situation_chars": 59850}}})
    lengths = [2022, 1575, 930, 384, 375, 240, 160, 120] + [300 + (k * 53) % 900 for k in range(26)]
    for k, n in enumerate(reversed(lengths)):
        memory.put("message", f"m{k:02d}", {"sender": "human" if k % 2 else "kairo",
                                             "text": text(n, f"message {k}:"),
                                             "at": now - 86400 + k * 600, "id": f"m{k:02d}"})
    base = {"why": text(600, "why:"), "strategy": text(900, "strategy:"), "strategy_revision": 1,
            "understanding": text(1800, "understanding:"), "next_step": text(300, "next:"),
            "evidence": [], "completion_basis": None, "history": [], "strategy_log": []}
    memory.put("work", "w-open", {**base, "id": "w-open", "objective": text(200, "objective:"),
                                  "state": "active", "state_reason": None,
                                  "created_at": now - 7200, "updated_at": now - 3600,
                                  "state_since": now - 7200})
    for k in range(5):
        memory.put("work", f"w-done{k}", {**base, "id": f"w-done{k}", "objective": text(180, f"done {k}:"),
                                          "state": "completed", "state_reason": text(400, "done:"),
                                          "completion_basis": "unverified",
                                          "created_at": now - 86400 * (k + 2),
                                          "updated_at": now - 86400 * (k + 1),
                                          "state_since": now - 86400 * (k + 1)})
    memory.put("directive", "d1", {"id": "d1", "statement": text(160, "statement:"), "active": False,
                                   "created_at": now - 7 * 86400, "origin": "operator",
                                   "history": [], "description": None})
    memory.put("runtime", "identity", {"id": uuid.uuid4().hex, "born_at": now - 7 * 86400,
                                       "starts": 12})
