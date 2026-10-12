"""The size of what Kairo sends its model, measured on a history shaped like production.

On 12 October 2026 every wake in production sent about 60,000 characters (the
budget), even a quiet timer wake at which nothing had happened: old commands, old
output and old assessments filled it. History is now shown shorter as it ages (new
results in full, the newest others cut to a few hundred characters, older ones to
less) and the situation is rendered without indentation. These tests pin both the
saving and what must not be lost with it.
"""

import dataclasses
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kairo import Action, Decision, Memory, Runtime
from kairo.chat import Sender
from kairo.redact import MARKER, protect_env
from kairo.situation import LIMITS, STAGE_PRINTED, Limits, build_situation, render_situation
from production_shape import seed
from test_continuous import Cognition
from test_deploy import DeployCase

# The limits as they were before history was shortened with age.
BEFORE = Limits(messages=20, actions=15, cycles=10, messages_full=20, actions_recent=15,
                action_recent=10**6, cycles_full=10, action_older=10**6, assessment_older=600,
                message_older=10**6)


class ProductionShapeTest(unittest.TestCase):
    def setUp(self):
        self.memory = Memory()
        self.addCleanup(self.memory.close)
        seed(self.memory)
        self.runtime = Runtime(self.memory)
        self.runtime.start()
        self.runtime.cycle()  # a quiet timer wake: nothing new since the last decision

    def test_a_quiet_wake_fits_well_inside_the_budget(self):
        context = self.runtime.context()
        now = build_situation(context)
        # Before: as many items as the runtime used to gather, shown whole.
        old_context = dataclasses.replace(
            context, recent_actions=self.memory.recent("action", 15),
            messages=self.runtime.chat.recent(20), recent_cycles=self.memory.recent("cycle", 10))
        before = build_situation(old_context, BEFORE)
        size, old_size = len(render_situation(now)), len(render_situation(before))
        # It used to fill the budget: it had to drop items to fit. (How close to the
        # budget it ends depends on the size of the item dropped last, and on the host:
        # the observation includes the working directory.)
        self.assertGreater(before["context"]["trimmed_for_budget"], 0)
        self.assertGreater(old_size, 0.9 * LIMITS.budget)
        self.assertLess(size, 0.65 * LIMITS.budget)           # now well inside it
        self.assertLess(size, 0.65 * old_size)
        self.assertEqual(now["context"]["trimmed_for_budget"], 0)  # and nothing is dropped

    def test_shortening_keeps_every_fact_and_marks_every_cut(self):
        s = build_situation(self.runtime.context())
        actions = s["history"]["actions"]["items"]
        self.assertEqual(len(actions), LIMITS.actions)
        self.assertEqual(s["history"]["actions"]["omitted_older"], 120 - LIMITS.actions)
        for a in actions:  # the runtime's facts are whole
            self.assertTrue({"id", "kind", "state", "failure", "returncode", "requested",
                             "finished"} <= set(a))
            self.assertEqual((a["state"], a["returncode"]), ("executed_unverified", 0))
        recent, older = actions[-LIMITS.actions_recent:], actions[:-LIMITS.actions_recent]
        for a in recent:
            self.assertLessEqual(len(a["purpose"]), LIMITS.action_recent + 40)
            self.assertLessEqual(len(a["output"]["stdout"]), LIMITS.action_output_old)
        for a in older:
            self.assertLessEqual(len(a["params"]["argv"][2]), LIMITS.action_older + 40)
            self.assertLessEqual(len(a["output"]["stdout"]), LIMITS.action_older)
            self.assertIn("[truncated", a["params"]["argv"][2])  # marked, not silently cut
            self.assertIn("[truncated", a["output"]["stdout"])
        cycles = s["history"]["cycles"]["items"]
        self.assertEqual(len(cycles), LIMITS.cycles)
        for c in cycles[:-LIMITS.cycles_full]:
            self.assertLessEqual(len(c["assessment"]), LIMITS.assessment_older + 40)
        for c in cycles[-LIMITS.cycles_full:]:  # (the newest is this test's own, without a model)
            if "assessment" in c:
                self.assertGreater(len(c["assessment"]), LIMITS.assessment_older + 40)
        chat = s["history"]["chat"]["items"]
        self.assertEqual(len(chat), LIMITS.messages)
        self.assertTrue(all(len(m["text"]) <= LIMITS.message_older + 40
                            for m in chat[:-LIMITS.messages_full]))
        # Work is never shortened by age: its understanding is Kairo's long memory.
        [w] = s["work"]["open"]
        self.assertEqual(len(w["understanding"]), 1800)


class NewResultsTest(unittest.TestCase):
    def test_new_results_are_shown_in_full_and_shorten_once_seen(self):
        printed = [f"{i}" * 5000 for i in range(3)]
        steps = [Decision(actions=[Action("process.run", {"argv": [sys.executable, "-c", f"print('{p[0]}' * 5000)"]},
                                          reason=f"look {i}") for i, p in enumerate(printed)],
                          sleep=False, reason="looking"),
                 Decision(sleep=False, reason="read them"),
                 Decision(sleep=True, reason="done")]
        cognition = Cognition(lambda context, n: steps[min(n, len(steps)) - 1])
        memory = Memory()
        self.addCleanup(memory.close)
        runtime = Runtime(memory, cognition=cognition)
        runtime.start()
        for _ in range(3):
            runtime.cycle()
        seen_new = build_situation(cognition.contexts[1])["history"]["actions"]["items"]
        self.assertEqual([len(a["output"]["stdout"]) for a in seen_new], [5001] * 3)  # whole
        self.assertEqual([a["purpose"] for a in seen_new], ["look 0", "look 1", "look 2"])
        seen_again = build_situation(cognition.contexts[2])["history"]["actions"]["items"]
        self.assertTrue(all(len(a["output"]["stdout"]) <= LIMITS.action_output_old
                            for a in seen_again))  # no longer new: shown short


class ChatSecretTest(unittest.TestCase):
    def test_an_older_message_is_redacted_before_it_is_cut(self):
        secret = "chat-secret-0123456789abcdefXYZ"
        protect_env(["KAIRO_CONTEXT_TEST_SECRET"])
        memory = Memory()
        self.addCleanup(memory.close)
        runtime = Runtime(memory)
        # The secret straddles the place where an older message is cut.
        start = LIMITS.message_older - len(secret) // 2
        runtime.chat.post(Sender.HUMAN, "x" * start + secret + " after")
        for i in range(LIMITS.messages):
            runtime.chat.post(Sender.HUMAN, f"later {i}")
        with mock.patch.dict(os.environ, {"KAIRO_CONTEXT_TEST_SECRET": secret}):
            text = render_situation(build_situation(runtime.context(), Limits(messages=LIMITS.messages + 1)))
        self.assertNotIn(secret[:12], text)
        self.assertNotIn(secret[-12:], text)


class DeployStagesTest(DeployCase):
    """Kairo sees what its deployments did, stage by stage (it could not before: a
    deployment prints nothing to stdout, so its action showed no output at all)."""

    def test_a_deployment_shows_each_stage_to_kairo(self):
        sha_b = self.candidate()
        self.deploy(sha_b)
        [action] = build_situation(self.runtime.context())["history"]["actions"]["items"]
        stages = action["deploy"]["stages"]
        self.assertEqual([(s["stage"], s.get("role"), s["passed"]) for s in stages],
                         [("build", None, True), ("tests", "gate", True),
                          ("baseline", "evidence", True), ("dry_cycle", "gate", True),
                          ("switch", None, True)])
        self.assertEqual(stages[1]["tests_run"], 2)
        self.assertEqual((action["deploy"]["from"], action["deploy"]["to"],
                          action["deploy"]["switched"], action["deploy"]["stopped_at_stage"]),
                         (self.sha_a, sha_b, True, None))
        printed = action["output"]
        self.assertEqual(printed["trust"], "untrusted")  # what the test runs printed
        self.assertIn("OK", printed["tests"])
        self.assertTrue(all(len(v) <= STAGE_PRINTED for k, v in printed.items()
                            if k not in ("trust", "source")))

    def test_a_refused_deployment_says_where_it_stopped(self):
        self.repo.write("tests/test_smoke.py", "import unittest\n\n"
                        "class T(unittest.TestCase):\n    def test_no(self):\n        self.fail('x')\n")
        self.deploy(self.repo.commit("breaks the suite"))
        [action] = build_situation(self.runtime.context())["history"]["actions"]["items"]
        self.assertEqual((action["state"], action["deploy"]["switched"],
                          action["deploy"]["stopped_at_stage"]), ("verified_failed", False, "tests"))
        self.assertEqual([(s["stage"], s["passed"]) for s in action["deploy"]["stages"]],
                         [("build", True), ("tests", False)])
        self.assertIn("did not pass", action["output"]["error"])
        self.assertIn("FAILED", action["output"]["tests"])


if __name__ == "__main__":
    unittest.main()
