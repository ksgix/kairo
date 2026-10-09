"""Cognition continuity: a long-lived work item's understanding.

The understanding is cognition's current synthesis of a work item (what is known,
what was tried and why it failed, constraints, open questions), up to 10,000
characters, replaced as a whole when it changes. These tests check that it reaches
cognition intact cycle after cycle, that it stays labelled as interpretation, and
that the situation, and so the prompt, stays bounded however much of it there is.
The runtime's records (attempts, failures, strategy revisions) stay the facts.
"""

import json
import os
import unittest
from unittest import mock

from kairo import Action, Decision
from kairo.chat import Sender
from kairo.instructions import INSTRUCTIONS, cognition_request
from kairo.redact import MARKER, protect_env
from kairo.situation import LIMITS, Limits, build_situation, render_situation
from kairo.work import STRATEGY_LOG, TEXT_LIMITS
from test_work import Script, WorkCase, create, only_open, run, set_state, update

SECRET_NAME = "KAIRO_CONTINUITY_TEST_TOKEN"
SECRET = "continuity-secret-0123456789abcdef"
LIMIT = TEXT_LIMITS["understanding"]


def synthesis(tag, size):
    """A long understanding with recognisable start, middle and end."""
    start, middle, end = f"<{tag}:start>", f"<{tag}:middle>", f"<{tag}:end>"
    filler = size - len(start) - len(middle) - len(end)
    return start + "a" * (filler // 2) + middle + "z" * (filler - filler // 2) + end


class UnderstandingLimitTest(WorkCase):
    def setUp(self):
        self.rt = self.runtime()
        self.wid = self.rt.work.apply([create("w", "Find the slow query")]).refs["w"]

    def test_ten_thousand_characters_are_accepted_and_persisted(self):
        self.assertEqual(LIMIT, 10_000)
        text = synthesis("u", LIMIT)
        outcome = self.rt.work.apply([update(self.wid, understanding=text)])
        self.assertEqual(outcome.rejected, [])
        self.assertEqual(self.rt.memory.get("work", self.wid)["understanding"], text)

    def test_beyond_the_limit_the_whole_update_is_rejected_and_nothing_changes(self):
        kept = synthesis("kept", 500)
        self.rt.work.apply([update(self.wid, understanding=kept)])
        for size in (LIMIT + 1, 3 * LIMIT):
            with self.subTest(size=size):
                outcome = self.rt.work.apply([update(self.wid, understanding="u" * size,
                                                     next_step="also changed?")])
                [rejected] = outcome.rejected
                self.assertIn(f"longer than {LIMIT} characters", rejected["reason"])
                record = self.rt.memory.get("work", self.wid)
                self.assertEqual(record["understanding"], kept)  # not cut, not stored
                self.assertNotEqual(record["next_step"], "also changed?")  # all or nothing


class CognitionSeesTheUnderstandingTest(WorkCase):
    def test_the_whole_understanding_reaches_cognition_and_its_prompt(self):
        text = synthesis("diagnosis", LIMIT)

        def think(s, n):
            if n == 1:
                return Decision(work=[create("w", "Find the slow query")], sleep=False)
            if n == 2:
                return Decision(work=[update(only_open(s)["id"], understanding=text)],
                                sleep=False)
            return Decision(sleep=True)

        cognition = Script(think)
        rt = self.runtime(cognition)
        rt.chat.post(Sender.HUMAN, "m" * 5000)  # an ordinary long string
        self.cycles(rt, 3)
        item = only_open(cognition.situations[2])
        self.assertEqual(item["understanding"], text)  # all 10,000 characters
        self.assertNotIn("understanding_shortened", item)
        prompt = cognition_request(rt.context()).prompt
        for marker in ("<diagnosis:start>", "<diagnosis:middle>", "<diagnosis:end>"):
            self.assertIn(marker, prompt)
        # The per-string cap still applies everywhere else.
        [message] = cognition.situations[2]["history"]["chat"]["items"]
        self.assertLessEqual(len(message["text"]), LIMITS.text + 40)
        self.assertIn("[truncated ", message["text"])

    def test_the_understanding_stays_labelled_as_interpretation(self):
        rt = self.runtime()
        wid = rt.work.apply([create("w", "Find the slow query")]).refs["w"]
        text = synthesis("label", 3000)
        rt.work.apply([update(wid, understanding=text)])
        rt.start()
        s = build_situation(rt.context())
        note = s["work"]["note"]
        self.assertIn("understanding", note)
        self.assertIn("your earlier words (interpretation)", note)
        self.assertIn("current synthesis", INSTRUCTIONS)
        [item] = s["work"]["open"]
        # The runtime's facts about the work never carry cognition's words.
        self.assertNotIn("<label:", json.dumps(item["recovery"]))
        self.assertNotIn("<label:", json.dumps(item["recent_changes"]))
        self.assertIn("not a log", INSTRUCTIONS)
        self.assertIn("10,000 characters", INSTRUCTIONS)

    def test_secrets_in_the_understanding_are_redacted_where_stored_and_shown(self):
        protect_env([SECRET_NAME])
        with mock.patch.dict(os.environ, {SECRET_NAME: SECRET}):
            rt = self.runtime()
            wid = rt.work.apply([create("w", "Rotate the key")]).refs["w"]
            text = "The key is " + SECRET + " and " + "k" * 6000 + " end"
            self.assertEqual(rt.work.apply([update(wid, understanding=text)]).rejected, [])
            stored = rt.memory.get("work", wid)["understanding"]
            rt.start()
            prompt = cognition_request(rt.context()).prompt
        self.assertNotIn(SECRET, stored)
        self.assertIn(MARKER, stored)
        self.assertNotIn(SECRET, prompt)
        self.assertIn(MARKER, prompt)


class LongLivedWorkTest(WorkCase):
    def test_a_long_lived_work_carries_its_state_across_cycles(self):
        diagnosis = synthesis("diag", 7000)

        def think(s, n):
            if n == 1:
                return Decision(work=[create("w", "Make the backup job pass",
                                             strategy="rerun it as is")], sleep=False)
            w = only_open(s)
            if n == 2:  # an attempt that fails
                return Decision(actions=[run(["false"], w["id"])], sleep=False)
            if n == 3:  # the failure is understood: a synthesis and a new strategy
                return Decision(work=[update(w["id"], understanding=diagnosis,
                                             strategy="fix the target path first")],
                                sleep=False)
            return Decision(sleep=n >= 6)

        cognition = Script(think)
        rt = self.runtime(cognition)
        self.cycles(rt, 6)
        for n in (3, 4, 5):  # cycles 4..6 see the same state, without being told again
            w = only_open(cognition.situations[n])
            self.assertEqual(w["understanding"], diagnosis)
            self.assertEqual(w["strategy"], {"revision": 2, "text": "fix the target path first"})
            revisions = {r["revision"]: r for r in w["recovery"]["revisions"]}
            self.assertEqual((revisions[1]["attempts"], revisions[1]["failed"]), (1, 1))
            self.assertEqual(revisions[1]["strategy"], "rerun it as is")
            self.assertEqual(revisions[2]["attempts"], 0)
            self.assertEqual(w["recovery"]["latest_failure"]["failure"], "exited_nonzero")
            self.assertIs(w["recovery"]["diagnosis_since_latest_failure"], True)


class StrategyHistoryTest(WorkCase):
    def test_strategies_replaced_before_any_attempt_stay_visible(self):
        rt = self.runtime()
        wid = rt.work.apply([create("w", "Speed up the build", strategy="cache deps")]).refs["w"]
        rt.work.apply([update(wid, strategy="parallelise tests")])
        rt.work.apply([update(wid, strategy="split the monorepo")])
        rt.start()
        [item] = build_situation(rt.context())["work"]["open"]
        revisions = item["recovery"]["revisions"]
        self.assertEqual([r["revision"] for r in revisions], [3, 2, 1])
        self.assertEqual([r["strategy"] for r in revisions],
                         ["split the monorepo", "parallelise tests", "cache deps"])
        self.assertEqual([r["attempts"] for r in revisions], [0, 0, 0])
        self.assertTrue(all("at" in r["adopted"] for r in revisions))
        self.assertTrue(all(r["last_attempt"] is None for r in revisions))

    def test_strategy_history_stays_within_its_bounds(self):
        rt = self.runtime()
        wid = rt.work.apply([create("w", "Speed up the build", strategy="s1")]).refs["w"]
        for i in range(2, 15):
            rt.work.apply([update(wid, strategy=f"s{i} " + "x" * 590)])
        record = rt.memory.get("work", wid)
        self.assertEqual(len(record["strategy_log"]), STRATEGY_LOG)
        rt.start()
        [item] = build_situation(rt.context())["work"]["open"]
        revisions = item["recovery"]["revisions"]
        self.assertEqual([r["revision"] for r in revisions],
                         list(range(14, 14 - LIMITS.work_revisions, -1)))
        self.assertTrue(all(len(r["strategy"]) <= 600 for r in revisions))


class RecoveryUnchangedTest(WorkCase):
    def test_a_failed_attempt_is_refused_until_the_understanding_changes(self):
        rt = self.runtime()
        rt.start()
        wid = rt.work.apply([create("w", "Restart the service")]).refs["w"]
        rt.act(Action("process.run", {"argv": ["false"]}, reason="try", work_id=wid))
        repeat = run(["false"], wid)

        def attempt():
            rt.wake("next") if rt.state.value == "sleeping" else None
            rt.cognition = Script(lambda s, n: Decision(actions=[repeat], sleep=True))
            return rt.cycle()

        refused = attempt()
        self.assertEqual(refused.steps, [])  # the identical repeat was refused
        rt.work.apply([update(wid, understanding=synthesis("why", LIMIT))])
        allowed = attempt()
        self.assertEqual(len(allowed.steps), 1)  # reassessed: the retry may run


class BoundedSituationTest(WorkCase):
    def busy_runtime(self, works=8, directives=6):
        """A runtime whose every bounded list is full of long text."""
        rt = self.runtime()
        rt.start()
        for i in range(directives):
            rt.add_directive(f"Purpose {i}", synthesis(f"desc{i}", 4000))
        for i in range(works):
            wid = rt.work.apply([create(f"w{i}", f"Objective {i}")]).refs[f"w{i}"]
            rt.work.apply([update(wid, understanding=synthesis(f"u{i}", LIMIT))])
            rt.act(Action("process.run", {"argv": ["false"]}, reason="r" * 200, work_id=wid))
        for i in range(LIMITS.actions):
            rt.act(Action("process.run", {"argv": ["python3", "-c", "print('o' * 3000)"]},
                          reason=f"look {i}"))
        for i in range(LIMITS.messages):
            rt.chat.post(Sender.HUMAN if i % 2 else Sender.KAIRO, f"msg{i} " + "c" * 3000)
        for i in range(LIMITS.cycles):
            rt.memory.put("cycle", f"c{i}", {"at": 1.0 + i, "note": "n" * 1000,
                                              "cognition": {"result": "decided"}})
        return rt

    def test_understanding_across_open_work_is_bounded_and_prioritised(self):
        rt = self.runtime()
        rt.start()
        ids = []
        for i in range(8):
            wid = rt.work.apply([create(f"w{i}", f"Objective {i}")]).refs[f"w{i}"]
            rt.work.apply([update(wid, understanding=synthesis(f"u{i}", LIMIT))])
            ids.append(wid)
        # The most recently updated is waiting: active work comes first.
        rt.work.apply([set_state(ids[-1], "waiting", "for the vendor")])
        limits = Limits(budget=10**9)  # isolate the allotment from the budget
        s = build_situation(rt.context(), limits)
        shown = {w["id"]: w for w in s["work"]["open"]}
        full = [i for i in ids if "understanding_shortened" not in shown[i]]
        self.assertEqual(full, [ids[5], ids[6]])  # the two most recent *active* ones
        total = sum(len(w["understanding"]) for w in shown.values())
        self.assertLessEqual(total, limits.understanding_total
                             + limits.understanding_floor * (len(ids) - 2))
        for wid in set(ids) - set(full):
            w = shown[wid]
            self.assertEqual(w["understanding_shortened"]["full_chars"], LIMIT)
            self.assertLessEqual(len(w["understanding"]),
                                 w["understanding_shortened"]["shown_chars"])
            self.assertIn("[truncated ", w["understanding"])
            self.assertTrue(w["understanding"].startswith("<u"))  # beginning and end kept
            self.assertTrue(w["understanding"].endswith(":end>"))
        again = build_situation(rt.context(), limits)
        self.assertEqual(again["work"]["open"], s["work"]["open"])  # deterministic

    def test_a_busy_situation_keeps_recent_history_and_shortens_long_texts_first(self):
        rt = self.busy_runtime(works=3, directives=2)
        s = build_situation(rt.context())
        size = len(render_situation({k: v for k, v in s.items() if k != "context"}))
        self.assertLessEqual(size, LIMITS.budget)
        for kind in ("actions", "chat", "cycles"):  # the newest of each kind survive
            self.assertGreaterEqual(len(s["history"][kind]["items"]), LIMITS.history_keep)
        self.assertGreater(s["context"]["trimmed_for_budget"], 0)          # old history went,
        self.assertGreater(s["context"]["long_texts_shortened_for_budget"], 0)  # then long text
        for w in s["work"]["open"]:  # shortened, never below its floor, never dropped
            self.assertGreaterEqual(len(w["understanding"]), LIMITS.understanding_floor - 20)
            self.assertTrue(w["understanding"].startswith("<u"))
            self.assertTrue(w["understanding"].endswith(":end>"))

    def test_an_overfull_situation_stays_within_budget_without_losing_work_facts(self):
        rt = self.busy_runtime(works=8, directives=6)
        s = build_situation(rt.context())
        size = len(render_situation({k: v for k, v in s.items() if k != "context"}))
        self.assertLessEqual(size, LIMITS.budget)
        self.assertEqual(len(s["work"]["open"]), LIMITS.work_open)  # every open item, with
        for w in s["work"]["open"]:                                  # its runtime facts
            self.assertEqual(w["recovery"]["latest_failure"]["failure"], "exited_nonzero")
            self.assertTrue(w["understanding"].startswith("<u"))
        self.assertEqual(len(s["directives"]["active"]), 6)
        for d in s["directives"]["active"]:
            self.assertTrue(d["description"].startswith("<desc"))
        # History went below its keep floor only after every long text was at its floor.
        if any(len(s["history"][k]["items"]) < LIMITS.history_keep
               for k in ("actions", "chat", "cycles")):
            for w in s["work"]["open"]:
                self.assertLessEqual(len(w["understanding"]), LIMITS.understanding_floor)
            for d in s["directives"]["active"]:
                self.assertLessEqual(len(d["description"]), LIMITS.directive_description_floor)

    def test_no_unbounded_prompt_growth(self):
        """However long the work and its history grow, the prompt stays bounded."""
        sizes = []
        rt = self.busy_runtime(works=2, directives=1)
        for round_ in range(4):
            for i in range(10):  # more of everything, every round
                rt.act(Action("process.run", {"argv": ["python3", "-c", "print('p' * 3000)"]},
                              reason="more"))
                rt.chat.post(Sender.HUMAN, "more " + "c" * 3000)
            wid = rt.work.apply([create(f"x{round_}", f"More work {round_}")]).refs.get(
                f"x{round_}")
            if wid:
                rt.work.apply([update(wid, understanding=synthesis(f"x{round_}", LIMIT))])
            sizes.append(len(cognition_request(rt.context()).prompt))
        bound = LIMITS.budget + 4000  # the budget, plus the context section and framing
        self.assertTrue(all(size <= bound for size in sizes), sizes)


if __name__ == "__main__":
    unittest.main()
