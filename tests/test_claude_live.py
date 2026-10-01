"""Live smoke test against the real local Claude CLI. Opt-in; uses the model.

    KAIRO_LIVE_CLAUDE=1 [KAIRO_LIVE_MODEL=haiku] \
        PYTHONPATH=src python3 -m unittest discover -s tests -p test_claude_live.py -v
"""

import os
import shutil
import unittest

from kairo import Decision, Memory, Runtime
from kairo.claude import ClaudeCognition

LIVE = os.environ.get("KAIRO_LIVE_CLAUDE") == "1"


@unittest.skipUnless(LIVE, "set KAIRO_LIVE_CLAUDE=1 to run against the real Claude CLI")
class LiveClaudeSmokeTest(unittest.TestCase):
    def test_one_real_decision(self):
        self.assertIsNotNone(shutil.which("claude"), "claude CLI not on PATH")
        memory = Memory()
        self.addCleanup(memory.close)
        cognition = ClaudeCognition(model=os.environ.get("KAIRO_LIVE_MODEL", "haiku"),
                                    timeout=180)
        runtime = Runtime(memory, cognition=cognition)
        runtime.receive("Smoke test: please reply with one short sentence, then sleep.")
        runtime.start()

        decision = cognition.decide(runtime.context())

        self.assertIsInstance(decision, Decision)
        self.assertTrue(decision.replies, "expected a reply to the human message")
        for action in decision.actions:
            self.assertIn(action.kind, runtime.environment.actions())
        print(f"\nlive decision: sleep={decision.sleep} actions={len(decision.actions)} "
              f"replies={decision.replies!r} reason={decision.reason!r} meta={decision.meta}")

    def test_continues_existing_work_after_a_failed_attempt(self):
        from kairo import Action
        from kairo.work import OPEN

        memory = Memory()
        self.addCleanup(memory.close)
        cognition = ClaudeCognition(model=os.environ.get("KAIRO_LIVE_MODEL", "haiku"),
                                    timeout=180)
        runtime = Runtime(memory, cognition=cognition)
        runtime.start()
        d = runtime.directives.add("Keep this host healthy and tell the human about real problems.")
        refs = runtime.work.apply([
            {"op": "create", "ref": "disk", "objective": "Find out how large /var/log is",
             "why": "disk usage has been growing", "directive_id": d.id,
             "strategy": "use a disk-usage tool", "next_step": "measure /var/log"},
            {"op": "create", "ref": "done", "objective": "Check the hostname is set",
             "why": "operator asked", "directive_id": None, "strategy": "", "next_step": ""},
        ]).refs
        runtime.act(Action("process.run", {"argv": ["/usr/bin/diskusage-tool", "/var/log"]},
                           reason="measure /var/log", work_id=refs["disk"]))
        runtime.act(Action("process.run", {"argv": ["hostname"]}, reason="read hostname",
                           work_id=refs["done"]))
        evidence = runtime.work.attempts(refs["done"], 1)[0]["id"]
        runtime.work.apply([{"op": "set_state", "work_id": refs["done"], "state": "completed",
                             "reason": "hostname is set", "wait_seconds": None,
                             "evidence": [evidence]}])

        decision = cognition.decide(runtime.context())

        touched = {r.get("work_id") for r in decision.work} | {a.work_id for a in decision.actions}
        self.assertIn(refs["disk"], touched, "expected the existing work to be continued")
        self.assertNotIn(refs["done"], touched, "completed work must not be resumed")
        creates = [r for r in decision.work if r["op"] == "create"]
        self.assertFalse(any("/var/log" in r["objective"] for r in creates), "duplicated work")
        # The failure must be reassessed in the active work's own record: an update of
        # *that* work with a non-empty understanding (not just a linked action, and not
        # an update of some other work item).
        reassessed = [r for r in decision.work if r["op"] == "update"
                      and r["work_id"] == refs["disk"] and (r.get("understanding") or "").strip()]
        self.assertEqual(len(reassessed), 1, f"expected an understanding update: {decision.work}")
        outcome = runtime.work.apply(decision.work)  # validate as the runtime would
        self.assertEqual(outcome.rejected, [])
        work = runtime.work.get(refs["disk"])
        self.assertIn(work.state, OPEN)
        # Applied, it is persisted as the work's understanding, timestamped by the runtime.
        self.assertEqual(work.understanding, reassessed[0]["understanding"].strip())
        failed_at = runtime.work.attempts(refs["disk"], 1)[0]["finished_at"]
        self.assertGreater(work.understanding_at, failed_at)
        print(f"\nlive work decision: work={decision.work} actions="
              f"{[(a.params, a.work_id == refs['disk']) for a in decision.actions]} "
              f"reason={decision.reason!r}")

    def test_fallback_from_unavailable_claude_to_real_claude(self):
        """Real fallback: an unavailable Claude first, then a real Claude model,
        through a real runtime cycle."""
        from kairo.cognition import Cognition

        memory = Memory()
        self.addCleanup(memory.close)
        model = os.environ.get("KAIRO_LIVE_MODEL", "haiku")
        cognition = Cognition([
            ClaudeCognition(executable="/nonexistent/claude", name="claude"),
            ClaudeCognition(model=model, timeout=180, name=f"claude@{model}"),
        ])
        runtime = Runtime(memory, cognition=cognition)
        runtime.receive("Smoke test: please reply with one short sentence, then sleep.")
        runtime.start()
        with self.assertLogs("kairo", "ERROR"):  # the first provider's failure is logged
            runtime.cycle()

        cog = memory.all("cycle")[-1]["cognition"]
        self.assertEqual(cog["result"], "decided")
        self.assertEqual(cog["provider"], f"claude@{model}")
        self.assertEqual(cog["selection"], {"position": 1,
                                            "reason": "fallback_after:claude:unavailable"})
        self.assertEqual([(a["provider"], a["outcome"]) for a in cog["attempts"]],
                         [("claude", "unavailable"), (f"claude@{model}", "decided")])
        print(f"\nlive fallback: provider={cog['provider']} selection={cog['selection']} "
              f"meta={cog.get('meta')}")


if __name__ == "__main__":
    unittest.main()
