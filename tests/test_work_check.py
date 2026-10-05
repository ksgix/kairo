"""Completion checks: work whose completion the runtime tests itself.

A check is a command fixed when the work is created. When cognition asks to
complete the work, the runtime runs the check; the work completes only if it
exits 0 (completion_basis "checked").
"""

import tempfile
import unittest
from pathlib import Path

from kairo import Action, Decision, Memory, Runtime
from kairo.actions import action_state
from kairo.cognition import CognitionError, decision_schema, parse_decision
from kairo.environment import ACTIONS
from kairo.situation import build_situation
from kairo.work import CHECKED, MAX_CHECK_ARGV, UNVERIFIED, WorkLedger


def create(check=None, ref="w", objective="make the marker exist"):
    request = {"op": "create", "ref": ref, "objective": objective, "why": "test",
               "directive_id": None, "strategy": "", "next_step": ""}
    if check is not None:
        request["check"] = check
    return request


def complete(work_id, evidence=()):
    return {"op": "set_state", "work_id": work_id, "state": "completed", "reason": "done",
            "wait_seconds": None, "evidence": list(evidence)}


class Scripted:
    name = "test"

    def __init__(self, script):
        self.script, self.calls, self.contexts = script, 0, []

    def decide(self, context):
        self.calls += 1
        self.contexts.append(context)
        return self.script(self.calls, context)


class CheckCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.marker = Path(tmp.name) / "marker"
        self.check = ["test", "-f", str(self.marker)]

    def runtime(self, script) -> Runtime:
        rt = Runtime(Memory(), cognition=Scripted(script))
        rt.start()
        return rt

    def work(self, rt):
        return rt.work.all()[0]


class CompletionCheckTest(CheckCase):
    def test_a_passing_check_completes_the_work_as_checked(self):
        def script(n, ctx):
            if n == 1:
                return Decision(sleep=False, work=[create(self.check)], actions=[
                    Action("process.run", {"argv": ["touch", str(self.marker)]}, work_id="w")])
            return Decision(sleep=True, work=[complete(ctx.open_work[0]["id"])])

        rt = self.runtime(script)
        rt.cycle()
        report = rt.cycle()
        work = self.work(rt)
        self.assertEqual((work.state, work.completion_basis), ("completed", CHECKED))
        (evidence,) = work.evidence
        self.assertTrue(evidence["check"])
        self.assertEqual(evidence["returncode"], 0)
        # The check ran as an attempt at this work, requested by the runtime.
        record = rt.memory.get("action", evidence["action_id"])
        self.assertEqual(record["work_id"], work.id)
        self.assertEqual(record["params"], {"argv": self.check})
        self.assertIn("run by the runtime", record["reason"])
        self.assertEqual([s.action.id for s in report.steps], [evidence["action_id"]])

    def test_a_failing_check_refuses_the_completion(self):
        def script(n, ctx):
            if n == 1:
                return Decision(sleep=False, work=[create(self.check)], actions=[
                    Action("process.run", {"argv": ["true"]}, work_id="w")])
            # "true" exited 0 and is cited, but the objective is not achieved.
            return Decision(sleep=True, work=[complete(
                ctx.open_work[0]["id"], [ctx.recent_actions[-1]["id"]])])

        rt = self.runtime(script)
        rt.cycle()
        with self.assertLogs("kairo", "WARNING"):
            report = rt.cycle()
        work = self.work(rt)
        self.assertEqual(work.state, "active")
        self.assertIsNone(work.completion_basis)
        (rejected,) = report.cognition["work"]["rejected"]
        self.assertIn("did not pass", rejected["reason"])
        self.assertIn("exit 1", rejected["reason"])
        # The failed check is a failed attempt cognition will see.
        attempts = rt.work.attempts(work.id, 10)
        self.assertEqual(action_state(attempts[-1]), "exited_nonzero")
        item = build_situation(rt.context())["work"]["open"][0]
        self.assertEqual(item["completion_check"], self.check)
        self.assertEqual(item["recovery"]["latest_failure"]["action_id"], attempts[-1]["id"])

    def test_the_work_completes_once_the_world_matches(self):
        def script(n, ctx):
            if n == 1:
                return Decision(sleep=False, work=[create(self.check)])
            if n == 2:   # premature
                return Decision(sleep=False, work=[complete(ctx.open_work[0]["id"])])
            if n == 3:
                return Decision(sleep=False, actions=[Action(
                    "process.run", {"argv": ["touch", str(self.marker)]},
                    work_id=ctx.open_work[0]["id"])])
            return Decision(sleep=True, work=[complete(ctx.open_work[0]["id"])])

        rt = self.runtime(script)
        rt.cycle()
        with self.assertLogs("kairo", "WARNING"):
            rt.cycle()
        self.assertEqual(self.work(rt).state, "active")
        rt.cycle()
        rt.cycle()
        self.assertEqual(self.work(rt).completion_basis, CHECKED)
        closed = build_situation(rt.context())["work"]["recently_closed"][0]
        self.assertEqual(closed["completion_basis"], "checked")

    def test_cited_evidence_is_still_validated_before_the_check_runs(self):
        def script(n, ctx):
            if n == 1:
                return Decision(sleep=False, work=[create(self.check)])
            return Decision(sleep=True, work=[complete(ctx.open_work[0]["id"], ["nope"])])

        self.marker.touch()
        rt = self.runtime(script)
        rt.cycle()
        actions = rt.memory.count("action")
        with self.assertLogs("kairo", "WARNING"):
            rt.cycle()
        self.assertEqual(self.work(rt).state, "active")
        self.assertEqual(rt.memory.count("action"), actions)  # the check did not run

    def test_work_without_a_check_is_unchanged(self):
        def script(n, ctx):
            if n == 1:
                return Decision(sleep=False, work=[create()], actions=[
                    Action("process.run", {"argv": ["true"]}, work_id="w")])
            return Decision(sleep=True, work=[complete(
                ctx.open_work[0]["id"], [ctx.recent_actions[-1]["id"]])])

        rt = self.runtime(script)
        rt.cycle()
        rt.cycle()
        work = self.work(rt)
        self.assertEqual((work.state, work.completion_basis), ("completed", UNVERIFIED))
        self.assertIsNone(work.check)

    def test_a_check_that_cannot_run_refuses_the_completion(self):
        def script(n, ctx):
            if n == 1:
                return Decision(sleep=False, work=[create(["/nonexistent/kairo-check"])])
            return Decision(sleep=True, work=[complete(ctx.open_work[0]["id"])])

        rt = self.runtime(script)
        rt.cycle()
        with self.assertLogs("kairo", "WARNING"):
            report = rt.cycle()
        self.assertEqual(self.work(rt).state, "active")
        self.assertIn("failed_to_execute", report.cognition["work"]["rejected"][0]["reason"])


class LedgerTest(unittest.TestCase):
    def test_the_ledger_never_runs_a_check_itself(self):
        ledger = WorkLedger(Memory())
        refs = ledger.apply([create(["true"])]).refs
        outcome = ledger.apply([complete(refs["w"])])
        self.assertIn("nothing here can run it", outcome.rejected[0]["reason"])
        self.assertEqual(ledger.get(refs["w"]).state, "active")

    def test_invalid_checks_are_rejected(self):
        ledger = WorkLedger(Memory())
        for n, bad in enumerate(([], [""], ["x"] * (MAX_CHECK_ARGV + 1), ["x", 5], "true",
                                 ["x" * 501])):
            outcome = ledger.apply([create(bad, objective=f"objective {n}")])
            self.assertEqual(len(outcome.rejected), 1, bad)
        self.assertEqual(ledger.all(), [])

    def test_the_check_survives_storage(self):
        ledger = WorkLedger(Memory())
        refs = ledger.apply([create(["test", "-f", "/tmp/x"])]).refs
        self.assertEqual(ledger.get(refs["w"]).check, ["test", "-f", "/tmp/x"])


class DecisionTest(unittest.TestCase):
    def decision(self, work):
        return {"reason": "", "actions": [], "replies": [], "sleep": True, "wake_after": None,
                "work": work}

    def test_check_is_optional_on_create(self):
        for request in (create(), create(["true"]), {**create(), "check": None}):
            parsed = parse_decision(self.decision([request]), ACTIONS)
            self.assertEqual(parsed.work[0].get("check"), request.get("check"))

    def test_malformed_checks_are_invalid_decisions(self):
        for bad in ("true", [], [1], {"argv": ["true"]}):
            with self.assertRaises(CognitionError):
                parse_decision(self.decision([{**create(), "check": bad}]), ACTIONS)

    def test_a_check_cannot_be_set_or_changed_later(self):
        update = {"op": "update", "work_id": "w", "understanding": "x", "strategy": None,
                  "next_step": None, "check": ["true"]}
        with self.assertRaises(CognitionError):
            parse_decision(self.decision([update]), ACTIONS)

    def test_the_schema_offers_the_check(self):
        variants = decision_schema(ACTIONS)["properties"]["work"]["items"]["anyOf"]
        (creation,) = [v for v in variants if v["properties"]["op"] == {"const": "create"}]
        self.assertIn("check", creation["properties"])
        self.assertNotIn("check", creation["required"])


if __name__ == "__main__":
    unittest.main()
