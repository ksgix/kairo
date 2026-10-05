"""Ongoing work: runtime-authoritative pursuits carried across cognition cycles.

Cognition here is a deterministic script that reads the situation it is shown
and returns decisions; the tests check what the runtime does with them:
validation, persistence, linkage, continuity. No intelligence is simulated.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kairo import Action, Decision, Memory, Outcome, Runtime, State, Verification
from kairo.cognition import CognitionError, decision_schema, parse_decision
from kairo.environment import ACTIONS
from kairo.redact import MARKER
from kairo.situation import build_situation, render_situation
from kairo.work import MAX_OPEN, MAX_REQUESTS, TEXT_LIMITS
from test_continuous import SRC, TIMEOUT


# -- helpers -------------------------------------------------------------------


def create(ref, objective, why="it matters", directive_id=None, strategy="look first",
           next_step="observe"):
    return {"op": "create", "ref": ref, "objective": objective, "why": why,
            "directive_id": directive_id, "strategy": strategy, "next_step": next_step}


def update(work_id, understanding=None, strategy=None, next_step=None):
    return {"op": "update", "work_id": work_id, "understanding": understanding,
            "strategy": strategy, "next_step": next_step}


def set_state(work_id, state, reason="because", wait_seconds=None, evidence=None):
    return {"op": "set_state", "work_id": work_id, "state": state, "reason": reason,
            "wait_seconds": wait_seconds, "evidence": evidence or []}


def run(argv, work=None, reason="attempt"):
    return Action("process.run", {"argv": argv}, reason=reason, work_id=work)


class Script:
    """Cognition double: ``fn(situation, n)`` returns the n-th Decision (1-based)."""

    name = "script"

    def __init__(self, fn):
        self.fn = fn
        self.situations = []

    def decide(self, context):
        situation = build_situation(context)
        self.situations.append(situation)
        return self.fn(situation, len(self.situations))


def plan(*decisions):
    """A Script returning fixed decisions, then sleeping."""
    return Script(lambda s, n: decisions[n - 1] if n <= len(decisions) else Decision(sleep=True))


def open_work(situation):
    return situation["work"]["open"]


def only_open(situation):
    [w] = open_work(situation)
    return w


class ReturnCode:
    def verify(self, action, result):
        ok = result.output.get("returncode") == 0
        return Verification(Outcome.SUCCESS if ok else Outcome.FAILURE,
                            evidence={"returncode": result.output.get("returncode")})


class WorkCase(unittest.TestCase):
    def runtime(self, cognition=None, path=":memory:", **kwargs):
        memory = Memory(path)
        self.addCleanup(memory.close)
        rt = Runtime(memory, cognition=cognition, **kwargs)
        return rt

    def cycles(self, rt, n):
        """Run n cycles, waking between them as a sleeping Kairo would be woken."""
        if rt.state is State.CREATED:
            rt.start()
        reports = []
        for _ in range(n):
            if rt.state is State.SLEEPING:
                rt.wake("next cycle")
            reports.append(rt.cycle())
        return reports


# -- A, B, C, K: creation and continuity ----------------------------------------


class CreateAndContinueTest(WorkCase):
    def test_create_needs_no_todo_and_appears_next_cycle(self):
        cognition = plan(Decision(work=[create("disk", "Find why /var keeps filling up")],
                                  sleep=False))
        rt = self.runtime(cognition)
        self.cycles(rt, 2)
        [record] = rt.memory.all("work")
        self.assertEqual((record["state"], record["objective"]),
                         ("active", "Find why /var keeps filling up"))
        seen = only_open(cognition.situations[1])
        self.assertEqual(seen["id"], record["id"])
        self.assertEqual(seen["strategy"], {"revision": 1, "text": "look first"})
        # No todo exists, and the work is still there to pursue.
        self.assertEqual(cognition.situations[1]["todo"]["open"], [])
        cycle = cognition.situations[1]["history"]["cycles"]["items"][0]
        self.assertEqual(cycle["work_applied"][0]["work_id"], record["id"])

    def test_same_identity_across_cycles_and_sleep(self):
        cognition = plan(Decision(work=[create("w", "Keep backups verified")], sleep=True))
        rt = self.runtime(cognition)
        self.cycles(rt, 4)  # sleeps after every cycle, woken each time
        ids = {only_open(s)["id"] for s in cognition.situations[1:]}
        self.assertEqual(len(ids), 1)
        self.assertTrue(all(only_open(s)["state"] == "active" for s in cognition.situations[1:]))

    def test_runtime_assigns_identity_and_times(self):
        rt = self.runtime()
        before = time.time()
        outcome = rt.work.apply([create("mine", "Objective")])
        [work] = rt.work.all()
        self.assertEqual(outcome.refs, {"mine": work.id})
        self.assertNotEqual(work.id, "mine")
        self.assertGreaterEqual(work.created_at, before)
        self.assertEqual(work.history[0]["event"], "created")


# -- D: process restart ------------------------------------------------------------


class RestartTest(WorkCase):
    def test_work_is_reconstructed_by_a_new_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            first = self.runtime(plan(
                Decision(work=[create("w", "Investigate slow queries", strategy="read logs")],
                         actions=[run(["true"], work="w")], sleep=False),
                Decision(sleep=True)), path=db)
            self.cycles(first, 2)
            first.stop()
            [before] = first.memory.all("work")
            first.memory.close()

            # A genuinely new OS process reads the same database.
            out = subprocess.run([sys.executable, "-m", "kairo", "--situation", "--db", str(db)],
                                 capture_output=True, text=True, timeout=TIMEOUT,
                                 env={**os.environ, "PYTHONPATH": str(SRC)})
            self.assertEqual(out.returncode, 0, out.stderr)
            seen = only_open(json.loads(out.stdout))
            self.assertEqual((seen["id"], seen["state"], seen["objective"]),
                             (before["id"], "active", "Investigate slow queries"))
            self.assertEqual(len(seen["recent_attempts"]), 1)

            # And a restarted runtime continues it.
            again = self.runtime(plan(Decision(work=[update(before["id"],
                                                            understanding="logs show locks")])),
                                 path=db)
            self.cycles(again, 1)
            [after] = again.memory.all("work")
            self.assertEqual((after["id"], after["understanding"]), (before["id"], "logs show locks"))


# -- E, F, G, H, M: lifecycle ------------------------------------------------------


class LifecycleTest(WorkCase):
    def created(self, rt, objective="Tune the database"):
        rt.work.apply([create("w", objective)])
        return rt.work.all()[-1].id

    def test_waiting(self):
        rt = self.runtime()
        wid = self.created(rt)
        rt.work.apply([set_state(wid, "waiting", "operator must approve downtime",
                                 wait_seconds=3600)])
        s = build_situation(rt.context())
        w = only_open(s)
        self.assertEqual((w["state"], w["state_reason"]), ("waiting", "operator must approve downtime"))
        self.assertIs(w["wait_elapsed"], False)
        # A deadline is a future time by nature, not a clock anomaly.
        self.assertAlmostEqual(w["waiting_until"]["due_in_seconds"], 3600, delta=5)
        self.assertNotIn("note", w["waiting_until"])
        self.assertEqual(s["open_threads"]["work_wait_elapsed"], [])
        self.assertNotIn(wid, [x["id"] for x in open_work(s) if x["state"] == "active"])

    def test_elapsed_wait_is_surfaced(self):
        rt = self.runtime()
        wid = self.created(rt)
        rt.work.apply([set_state(wid, "waiting", "retry after cooldown", wait_seconds=10)],
                      now=time.time() - 100)
        s = build_situation(rt.context())
        self.assertIs(only_open(s)["wait_elapsed"], True)
        self.assertAlmostEqual(only_open(s)["waiting_until"]["passed_seconds_ago"], 90, delta=5)
        self.assertEqual(s["open_threads"]["work_wait_elapsed"], [wid])
        self.assertEqual(only_open(s)["state"], "waiting")  # the runtime does not resume it itself

    def test_blocked_is_kept_with_its_obstacle_and_can_resume(self):
        rt = self.runtime()
        wid = self.created(rt)
        rt.work.apply([set_state(wid, "blocked", "needs root; runtime runs as test-kairo-user")])
        w = only_open(build_situation(rt.context()))
        self.assertEqual((w["state"], w["state_reason"]),
                         ("blocked", "needs root; runtime runs as test-kairo-user"))
        outcome = rt.work.apply([set_state(wid, "active", "permission granted")])
        self.assertEqual(outcome.rejected, [])
        self.assertEqual(rt.work.get(wid).state, "active")

    def test_completed_is_history_and_never_resurrects(self):
        def think(s, n):
            if n == 1:
                return Decision(work=[create("w", "Rotate the old logs")],
                                actions=[run(["true"], work="w")], sleep=False)
            if n == 2:
                w = only_open(s)
                return Decision(work=[set_state(w["id"], "completed", "logs rotated",
                                                evidence=[w["recent_attempts"][0]["action_id"]])])
            return Decision(sleep=True)

        cognition = Script(think)
        rt = self.runtime(cognition)
        self.cycles(rt, 5)
        [record] = rt.memory.all("work")
        self.assertEqual(record["state"], "completed")
        for s in cognition.situations[2:]:
            self.assertEqual(open_work(s), [])
            [closed] = s["work"]["recently_closed"]
            self.assertEqual((closed["id"], closed["state"], closed["reason"]),
                             (record["id"], "completed", "logs rotated"))
            self.assertEqual((closed["evidence"][0]["state"], closed["evidence"][0]["returncode"]),
                             ("executed_unverified", 0))
            self.assertEqual(closed["completion_basis"], "unverified")
        # Changing closed work is refused; a new reason means new work.
        refused = rt.work.apply([set_state(record["id"], "active", "again?"),
                                 update(record["id"], understanding="more")])
        self.assertEqual(len(refused.rejected), 2)
        self.assertIn("closed work does not change", refused.rejected[0]["reason"])
        fresh = rt.work.apply([create("again", "Rotate the old logs")])
        self.assertEqual(fresh.rejected, [])
        self.assertNotEqual(fresh.refs["again"], record["id"])
        self.assertEqual(rt.work.get(record["id"]).state, "completed")

    def test_abandoned_keeps_its_reason_and_does_not_resume(self):
        rt = self.runtime()
        wid = self.created(rt)
        rt.work.apply([set_state(wid, "abandoned", "service was decommissioned")])
        s = build_situation(rt.context())
        self.assertEqual(open_work(s), [])
        [closed] = s["work"]["recently_closed"]
        self.assertEqual((closed["state"], closed["reason"]),
                         ("abandoned", "service was decommissioned"))
        self.assertEqual(len(rt.work.apply([set_state(wid, "active", "retry")]).rejected), 1)


# -- I, J: failure continuity and strategy change ----------------------------------------


class FailureAndStrategyTest(WorkCase):
    def test_failed_attempt_is_visible_with_the_work(self):
        cognition = plan(Decision(work=[create("w", "Restart the exporter")],
                                  actions=[run(["/nonexistent/exporter", "--restart"], work="w")],
                                  sleep=False))
        rt = self.runtime(cognition)
        self.cycles(rt, 2)
        w = only_open(cognition.situations[1])
        [attempt] = w["recent_attempts"]
        self.assertEqual((attempt["state"], attempt["strategy_revision"]), ("failed_to_execute", 1))
        self.assertIn("No such file", attempt["problem"])
        self.assertEqual(w["attempts_with_current_strategy"], {"attempts": 1, "failed": 1})
        self.assertEqual(attempt["failure"], "not_found")
        self.assertEqual(w["state"], "active")  # a failure is not a blocker by itself
        history = cognition.situations[1]["history"]["actions"]["items"]
        self.assertEqual(history[0]["verification"]["outcome"], "failure")

    def test_retry_and_strategy_change_are_distinguished(self):
        rt = self.runtime()
        rt.start()
        rt.work.apply([create("w", "Get service healthy", strategy="restart it")])
        wid = rt.work.all()[0].id
        rt.act(run(["false"], work=wid))                 # revision 1 (exits 1)
        rt.act(run(["false"], work=wid))                 # revision 1 again: a retry
        rt.work.apply([update(wid, strategy="fix its config first")])
        rt.act(run(["true"], work=wid))                  # revision 2: a new strategy
        w = only_open(build_situation(rt.context()))
        self.assertEqual(w["strategy"], {"revision": 2, "text": "fix its config first"})
        self.assertEqual([a["strategy_revision"] for a in w["recent_attempts"]], [1, 1, 2])
        self.assertEqual(w["attempts_with_current_strategy"], {"attempts": 1, "failed": 0})
        change = [c for c in w["recent_changes"] if c["event"] == "strategy_changed"][0]
        self.assertEqual(change["revision"], 2)
        revisions = {r["revision"]: (r["strategy"], r["attempts"], r["failed"], r["succeeded"])
                     for r in w["recovery"]["revisions"]}
        self.assertEqual(revisions, {1: ("restart it", 2, 2, 0), 2: ("fix its config first", 1, 0, 1)})
        # Re-sending the same strategy is not a change.
        self.assertIn("changes nothing",
                      rt.work.apply([update(wid, strategy="fix its config first")]).rejected[0]["reason"])


# -- the mandatory end-to-end lifecycle ----------------------------------------------------


class EndToEndTest(WorkCase):
    def test_discover_fail_rethink_succeed_complete_stay_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixed = Path(tmp) / "service.conf"

            def think(s, n):
                work = open_work(s)
                if n == 1:  # discover a problem (an operator report) and start work on it
                    assert s["open_threads"]["unanswered_human_messages"]
                    return Decision(
                        work=[create("conf", "Service config file must exist",
                                     strategy="copy it from the packaged default")],
                        actions=[run(["cp", "/nonexistent/default.conf", str(fixed)], work="conf")],
                        replies=["Looking into the missing config."], sleep=False)
                if n == 2:  # the attempt failed: understand it, change strategy, try again
                    [w] = work
                    [failed] = w["recent_attempts"]
                    assert failed["state"] == "verified_failed", failed
                    return Decision(
                        work=[update(w["id"], understanding="no packaged default exists",
                                     strategy="write a minimal config directly")],
                        actions=[run([sys.executable, "-c",
                                      f"open({str(fixed)!r}, 'w').write('port=8080\\n')"],
                                     work=w["id"])],
                        sleep=False)
                if n == 3:  # the new attempt is verified: complete, citing it as evidence
                    [w] = work
                    latest = w["recent_attempts"][-1]
                    assert latest["state"] == "verified_successful", latest
                    return Decision(work=[set_state(w["id"], "completed", "config written",
                                                    evidence=[latest["action_id"]])],
                                    replies=["Config restored."], sleep=True)
                return Decision(sleep=True)  # later cycles: nothing to resume

            cognition = Script(think)
            rt = self.runtime(cognition, verifiers={"process.run": ReturnCode()})
            rt.receive("The service won't start: its config file is missing.")
            self.cycles(rt, 6)

            self.assertEqual(fixed.read_text(), "port=8080\n")
            [record] = rt.memory.all("work")
            self.assertEqual(record["state"], "completed")
            self.assertEqual(record["strategy_revision"], 2)
            self.assertEqual([e["state"] for e in record["evidence"]], ["verified_successful"])
            self.assertEqual(record["completion_basis"], "verified")
            attempts = rt.work.attempts(record["id"], 10)
            self.assertEqual([(a["strategy_revision"], a["verification"]["outcome"])
                              for a in attempts], [(1, "failure"), (2, "success")])
            events = [h["event"] for h in record["history"]]
            self.assertEqual(events[0], "created")
            self.assertIn("strategy_changed", events)
            self.assertEqual(events[-1], "state_changed")
            for s in cognition.situations[3:]:  # after completion: history, not work
                self.assertEqual(open_work(s), [])
                self.assertEqual(s["work"]["recently_closed"][0]["state"], "completed")
            self.assertEqual(rt.work.get(record["id"]).state, "completed")


# -- L: several work items ------------------------------------------------------------


class MultipleWorkTest(WorkCase):
    def test_items_are_independent(self):
        rt = self.runtime()
        rt.start()
        rt.work.apply([create("a", "Alpha"), create("b", "Beta"), create("c", "Gamma")])
        ids = {w.objective: w.id for w in rt.work.all()}
        rt.act(run(["true"], work=ids["Beta"]))
        attempt = rt.work.attempts(ids["Beta"], 5)[0]["id"]
        rt.work.apply([set_state(ids["Beta"], "completed", "done", evidence=[attempt]),
                       set_state(ids["Gamma"], "waiting", "until Monday")])
        s = build_situation(rt.context())
        self.assertEqual({w["id"]: w["state"] for w in open_work(s)},
                         {ids["Alpha"]: "active", ids["Gamma"]: "waiting"})
        self.assertEqual([w["id"] for w in s["work"]["recently_closed"]], [ids["Beta"]])
        self.assertEqual(len(rt.memory.all("work")), 3)  # nothing deleted
        # Each item's attempts are its own.
        self.assertEqual(rt.work.attempts(ids["Alpha"], 5), [])


# -- N, O: malformed state and provider failure ---------------------------------------


class RobustnessTest(WorkCase):
    def test_malformed_work_records_do_not_stop_kairo(self):
        cognition = plan(Decision(work=[set_state("broken", "completed", "x", evidence=["a"]),
                                        create("new", "Still possible")], sleep=True))
        rt = self.runtime(cognition)
        # A known field of the wrong type is corruption (an unknown field is not: Phase 9).
        rt.memory.put("work", "broken", {"id": "broken", "state": "active", "objective": 5,
                                         "why": "y"})
        rt.memory.put("work", "weird", {"id": "weird", "state": "sideways"})
        rt.memory.put("work", "nulls", {"id": "nulls", "state": "waiting", "waiting_until": "soon"})
        with self.assertLogs("kairo", "WARNING"):
            [report] = self.cycles(rt, 1)
        self.assertIs(report.state, State.SLEEPING)
        [rejected] = report.cognition["work"]["rejected"]
        self.assertIn("unreadable (corrupt record)", rejected["reason"])
        self.assertEqual(report.cognition["work"]["applied"][0]["op"], "create")
        s = build_situation(rt.context())
        self.assertNotIn("work", s["context"]["unavailable_sections"])
        self.assertIn("nulls", [w["id"] for w in open_work(s)])  # shown, bad time unknown

    def test_provider_failure_leaves_work_intact(self):
        class Flaky:
            name = "flaky"
            calls = 0

            def decide(self, context):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    return Decision(work=[create("w", "Watch replication lag")])
                if Flaky.calls == 2:
                    raise RuntimeError("provider down")
                self.seen = build_situation(context)
                return Decision(sleep=True)

        cognition = Flaky()
        rt = self.runtime(cognition)
        with self.assertLogs("kairo", "ERROR"):
            self.cycles(rt, 3)
        self.assertEqual(only_open(cognition.seen)["objective"], "Watch replication lag")
        self.assertEqual(rt.memory.all("work")[0]["state"], "active")


# -- P: security --------------------------------------------------------------------


class SecurityTest(WorkCase):
    def test_secret_bearing_work_text_is_redacted_everywhere(self):
        secret = "sk-work-0123456789abcdef"
        with mock.patch.dict(os.environ, {"VAULT_TOKEN": secret}):
            rt = self.runtime()
            rt.work.apply([create("w", f"Rotate token {secret}", why=f"leaked as {secret}")])
            wid = rt.work.all()[0].id
            rt.work.apply([update(wid, understanding=f"the value {secret} is in env")])
            text = render_situation(build_situation(rt.context()))
        self.assertNotIn(secret, text)
        self.assertIn(MARKER, text)
        dump = "\n".join(rt.memory._db.iterdump())
        self.assertNotIn(secret, dump)

    def test_work_text_is_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "pwned"
            cognition = plan(Decision(work=[create("w", f"$(touch {marker})",
                                                   why=f"; touch {marker}",
                                                   next_step=f"`touch {marker}`")]))
            rt = self.runtime(cognition)
            self.cycles(rt, 2)
            self.assertFalse(marker.exists())
            self.assertEqual(rt.memory.all("action"), [])

    def test_oversized_text_is_rejected_not_stored(self):
        rt = self.runtime()
        outcome = rt.work.apply([create("w", "x" * (TEXT_LIMITS["objective"] + 1))])
        self.assertIn("longer than", outcome.rejected[0]["reason"])
        self.assertEqual(rt.memory.all("work"), [])


# -- Q: decision validation -------------------------------------------------------------


class CompletionBasisTest(WorkCase):
    """The adversarial review: completion must be honest about its grounding."""

    def attempt_completion(self, argv, verifiers=None, path=":memory:"):
        """Cycle 1: create meaningful work with one linked attempt. Cycle 2: cognition
        claims completion citing that attempt. Returns (runtime, work record, rejections)."""
        def think(s, n):
            if n == 1:
                return Decision(work=[create("w", "Fix replication between db1 and db2",
                                             why="replica is 6h behind")],
                                actions=[run(argv, work="w", reason="fix replication")],
                                sleep=False)
            if n == 2:
                w = only_open(s)
                return Decision(work=[set_state(w["id"], "completed", "replication fixed",
                                                evidence=[w["recent_attempts"][-1]["action_id"]])])
            return Decision(sleep=True)

        rt = self.runtime(Script(think), path=path, verifiers=verifiers or {})
        reports = self.cycles(rt, 2)
        [record] = rt.memory.all("work")
        return rt, record, (reports[1].cognition.get("work") or {}).get("rejected") or []

    def test_a_b_unverified_exit_zero_completes_as_unverified(self):
        for argv in (["true"], ["echo", "replication fixed"]):
            with self.subTest(argv=argv):
                _, record, rejected = self.attempt_completion(argv)
                self.assertEqual(rejected, [])
                self.assertEqual((record["state"], record["completion_basis"]),
                                 ("completed", "unverified"))
                self.assertEqual(record["evidence"][0]["returncode"], 0)

    def test_c_d_unverified_nonzero_exit_cannot_complete(self):
        for argv, code in ((["false"], 1), (["sh", "-c", "exit 3"], 3)):
            with self.subTest(argv=argv):
                with self.assertLogs("kairo", "WARNING"):
                    _, record, [rejected] = self.attempt_completion(argv)
                self.assertIn(f"exited {code} and no verifier confirmed success", rejected["reason"])
                self.assertEqual(record["state"], "active")
                self.assertIsNone(record["completion_basis"])
                self.assertEqual(record["evidence"], [])

    def test_e_verified_evidence_completes_as_verified(self):
        _, record, rejected = self.attempt_completion(["true"], verifiers={"process.run": ReturnCode()})
        self.assertEqual(rejected, [])
        self.assertEqual((record["state"], record["completion_basis"]), ("completed", "verified"))

    def test_verified_basis_needs_one_verified_attempt(self):
        rt = self.runtime(verifiers={"process.run": ReturnCode()})
        rt.start()
        wid = rt.work.apply([create("w", "Objective")]).refs["w"]
        rt.act(run(["true"], work=wid))                  # verified successful
        del rt.verifiers["process.run"]
        rt.act(run(["true"], work=wid))                  # unverified, exit 0
        ids = [a["id"] for a in rt.work.attempts(wid, 5)]
        rt.work.apply([set_state(wid, "completed", "done", evidence=ids)])
        self.assertEqual(rt.work.get(wid).completion_basis, "verified")

    def test_f_verifier_failure_cannot_complete(self):
        # A verifier that rejects even exit 0: explicit verification failure wins.
        class Rejects:
            def verify(self, action, result):
                return Verification(Outcome.FAILURE, "objective not met")

        with self.assertLogs("kairo", "WARNING"):
            _, record, [rejected] = self.attempt_completion(
                ["true"], verifiers={"process.run": Rejects()})
        self.assertIn("verified_failed", rejected["reason"])
        self.assertEqual(record["state"], "active")

    def test_g_h_i_ownership_invention_and_closed_work(self):
        rt = self.runtime()
        rt.start()
        refs = rt.work.apply([create("a", "Work A"), create("b", "Work B")]).refs
        rt.act(run(["true"], work=refs["b"]))
        foreign = rt.work.attempts(refs["b"], 1)[0]["id"]
        cases = {
            "another work's attempt": [foreign],
            "invented id": ["deadbeef" * 4],
        }
        for label, evidence in cases.items():
            with self.subTest(label):
                [r] = rt.work.apply([set_state(refs["a"], "completed", "done",
                                               evidence=evidence)]).rejected
                self.assertIn("not an attempt at this work", r["reason"])
        rt.work.apply([set_state(refs["b"], "completed", "done", evidence=[foreign])])
        closed = rt.work.apply([set_state(refs["b"], "completed", "again", evidence=[foreign]),
                                update(refs["b"], understanding="rewrite history")]).rejected
        self.assertEqual(len(closed), 2)
        self.assertTrue(all("closed work does not change" in r["reason"] for r in closed))
        self.assertEqual(rt.work.get(refs["a"]).state, "active")

    def test_j_cognition_cannot_set_the_basis(self):
        forged = {**set_state("x", "completed", "done", evidence=["a"]), "completion_basis": "verified"}
        with self.assertRaises(CognitionError):  # a provider's answer: refused outright
            parse_decision({"reason": "", "actions": [], "replies": [], "sleep": True,
                            "wake_after": None, "work": [forged]}, ACTIONS)
        with self.assertRaises(CognitionError):  # nor through an update
            parse_decision({"reason": "", "actions": [], "replies": [], "sleep": True,
                            "wake_after": None,
                            "work": [{**update("x", understanding="u"), "completion_basis": "verified"}]},
                           ACTIONS)
        # An in-process provider bypassing the parser still cannot choose it.
        rt = self.runtime()
        rt.start()
        wid = rt.work.apply([create("w", "Objective")]).refs["w"]
        rt.act(run(["true"], work=wid))
        attempt = rt.work.attempts(wid, 1)[0]["id"]
        rt.work.apply([{**set_state(wid, "completed", "done", evidence=[attempt]),
                        "completion_basis": "verified"}])
        self.assertEqual(rt.work.get(wid).completion_basis, "unverified")

    def test_k_situation_distinguishes_verified_and_unverified(self):
        rt = self.runtime(verifiers={"process.run": ReturnCode()})
        rt.start()
        refs = rt.work.apply([create("v", "Verified objective"),
                              create("u", "Unverified objective")]).refs
        rt.act(run(["true"], work=refs["v"]))
        del rt.verifiers["process.run"]
        rt.act(run(["true"], work=refs["u"]))
        for key in ("v", "u"):
            attempt = rt.work.attempts(refs[key], 1)[0]["id"]
            rt.work.apply([set_state(refs[key], "completed", "done", evidence=[attempt])])
        s = build_situation(rt.context())
        basis = {w["objective"]: w["completion_basis"] for w in s["work"]["recently_closed"]}
        self.assertEqual(basis, {"Verified objective": "verified",
                                 "Unverified objective": "unverified"})
        legend = s["work"]["completion_basis"]
        self.assertIn("did not independently verify", legend)
        self.assertIn("cognition's judgment", legend)

    def test_l_basis_survives_restart_in_a_new_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            rt, record, _ = self.attempt_completion(["true"], path=db)
            rt.stop()
            rt.memory.close()
            out = subprocess.run([sys.executable, "-m", "kairo", "--situation", "--db", str(db)],
                                 capture_output=True, text=True, timeout=TIMEOUT,
                                 env={**os.environ, "PYTHONPATH": str(SRC)})
            self.assertEqual(out.returncode, 0, out.stderr)
            [closed] = json.loads(out.stdout)["work"]["recently_closed"]
            self.assertEqual((closed["id"], closed["completion_basis"]),
                             (record["id"], "unverified"))
            again = self.runtime(path=db)
            self.assertEqual(again.work.get(record["id"]).completion_basis, "unverified")

    def test_m_legacy_and_malformed_basis_are_unknown_not_verified(self):
        rt = self.runtime()
        base = {"objective": "o", "why": "w", "state": "completed", "state_since": time.time()}
        rt.memory.put("work", "legacy", {**base, "id": "legacy"})  # written before the field
        rt.memory.put("work", "forged", {**base, "id": "forged", "completion_basis": "super-verified"})
        rt.memory.put("work", "typed", {**base, "id": "typed", "completion_basis": 1})
        rt.start()
        report = rt.cycle()  # no crash
        self.assertIs(report.state, State.SLEEPING)
        s = build_situation(rt.context())
        basis = {w["id"]: w["completion_basis"] for w in s["work"]["recently_closed"]}
        self.assertEqual(basis, {"legacy": "unknown", "forged": "unknown", "typed": "unknown"})
        self.assertIsNone(rt.work.get("legacy").completion_basis)  # still readable


class ParseValidationTest(unittest.TestCase):
    def decision(self, work=(), actions=()):
        return {"reason": "r", "actions": list(actions), "replies": [], "sleep": True,
                "wake_after": None, "work": list(work)}

    def assertInvalid(self, data):
        with self.assertRaises(CognitionError) as caught:
            parse_decision(data, ACTIONS)
        self.assertEqual(caught.exception.category, "invalid_decision")

    def test_valid_work_decision_parses(self):
        d = parse_decision(self.decision(
            work=[create("w", "Objective"), set_state("abc", "waiting", "r", wait_seconds=60)],
            actions=[{"kind": "process.run", "params": {"argv": ["ls"]}, "reason": "", "work": "w"}]),
            ACTIONS)
        self.assertEqual([r["op"] for r in d.work], ["create", "set_state"])
        self.assertEqual(d.actions[0].work_id, "w")

    def test_structurally_invalid_requests_are_rejected(self):
        bad = {
            "missing work field": {k: v for k, v in self.decision().items() if k != "work"},
            "work not a list": {**self.decision(), "work": {"op": "create"}},
            "unknown op": self.decision(work=[{"op": "delete", "work_id": "x"}]),
            "missing field": self.decision(work=[{"op": "update", "work_id": "x"}]),
            "extra field": self.decision(work=[{**update("x", understanding="u"), "state": "done"}]),
            "wrong type": self.decision(work=[update(7, understanding="u")]),
            "null objective": self.decision(work=[{**create("w", "o"), "objective": None}]),
            "duplicate refs": self.decision(work=[create("w", "a"), create("w", "b")]),
            "empty ref": self.decision(work=[create("", "a")]),
            "too many": self.decision(work=[create(f"w{i}", f"o{i}") for i in range(MAX_REQUESTS + 1)]),
            "evidence not list": self.decision(work=[{**set_state("x", "completed"), "evidence": "a"}]),
            "wait not number": self.decision(work=[set_state("x", "waiting", wait_seconds="1h")]),
            "action work not str": self.decision(actions=[{"kind": "process.run",
                                                           "params": {"argv": ["ls"]},
                                                           "reason": "", "work": 3}]),
            "action without work field": self.decision(actions=[{"kind": "process.run",
                                                                 "params": {"argv": ["ls"]},
                                                                 "reason": ""}]),
        }
        for label, data in bad.items():
            with self.subTest(label):
                self.assertInvalid(data)

    def test_schema_describes_the_contract(self):
        schema = decision_schema(ACTIONS)
        self.assertIn("work", schema["required"])
        ops = {v["properties"]["op"]["const"] for v in schema["properties"]["work"]["items"]["anyOf"]}
        self.assertEqual(ops, {"create", "update", "set_state"})
        action = schema["properties"]["actions"]["items"]["anyOf"][0]
        self.assertIn("work", action["required"])


class RuntimeValidationTest(WorkCase):
    def setUp(self):
        self.rt = self.runtime()
        self.rt.start()
        self.rt.work.apply([create("w", "Primary objective")])
        self.wid = self.rt.work.all()[0].id

    def rejected(self, *requests):
        outcome = self.rt.work.apply(list(requests))
        self.assertEqual(outcome.applied, [], outcome)
        return [r["reason"] for r in outcome.rejected]

    def test_semantic_rejections(self):
        self.rt.work.apply([create("o", "Other objective")])
        other_id = [w.id for w in self.rt.work.all() if w.objective == "Other objective"][0]
        self.rt.act(run(["true"], work=other_id))
        foreign = self.rt.work.attempts(other_id, 1)[0]["id"]
        self.rt.act(run(["/nonexistent/binary"], work=self.wid))  # failed_to_execute
        failed = self.rt.work.attempts(self.wid, 1)[0]["id"]
        cases = {
            "unknown id": (update("no-such-work", understanding="x"), "no work"),
            "invented id format": (set_state("'; DROP TABLE records; --", "blocked"), "no work"),
            "same state": (set_state(self.wid, "active"), "cannot go from active to active"),
            "unknown state": (set_state(self.wid, "paused"), "unknown work state"),
            "empty reason": (set_state(self.wid, "blocked", reason=" "), "non-empty"),
            "no evidence": (set_state(self.wid, "completed"), "needs evidence"),
            "foreign evidence": (set_state(self.wid, "completed", evidence=[foreign]),
                                 "not an attempt at this work"),
            "failed evidence": (set_state(self.wid, "completed", evidence=[failed]),
                                "cannot be evidence"),
            "evidence elsewhere": (set_state(self.wid, "blocked", evidence=[failed]),
                                   "only applies to completion"),
            "wait on non-waiting": (set_state(self.wid, "blocked", wait_seconds=60),
                                    "only applies to waiting"),
            "absurd wait": (set_state(self.wid, "waiting", wait_seconds=-5), "wait_seconds"),
            "duplicate objective": (create("dup", "  primary   OBJECTIVE "), "already has this objective"),
            "missing directive": (create("d", "New thing", directive_id="nope"), "no active directive"),
            "oversized": (update(self.wid, understanding="u" * (TEXT_LIMITS["understanding"] + 1)),
                          "longer than"),
            "noop update": (update(self.wid), "changes nothing"),
        }
        for label, (request, expected) in cases.items():
            with self.subTest(label):
                [reason] = self.rejected(request)
                self.assertIn(expected, reason)
        self.assertEqual(self.rt.work.get(self.wid).state, "active")  # nothing changed

    def test_too_many_open_items(self):
        self.rt.work.apply([create(f"x{i}", f"Objective {i}") for i in range(MAX_OPEN - 1)])
        [reason] = self.rejected(create("one-more", "Objective beyond the limit"))
        self.assertIn("open work items", reason)

    def test_directive_link(self):
        d = self.rt.directives.add("Keep the host healthy.")
        outcome = self.rt.work.apply([create("h", "Check disk trends", directive_id=d.id)])
        work = self.rt.work.get(outcome.refs["h"])
        self.assertEqual(work.directive_id, d.id)
        s = build_situation(self.rt.context())
        self.assertEqual([w["directive_id"] for w in open_work(s) if w["id"] == work.id], [d.id])

    def test_actions_linked_to_unknown_work_run_unlinked_and_are_reported(self):
        cognition = plan(Decision(actions=[run(["true"], work="imaginary")]), Decision(sleep=True))
        rt = self.runtime(cognition)
        with self.assertLogs("kairo", "WARNING"):
            self.cycles(rt, 2)
        [action] = rt.memory.all("action")
        self.assertIsNone(action["work_id"])
        threads = cognition.situations[1]["open_threads"]
        self.assertIn("no open work 'imaginary'", threads["work_requests_rejected"][0]["reason"])

    def test_rejections_do_not_block_valid_requests(self):
        outcome = self.rt.work.apply([update("nope", understanding="x"),
                                      update(self.wid, understanding="real progress")])
        self.assertEqual(len(outcome.rejected), 1)
        self.assertEqual(self.rt.work.get(self.wid).understanding, "real progress")


class CapabilitiesTest(WorkCase):
    def test_work_interface_is_advertised_as_a_capability(self):
        s = build_situation(self.runtime().context())
        text = s["capabilities"]["work_requests"]
        for op in ("create", "update", "set_state", "evidence"):
            self.assertIn(op, text)
        self.assertEqual(s["work"]["open"], [])
        self.assertIn("interpretation", s["work"]["note"])


if __name__ == "__main__":
    unittest.main()
