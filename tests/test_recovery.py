"""Failure handling and recovery: failures as runtime facts, diagnosis as
cognition's interpretation, no blind repetition, waits that wake Kairo.

Cognition is a deterministic script reading the situation it is shown. The
tests check structure (states, kinds, revisions, counts, refusals), never the
wording of any diagnosis.
"""

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from kairo import Action, Decision, Environment, Memory, Outcome, Runtime, State, Verification
from kairo.actions import FAILURE_KINDS, action_state, failure_of
from kairo.cognition import CognitionError, parse_decision
from kairo.environment import ACTIONS
from kairo.redact import MARKER
from kairo.situation import build_situation, render_situation
from test_continuous import SRC, TIMEOUT
from test_work import (
    ReturnCode, Script, WorkCase, create, only_open, open_work, plan, run, set_state, update,
)

PY = sys.executable


def fail_with(code, stderr="boom"):
    return [PY, "-c", f"import sys; sys.stderr.write({stderr!r}); sys.exit({code})"]


def recovery(situation):
    return only_open(situation)["recovery"]


def refused(report):
    return [r for r in (report.cognition.get("work") or {}).get("rejected") or []
            if r["op"] == "action_refused"]


class RecoveryCase(WorkCase):
    def work_runtime(self, **kwargs):
        rt = self.runtime(**kwargs)
        rt.start()
        wid = rt.work.apply([create("w", "Restore the service", strategy="restart it")]).refs["w"]
        return rt, wid


# -- A, B, C: failures as runtime facts -----------------------------------------------


class FailureFactsTest(RecoveryCase):
    def test_a_each_failure_kind_is_recorded_from_the_real_exception(self):
        class Exploding(Environment):
            def execute(self, action):
                if action.params.get("argv") == ["explode"]:
                    raise RuntimeError("executor bug")
                return super().execute(action)

        rt, wid = self.work_runtime(environment=Exploding())
        with tempfile.NamedTemporaryFile() as not_a_dir:
            cases = {
                "not_found": run(["/nonexistent/tool"], work=wid),
                "permission_denied": run(["/etc/shadow"], work=wid),
                "timed_out": Action("process.run", {"argv": ["sleep", "5"], "timeout": 0.2},
                                    work_id=wid),
                "invalid_params": Action("process.run", {"argv": ["ls"], "timeout": -1},
                                         work_id=wid),
                "os_error": Action("process.run", {"argv": ["ls"], "cwd": not_a_dir.name},
                                   work_id=wid),
                "executor_error": run(["explode"], work=wid),
            }
            with self.assertLogs("kairo", "ERROR"):  # the executor crash is logged
                for kind, action in cases.items():
                    rt.act(action)
        for kind, action in cases.items():
            with self.subTest(kind):
                record = rt.memory.get("action", action.id)
                self.assertEqual(record["result"]["failure"], kind)
                self.assertEqual((action_state(record), failure_of(record)),
                                 ("failed_to_execute", kind))
        self.assertTrue({r["result"]["failure"] for r in rt.memory.all("action")} <= FAILURE_KINDS)

    def test_a_exit_codes_are_numbers_not_causes(self):
        rt, wid = self.work_runtime()
        step = rt.act(run(["sh", "-c", "nonexistent-command-xyz"], work=wid))
        record = rt.memory.get("action", step.action.id)
        self.assertEqual(record["result"]["output"]["returncode"], 127)
        self.assertIsNone(record["result"]["failure"])  # never "not_found" from an exit code
        self.assertEqual(failure_of(record), "exited_nonzero")

    def test_b_nonzero_exit_is_a_visible_failure(self):
        rt, wid = self.work_runtime()
        step = rt.act(run(["ls", "/definitely/missing"], work=wid))
        s = build_situation(rt.context())
        [attempt] = only_open(s)["recent_attempts"]
        self.assertEqual((attempt["state"], attempt["failure"], attempt["returncode"]),
                         ("exited_nonzero", "exited_nonzero", 2))
        self.assertIn("cannot access", attempt["problem"])
        self.assertEqual(only_open(s)["attempts_with_current_strategy"], {"attempts": 1, "failed": 1})
        self.assertEqual([a["id"] for a in s["open_threads"]["actions_failed"]], [step.action.id])
        self.assertEqual(recovery(s)["latest_failure"]["failure"], "exited_nonzero")

    def test_c_failure_detail_is_bounded_and_redacted(self):
        secret = "sk-recovery-0123456789abcdef"
        with mock.patch.dict(os.environ, {"DEPLOY_API_KEY": secret}):
            rt, wid = self.work_runtime()
            rt.act(run(["sh", "-c", 'echo "key=$DEPLOY_API_KEY $(head -c 5000 /dev/zero | tr "\\0" x)" >&2; exit 1'],
                       work=wid))
            s = build_situation(rt.context())
            text = render_situation(s)
        self.assertNotIn(secret, text)
        detail = recovery(s)["latest_failure"]["detail"]
        self.assertTrue(detail.startswith("key=" + MARKER))
        self.assertLess(len(detail), 400)
        self.assertLess(len(only_open(s)["recent_attempts"][0]["problem"]), 400)
        self.assertNotIn(secret, "\n".join(rt.memory._db.iterdump()))


# -- D, E, F, G: failure context survives time -----------------------------------------------


class SurvivalTest(RecoveryCase):
    def first_failure_then(self, *later):
        return plan(Decision(work=[create("w", "Restore the service", strategy="restart it")],
                             actions=[run(fail_with(3), work="w")], sleep=False), *later)

    def test_d_next_cycle_sees_the_latest_failure(self):
        cognition = self.first_failure_then()
        rt = self.runtime(cognition)
        self.cycles(rt, 2)
        r = recovery(cognition.situations[1])
        self.assertEqual((r["latest_failure"]["failure"], r["latest_failure"]["returncode"],
                          r["latest_failure"]["strategy_revision"]), ("exited_nonzero", 3, 1))
        self.assertEqual(r["latest_failure"]["detail"], "boom")
        self.assertIs(r["diagnosis_since_latest_failure"], False)

    def test_e_sleep_and_wake_keep_the_failure_context(self):
        cognition = self.first_failure_then(Decision(sleep=True), Decision(sleep=True))
        rt = self.runtime(cognition)
        self.cycles(rt, 4)  # fail, sleep, wake, sleep, wake
        ids = {recovery(s)["latest_failure"]["action_id"] for s in cognition.situations[1:]}
        self.assertEqual(len(ids), 1)

    def test_f_a_new_process_reconstructs_the_failure_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            rt = self.runtime(self.first_failure_then(), path=db)
            self.cycles(rt, 1)
            [failed] = rt.memory.all("action")
            rt.stop()
            rt.memory.close()
            out = subprocess.run([PY, "-m", "kairo", "--situation", "--db", str(db)],
                                 capture_output=True, text=True, timeout=TIMEOUT,
                                 env={**os.environ, "PYTHONPATH": str(SRC)})
            self.assertEqual(out.returncode, 0, out.stderr)
            r = recovery(json.loads(out.stdout))
            self.assertEqual((r["latest_failure"]["action_id"], r["latest_failure"]["failure"]),
                             (failed["id"], "exited_nonzero"))
            self.assertEqual(r["revisions"][0]["failed"], 1)

    def test_g_provider_failure_is_not_attributed_to_work(self):
        class Flaky:
            name = "flaky"
            calls = 0
            seen = None

            def decide(self, context):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    return Decision(work=[create("w", "Restore the service")],
                                    actions=[run(fail_with(1), work="w")], sleep=False)
                if Flaky.calls == 2:
                    raise RuntimeError("provider down")
                Flaky.seen = build_situation(context)
                return Decision(sleep=True)

        rt = self.runtime(Flaky())
        with self.assertLogs("kairo", "ERROR"):
            self.cycles(rt, 1)
            [before] = rt.memory.all("work")
            self.cycles(rt, 2)
        [after] = rt.memory.all("work")
        self.assertEqual(after, before)  # the provider failure touched no work
        failed_cycle = [c for c in rt.memory.all("cycle") if c["cognition"]["result"] == "failed"]
        self.assertNotIn("work", failed_cycle[0]["cognition"])
        self.assertEqual(recovery(Flaky.seen)["latest_failure"]["failure"], "exited_nonzero")
        self.assertEqual(len(rt.memory.all("action")), 1)


# -- H, I, J, K, L: retries, strategies, repetition, diagnosis ----------------------------------


class StrategyAndRepetitionTest(RecoveryCase):
    def test_h_justified_retry_keeps_the_revision(self):
        rt, wid = self.work_runtime()
        rt.act(run(fail_with(1), work=wid))
        rt.work.apply([update(wid, understanding="looks transient")])
        rt.act(run(fail_with(1), work=wid))
        self.assertEqual([a["strategy_revision"] for a in rt.work.attempts(wid, 5)], [1, 1])

    def test_i_strategy_change_gets_a_new_revision(self):
        rt, wid = self.work_runtime()
        rt.act(run(fail_with(1), work=wid))
        rt.work.apply([update(wid, strategy="reinstall it")])
        rt.act(run(["true"], work=wid))
        work = rt.work.get(wid)
        self.assertEqual(work.strategy_revision, 2)
        self.assertEqual([(e["revision"], e["text"]) for e in work.strategy_log],
                         [(1, "restart it"), (2, "reinstall it")])
        revisions = recovery(build_situation(rt.context()))["revisions"]
        self.assertEqual([(r["revision"], r["strategy"], r["failed"], r["succeeded"]) for r in revisions],
                         [(2, "reinstall it", 0, 1), (1, "restart it", 1, 0)])

    def test_j_k_repeated_identical_failures_and_reset(self):
        rt, wid = self.work_runtime()

        def attempt(argv, note):
            rt.work.apply([update(wid, understanding=note)])
            rt.act(run(argv, work=wid))
            return recovery(build_situation(rt.context()))["repeated_identical_failures"]

        self.assertEqual(attempt(fail_with(1), "try"), 1)
        self.assertEqual(attempt(fail_with(1), "again"), 2)
        self.assertEqual(attempt(fail_with(1), "once more"), 3)
        self.assertEqual(attempt(fail_with(2), "different"), 1)  # different attempt: new run
        self.assertEqual(attempt(["true"], "worked"), 0)          # success resets
        self.assertEqual(attempt(fail_with(1), "broke again"), 1)
        attempts = rt.work.attempts(wid, 10)
        self.assertEqual(len({a["id"] for a in attempts}), 6)      # every attempt distinct

    def test_l_diagnosis_is_interpretation_its_timing_is_a_fact(self):
        rt, wid = self.work_runtime()
        rt.act(run(fail_with(1), work=wid))
        self.assertIs(recovery(build_situation(rt.context()))["diagnosis_since_latest_failure"], False)
        rt.work.apply([update(wid, understanding="I think permissions are wrong")])
        s = build_situation(rt.context())
        self.assertIs(recovery(s)["diagnosis_since_latest_failure"], True)
        # The diagnosis never becomes a fact: the failure kind is unchanged.
        self.assertEqual(recovery(s)["latest_failure"]["failure"], "exited_nonzero")
        self.assertEqual(only_open(s)["understanding"], "I think permissions are wrong")
        self.assertIn("interpretation", s["work"]["note"])
        # A new failure after the diagnosis is again undiagnosed.
        rt.act(run(fail_with(2), work=wid))
        self.assertIs(recovery(build_situation(rt.context()))["diagnosis_since_latest_failure"], False)

    def test_l_cognition_cannot_forge_recovery_facts(self):
        with self.assertRaises(CognitionError):
            parse_decision({"reason": "", "actions": [], "replies": [], "sleep": True,
                            "wake_after": None, "work": [{**update("x", understanding="u"),
                                                          "understanding_at": 9e12}]}, ACTIONS)
        rt, wid = self.work_runtime()
        before = time.time()
        rt.work.apply([{**update(wid, understanding="u"), "understanding_at": 9e12,
                        "strategy_revision": 99, "strategy_log": []}])
        work = rt.work.get(wid)
        self.assertLess(work.understanding_at, before + 60)
        self.assertEqual((work.strategy_revision, len(work.strategy_log)), (1, 1))


# -- M, W: completion stays honest ---------------------------------------------------------


class CompletionTest(RecoveryCase):
    def test_m_failures_and_interruptions_are_never_evidence(self):
        rt, wid = self.work_runtime(verifiers={"process.run": ReturnCode()})
        rt.act(run(fail_with(1), work=wid))                        # verified_failed
        del rt.verifiers["process.run"]
        rt.act(run(fail_with(1), work=wid))                        # exited_nonzero
        rt.act(run(["/nonexistent/tool"], work=wid))               # failed_to_execute
        rt.memory.put("action", "cut", {"id": "cut", "kind": "process.run", "work_id": wid,
                                        "params": {"argv": ["x"]}, "status": "interrupted",
                                        "started_at": time.time()})
        for attempt in rt.work.attempts(wid, 10):
            with self.subTest(state=action_state(attempt)):
                [r] = rt.work.apply([set_state(wid, "completed", "done",
                                               evidence=[attempt["id"]])]).rejected
                self.assertIn("cannot be evidence", r["reason"])
        self.assertEqual(rt.work.get(wid).state, "active")

    def test_w_completion_basis_is_unchanged(self):
        rt, wid = self.work_runtime()
        rt.act(run(fail_with(1), work=wid))
        rt.act(run(["true"], work=wid))
        ok = rt.work.attempts(wid, 1)[0]["id"]
        rt.work.apply([set_state(wid, "completed", "done", evidence=[ok])])
        self.assertEqual(rt.work.get(wid).completion_basis, "unverified")


# -- N, O, P: robustness and independence --------------------------------------------------------


class IndependenceTest(RecoveryCase):
    def test_n_malformed_and_legacy_recovery_data(self):
        cognition = plan(Decision(sleep=True))
        rt = self.runtime(cognition)
        rt.memory.put("work", "legacy", {"id": "legacy", "state": "active", "objective": "old",
                                         "why": "w", "strategy_revision": 3})  # pre-Phase-6 record
        rt.memory.put("work", "odd", {"id": "odd", "state": "active", "objective": "odd", "why": "w",
                                      "understanding_at": "yesterday", "strategy_log": "garbage"})
        rt.memory.put("action", "weird", {"id": "weird", "work_id": "odd", "kind": "process.run",
                                          "params": {}, "status": "finished", "finished_at": 1.0,
                                          "result": {"executed": False, "failure": "made_up"}})
        rt.memory.put("action", "broken", {"id": "broken", "work_id": "odd", "result": "garbage",
                                           "status": "finished"})
        [report] = self.cycles(rt, 1)
        self.assertIs(report.state, State.SLEEPING)
        s = cognition.situations[0]
        self.assertNotIn("work", s["context"]["unavailable_sections"])
        items = {w["id"]: w for w in open_work(s)}
        self.assertEqual(items["odd"]["recovery"]["latest_failure"]["failure"], "unrecorded")
        self.assertIn({"action_id": "broken", "unreadable": True}, items["odd"]["recent_attempts"])
        self.assertIs(items["odd"]["recovery"]["diagnosis_since_latest_failure"], False)
        self.assertIsNone(items["legacy"]["recovery"]["latest_failure"])
        # The ledger still works with both, and a refusal check on them cannot crash.
        outcome = rt.work.apply([update("legacy", understanding="now diagnosed"),
                                 update("odd", strategy="new")])
        self.assertEqual(outcome.rejected, [])

    def test_o_work_items_fail_independently(self):
        cognition = Script(lambda s, n: {
            1: Decision(work=[create("a", "Alpha"), create("b", "Beta")],
                        actions=[run(fail_with(1), work="a"), run(["true"], work="b")], sleep=False),
        }.get(n) or Decision(
            actions=[run(fail_with(1), work=w["id"]) for w in open_work(s)], sleep=True))
        rt = self.runtime(cognition)
        reports = self.cycles(rt, 2)
        items = {w["objective"]: w for w in open_work(cognition.situations[1])}
        self.assertEqual(items["Alpha"]["recovery"]["latest_failure"]["failure"], "exited_nonzero")
        self.assertIsNone(items["Beta"]["recovery"]["latest_failure"])
        # The identical action is refused for Alpha (unreassessed failure) but runs for Beta.
        [r] = refused(reports[1])
        self.assertEqual(r["target"], items["Alpha"]["id"])
        beta = rt.work.attempts(items["Beta"]["id"], 5)
        self.assertEqual([action_state(a) for a in beta], ["executed_unverified", "exited_nonzero"])

    def test_p_unlinked_failures_do_not_touch_work(self):
        rt, wid = self.work_runtime()
        before = rt.memory.get("work", wid)
        rt.act(run(fail_with(1)))  # not linked
        self.assertEqual(rt.memory.get("work", wid), before)
        s = build_situation(rt.context())
        self.assertIsNone(recovery(s)["latest_failure"])
        self.assertEqual([a["work_id"] for a in s["open_threads"]["actions_failed"]], [None])


# -- Q, R, S, T: no blind repetition ------------------------------------------------------


class BlindRepetitionTest(RecoveryCase):
    def decide(self, rt, *decisions):
        rt.cognition = plan(*decisions)
        return self.cycles(rt, len(decisions))

    def test_q_exact_repeat_without_reassessment_is_refused(self):
        rt, wid = self.work_runtime()
        rt.act(run(fail_with(1), work=wid))
        with self.assertLogs("kairo", "WARNING"):
            [report] = self.decide(rt, Decision(actions=[run(fail_with(1), work=wid)], sleep=False))
        [r] = refused(report)
        self.assertEqual(r["repeats"], rt.work.attempts(wid, 1)[0]["id"])
        self.assertEqual(len(rt.memory.all("action")), 1)  # not executed
        self.assertEqual(report.steps, [])
        s = build_situation(rt.context())
        self.assertEqual(s["open_threads"]["attempts_refused"], [r])
        self.assertEqual(s["open_threads"]["work_requests_rejected"], [])

    def test_r_same_decision_diagnosis_allows_the_retry(self):
        rt, wid = self.work_runtime()
        rt.act(run(fail_with(1), work=wid))
        [report] = self.decide(rt, Decision(work=[update(wid, understanding="maybe transient")],
                                            actions=[run(fail_with(1), work=wid)], sleep=False))
        self.assertEqual(refused(report), [])
        self.assertEqual(len(rt.work.attempts(wid, 5)), 2)
        # Without a further reassessment, the next identical repeat is refused again.
        with self.assertLogs("kairo", "WARNING"):
            [again] = self.decide(rt, Decision(actions=[run(fail_with(1), work=wid)], sleep=False))
        self.assertEqual(len(refused(again)), 1)

    def test_s_alternating_attempts_are_still_caught(self):
        rt, wid = self.work_runtime()
        rt.act(run(fail_with(1), work=wid))   # A fails
        rt.act(run(fail_with(2), work=wid))   # B fails (different: allowed)
        with self.assertLogs("kairo", "WARNING"):
            [report] = self.decide(rt, Decision(actions=[run(fail_with(1), work=wid)], sleep=False))
        [r] = refused(report)                 # A again, never reassessed: refused
        self.assertEqual(r["repeats"], rt.work.attempts(wid, 5)[0]["id"])
        # A success in between does not make an unreassessed failure safe to repeat.
        rt.act(run(["true"], work=wid))
        with self.assertLogs("kairo", "WARNING"):
            [report] = self.decide(rt, Decision(actions=[run(fail_with(2), work=wid)], sleep=False))
        self.assertEqual(len(refused(report)), 1)

    def test_t_secrets_cannot_bypass_the_comparison_or_leak(self):
        secret = "sk-param-0123456789abcdef"
        with mock.patch.dict(os.environ, {"BACKUP_TOKEN": secret}):
            rt, wid = self.work_runtime()
            argv = [PY, "-c", f"import sys; sys.exit('{secret}' and 4)"]
            rt.act(run(argv, work=wid))
            stored = rt.work.attempts(wid, 1)[0]["params"]["argv"]
            self.assertNotIn(secret, json.dumps(stored))  # stored redacted...
            with self.assertLogs("kairo", "WARNING") as logs:
                [report] = self.decide(rt, Decision(actions=[run(argv, work=wid)], sleep=False))
        [r] = refused(report)                                # ...yet still recognised
        self.assertNotIn(secret, json.dumps(r) + "\n".join(logs.output))
        self.assertNotIn(secret, json.dumps(report.cognition))


# -- hardening: the reassessment rule through the real gated cycle path ------------------------


class GatedPathTest(RecoveryCase):
    """Every decision here goes cognition -> work requests -> action gate -> execution;
    nothing calls rt.act() directly."""

    A = fail_with(1, "still broken")

    def start_with_failure(self, **kwargs):
        """Cycle 1 through the real path: create work, attempt A, which fails."""
        rt = self.runtime(**kwargs)
        [report] = self.gated(rt, Decision(work=[create("w", "Restore the service",
                                                        strategy="restart it")],
                                           actions=[run(self.A, work="w")], sleep=False))
        [step] = report.steps
        self.assertEqual(action_state(rt.memory.get("action", step.action.id)), "exited_nonzero")
        return rt, rt.work.all()[0].id, step.action.id

    def gated(self, rt, decision):
        rt.cognition = plan(decision)
        return self.cycles(rt, 1)

    def retry(self, rt, wid, *work):
        """One cycle: the given work requests, then exact A linked to the work."""
        [report] = self.gated(rt, Decision(work=list(work), actions=[run(self.A, work=wid)],
                                           sleep=False))
        return report

    def next_situation(self, rt):
        """What the following cognition call is shown (through a real cycle)."""
        observer = Script(lambda s, n: Decision(sleep=True))
        rt.cognition = observer
        self.cycles(rt, 1)
        return observer.situations[0]

    def test_1_strategy_change_alone_does_not_clear_the_rule(self):
        rt, wid, failed = self.start_with_failure()
        with self.assertLogs("kairo", "WARNING"):
            report = self.retry(rt, wid, update(wid, strategy="reinstall it instead"))
        # The strategy change itself was applied: a new revision exists...
        [applied] = report.cognition["work"]["applied"]
        self.assertIn("strategy", applied["changed"])
        work = rt.work.get(wid)
        self.assertEqual((work.strategy_revision, work.understanding_at), (2, None))
        # ...but the identical failed action was refused and not executed.
        [r] = refused(report)
        self.assertEqual(r["repeats"], failed)
        self.assertEqual(report.steps, [])
        self.assertEqual([a["id"] for a in rt.work.attempts(wid, 10)], [failed])
        # The refusal is persisted and shown to the next cognition call.
        self.assertEqual(self.next_situation(rt)["open_threads"]["attempts_refused"], [r])

    def test_2_resending_the_same_understanding_does_not_clear_it(self):
        rt, wid, _ = self.start_with_failure()
        first = self.retry(rt, wid, update(wid, understanding="X: maybe the port was busy"))
        self.assertEqual((refused(first), len(first.steps)), ([], 1))  # really changed: allowed
        self.assertEqual(action_state(rt.memory.get("action", first.steps[0].action.id)),
                         "exited_nonzero")                               # and failed again
        before = rt.memory.get("work", wid)
        with self.assertLogs("kairo", "WARNING"):
            second = self.retry(rt, wid, update(wid, understanding="X: maybe the port was busy"))
        rejected = second.cognition["work"]["rejected"]
        self.assertIn("update changes nothing", [x["reason"] for x in rejected])
        [r] = refused(second)
        self.assertEqual(r["repeats"], first.steps[0].action.id)
        self.assertEqual(second.steps, [])
        # The no-op left no trace: no new timestamp, event or update time.
        self.assertEqual(rt.memory.get("work", wid), before)

    def test_3_no_hidden_retry_limit(self):
        rt, wid, _ = self.start_with_failure()
        verdicts = []  # what the gate itself returned for each request (None = allowed)
        original = rt.work.unsettled_repeat

        def recording(*args, **kwargs):
            verdicts.append(original(*args, **kwargs))
            return verdicts[-1]

        with mock.patch.object(rt.work, "unsettled_repeat", side_effect=recording) as spy:
            for i in range(1, 6):  # five justified identical retries in a row
                before = rt.work.get(wid).understanding_at
                report = self.retry(rt, wid, update(wid, understanding=f"hypothesis {i}"))
                with self.subTest(retry=i):
                    self.assertEqual(spy.call_count, i)            # consulted the gate...
                    self.assertIsNone(verdicts[-1])                # ...which allowed it
                    self.assertEqual(refused(report), [])
                    [step] = report.steps                           # ...and it really ran
                    self.assertEqual(action_state(rt.memory.get("action", step.action.id)),
                                     "exited_nonzero")
                    after = rt.work.get(wid).understanding_at
                    self.assertGreater(after, before or 0)         # because understanding changed
            self.assertEqual(len(rt.work.attempts(wid, 20)), 6)
            # The rule is still live after five retries: without a change, A is refused.
            with self.assertLogs("kairo", "WARNING"):
                final = self.retry(rt, wid)
            self.assertEqual(len(refused(final)), 1)
            self.assertEqual(spy.call_count, 6)
            self.assertIsNotNone(verdicts[-1])
        r = recovery(build_situation(rt.context()))
        self.assertEqual((r["revisions"][0]["attempts"], r["revisions"][0]["failed"]), (6, 6))

    def test_4_reassessment_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            reassessed, untouched = Path(tmp) / "reassessed.db", Path(tmp) / "untouched.db"
            for db, reassess in ((reassessed, True), (untouched, False)):
                rt, wid, failed = self.start_with_failure(path=db)
                if reassess:  # cycle 2: cognition records its understanding, no action
                    self.gated(rt, Decision(work=[update(wid, understanding="U1: config missing")],
                                            sleep=True))
                else:
                    self.gated(rt, Decision(sleep=True))
                rt.stop()
                rt.memory.close()

            # A new OS process reads the persisted understanding and recovery facts.
            out = subprocess.run([PY, "-m", "kairo", "--situation", "--db", str(reassessed)],
                                 capture_output=True, text=True, timeout=TIMEOUT,
                                 env={**os.environ, "PYTHONPATH": str(SRC)})
            self.assertEqual(out.returncode, 0, out.stderr)
            seen = only_open(json.loads(out.stdout))
            self.assertEqual(seen["understanding"], "U1: config missing")
            self.assertIs(seen["recovery"]["diagnosis_since_latest_failure"], True)

            # A genuinely new Runtime (new Memory, new ledger) on each database.
            results = {}
            for db in (reassessed, untouched):
                fresh = Runtime(Memory(db))
                self.addCleanup(fresh.memory.close)
                [work] = fresh.work.all()
                failure = fresh.work.attempts(work.id, 5)[0]
                failed_at = failure["finished_at"]
                observer = Script(lambda s, n: Decision(actions=[run(self.A, work=s["work"]["open"][0]["id"])],
                                                        sleep=True))
                fresh.cognition = observer
                with self.assertLogs("kairo", "WARNING") if db == untouched else contextlib.nullcontext():
                    [report] = self.cycles(fresh, 1)
                results[db.name] = (work, failed_at, observer.situations[0], report)

            work, failed_at, situation, report = results["reassessed.db"]
            self.assertEqual(work.understanding, "U1: config missing")
            self.assertGreater(work.understanding_at, failed_at)
            self.assertIs(recovery(situation)["diagnosis_since_latest_failure"], True)
            self.assertEqual(only_open(situation)["understanding"], "U1: config missing")
            self.assertEqual((refused(report), len(report.steps)), ([], 1))  # persisted reassessment counts

            work, failed_at, situation, report = results["untouched.db"]
            self.assertIsNone(work.understanding_at)
            self.assertIs(recovery(situation)["diagnosis_since_latest_failure"], False)
            self.assertEqual((len(refused(report)), report.steps), (1, []))  # no reassessment: refused

    def test_5_understanding_crosses_a_real_cycle_boundary(self):
        def think(s, n):
            if n == 1:
                return Decision(work=[create("w", "Restore the service")], sleep=False)
            w = only_open(s)
            if n == 2:  # the only place the text exists in this test's cognition
                return Decision(work=[update(w["id"], understanding="the unit file is masked")],
                                sleep=True)
            return Decision(sleep=True)  # cycle 3 only reads what it is shown

        cognition = Script(think)
        rt = self.runtime(cognition)
        self.cycles(rt, 1)
        self.assertEqual(open_work(cognition.situations[0]), [])  # created during cycle 1
        self.cycles(rt, 2)  # cycle 2 updates and sleeps; cycle 3 runs after a wake
        self.assertEqual(only_open(cognition.situations[1])["understanding"], "")  # before the update
        third = only_open(cognition.situations[2])
        self.assertEqual(third["understanding"], "the unit file is masked")
        self.assertIn("age_seconds", third["updated"])
        # It reached cognition from the stored work record (what the situation is built from).
        stored = rt.memory.get("work", third["id"])
        self.assertEqual(stored["understanding"], third["understanding"])
        self.assertEqual(cognition.situations[2]["now"]["wake_reason"], "next cycle")


# -- U: interrupted actions are indeterminate ------------------------------------------------


class InterruptedTest(RecoveryCase):
    def test_u_interrupted_is_indeterminate_not_failure_nor_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            rt, wid = self.work_runtime(path=db)
            argv = ["sh", "-c", "echo migrating"]
            # What a process killed mid-action leaves behind (the params as stored).
            rt.memory.put("action", "cut", {"id": "cut", "kind": "process.run", "work_id": wid,
                                            "params": {"argv": argv}, "reason": "migrate",
                                            "strategy_revision": 1, "status": "started",
                                            "started_at": time.time()})
            rt.memory.close()
            again = self.runtime(path=db)
            with self.assertLogs("kairo", "WARNING"):
                again.start()  # recovery marks it interrupted
            s = build_situation(again.context())
            w = only_open(s)
            [attempt] = w["recent_attempts"]
            self.assertEqual((attempt["state"], attempt["failure"], attempt["outcome"]),
                             ("interrupted", None, "indeterminate"))
            self.assertIsNone(w["recovery"]["latest_failure"])
            self.assertEqual((w["recovery"]["revisions"][0]["failed"],
                              w["recovery"]["revisions"][0]["succeeded"],
                              w["recovery"]["revisions"][0]["outcome_unknown"]), (0, 0, 1))
            self.assertEqual(s["open_threads"]["actions_outcome_unknown"][0]["id"], "cut")
            self.assertEqual(s["open_threads"]["actions_failed"], [])
            self.assertEqual(w["state"], "active")  # no automatic state change
            # Not known to be safe to repeat: an exact repeat needs a reassessment first.
            with self.assertLogs("kairo", "WARNING"):
                [report] = BlindRepetitionTest.decide(
                    self, again, Decision(actions=[run(argv, work=wid)], sleep=False))
            [r] = refused(report)
            self.assertIn("(interrupted)", r["reason"])
            [ok] = BlindRepetitionTest.decide(
                self, again, Decision(work=[update(wid, understanding="checked: not applied")],
                                      actions=[run(argv, work=wid)], sleep=True))
            self.assertEqual(refused(ok), [])
            # And never completion evidence.
            [rej] = again.work.apply([set_state(wid, "completed", "done", evidence=["cut"])]).rejected
            self.assertIn("interrupted", rej["reason"])


# -- V: waiting deadlines wake Kairo ------------------------------------------------------------


class WaitingDeadlineTest(RecoveryCase):
    def test_v_earliest_wait_wakes_kairo_without_changing_work(self):
        def think(s, n):
            if n == 1:
                return Decision(work=[create("soon", "Retry the mirror"), create("later", "Recheck")],
                                sleep=False)
            if n == 2:
                ids = {w["objective"]: w["id"] for w in open_work(s)}
                return Decision(work=[
                    set_state(ids["Retry the mirror"], "waiting", "mirror is down", wait_seconds=0.3),
                    set_state(ids["Recheck"], "waiting", "tomorrow", wait_seconds=3600)], sleep=True)
            return Decision(sleep=True)

        cognition = Script(think)
        rt = self.runtime(cognition)  # no normal deadline: only waits wake it
        thread = threading.Thread(target=rt.run_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (rt.request_stop(), thread.join(TIMEOUT)))
        deadline = time.time() + TIMEOUT
        while len(cognition.situations) < 3 and time.time() < deadline:
            rt.wait_for(State.AWAKE, 0.2)
        self.assertGreaterEqual(len(cognition.situations), 3, "Kairo did not wake for the wait")
        soon = [w for w in open_work(cognition.situations[2]) if w["objective"] == "Retry the mirror"][0]
        self.assertEqual(cognition.situations[2]["now"]["wake_reason"], f"wait elapsed for work {soon['id']}")
        self.assertEqual(soon["state"], "waiting")  # the runtime did not resume it
        self.assertIs(soon["wait_elapsed"], True)
        # An elapsed wait does not wake Kairo again: next deadline is the 1-hour wait.
        self.assertTrue(rt.wait_for(State.SLEEPING, TIMEOUT))
        self.assertFalse(rt.wait_for(State.AWAKE, 0.5))
        self.assertEqual(len(cognition.situations), 3)
        later = [w for w in rt.work.open() if w.objective == "Recheck"][0]
        self.assertAlmostEqual(rt.status()["wake_at"], later.waiting_until, delta=0.01)

    def test_v_an_earlier_normal_deadline_still_wins(self):
        rt, wid = self.work_runtime(reassess_after=60)
        rt.work.apply([set_state(wid, "waiting", "tomorrow", wait_seconds=3600)])
        before = time.time()
        rt.sleep("nothing to do")
        self.assertAlmostEqual(rt.status()["wake_at"], before + 60, delta=1)
        self.assertEqual(rt._deadline_reason, "reassessment due")
        rt.wake("test")
        rt.sleep("nothing to do", wake_after=7200)  # now the wait is earlier
        self.assertAlmostEqual(rt.status()["wake_at"], rt.work.get(wid).waiting_until, delta=0.01)
        self.assertEqual(rt._deadline_reason, f"wait elapsed for work {wid}")


# -- X: no second store ---------------------------------------------------------------------


class NoFailureStoreTest(RecoveryCase):
    def test_x_record_kinds_and_tables_are_unchanged(self):
        rt, wid = self.work_runtime()
        rt.act(run(fail_with(1), work=wid))
        rt.work.apply([update(wid, understanding="u", strategy="s2")])
        rt.act(run(["/nonexistent"], work=wid))
        rt.cycle()
        kinds = {k for (k,) in rt.memory._db.execute("SELECT DISTINCT kind FROM records")}
        self.assertTrue(kinds <= {"runtime", "action", "cycle", "work", "message", "directive", "todo"})
        tables = {t for (t,) in rt.memory._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"records"})


# -- the mandatory end-to-end lifecycle --------------------------------------------------------


class ConfigPresent:
    """A world-state verifier: success once the config file exists, otherwise it
    cannot say (unverifiable), so a failing attempt stays exited_nonzero."""

    def __init__(self, path):
        self.path = path

    def verify(self, action, result):
        if self.path.exists() and self.path.read_text() == "port=8080\n":
            return Verification(Outcome.SUCCESS, "config present")
        return Verification(Outcome.UNVERIFIABLE, "config absent")


class EndToEndRecoveryTest(RecoveryCase):
    def scenario(self, tmp, finish, branch_at):
        """Cycles 1-3 are shared (fail, refused blind repeat, justified retry that fails
        again). ``finish(s, work)`` decides cycle ``branch_at``: 4 to branch right after
        the second failure, 5 to branch after the strategy change succeeded."""
        conf = Path(tmp) / "service.conf"
        failing = fail_with(3, "no packaged default\n")
        writing = [PY, "-c", f"open({str(conf)!r}, 'w').write('port=8080\\n')"]
        checks = {}

        def think(s, n):
            if n == 1:  # work with strategy revision 1, first attempt
                return Decision(work=[create("w", "Service config must exist",
                                             strategy="copy the packaged default")],
                                actions=[run(failing, work="w")], sleep=False)
            w = only_open(s) if open_work(s) else None
            if n == 2:  # sees the failure; blindly repeats it
                checks["seen"] = (w["recovery"]["latest_failure"]["failure"],
                                  w["recovery"]["latest_failure"]["strategy_revision"],
                                  w["recovery"]["diagnosis_since_latest_failure"])
                return Decision(actions=[run(failing, work=w["id"])], sleep=False)
            if n == 3:  # sees the refusal; reassesses; justified identical retry
                checks["refused"] = s["open_threads"]["attempts_refused"]
                return Decision(work=[update(w["id"], understanding="may have been transient")],
                                actions=[run(failing, work=w["id"])], sleep=False)
            if n == 4:  # two distinct failures of revision 1
                checks["repeated"] = w["recovery"]["repeated_identical_failures"]
                checks["rev1"] = [(a["action_id"], a["strategy_revision"], a["state"])
                                  for a in w["recent_attempts"]]
            if n == branch_at:
                return finish(s, w)
            if n == 4:  # change strategy
                return Decision(work=[update(w["id"], understanding="no default exists",
                                             strategy="write the config directly")],
                                actions=[run(writing, work=w["id"])], sleep=False)
            return Decision(sleep=True)

        cognition = Script(think)
        rt = self.runtime(cognition, verifiers={"process.run": ConfigPresent(conf)})
        with self.assertLogs("kairo", "WARNING"):  # the refusal is logged
            self.cycles(rt, 8)
        return rt, cognition, checks, conf

    def assert_shared_history(self, rt, checks):
        self.assertEqual(checks["seen"], ("exited_nonzero", 1, False))
        [refusal] = checks["refused"]
        self.assertIn("identical to attempt", refusal["reason"])
        self.assertEqual(checks["repeated"], 2)
        (id1, rev_a, st_a), (id2, rev_b, st_b) = checks["rev1"]
        self.assertNotEqual(id1, id2)
        self.assertEqual((rev_a, st_a, rev_b, st_b), (1, "exited_nonzero", 1, "exited_nonzero"))
        [work] = rt.memory.all("work")
        failures = [a for a in rt.work.attempts(work["id"], 10) if action_state(a) == "exited_nonzero"]
        self.assertEqual([a["strategy_revision"] for a in failures], [1, 1])
        return work

    def test_failure_reassessment_recovery_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            def finish(s, w):
                latest = w["recent_attempts"][-1]
                assert (latest["state"], latest["strategy_revision"]) == ("verified_successful", 2)
                return Decision(work=[set_state(w["id"], "completed", "config restored",
                                                evidence=[latest["action_id"]])], sleep=True)

            rt, cognition, checks, conf = self.scenario(tmp, finish, branch_at=5)
            work = self.assert_shared_history(rt, checks)
            self.assertEqual(conf.read_text(), "port=8080\n")
            self.assertEqual(len(rt.memory.all("action")), 3)  # the refused repeat never ran
            self.assertEqual((work["state"], work["completion_basis"], work["strategy_revision"]),
                             ("completed", "verified", 2))
            revisions = recovery(cognition.situations[4])["revisions"]
            self.assertEqual({r["revision"]: (r["failed"], r["succeeded"]) for r in revisions},
                             {1: (2, 0), 2: (0, 1)})
            for s in cognition.situations[5:]:  # completed and never resurrected
                self.assertEqual(open_work(s), [])
                self.assertEqual(s["work"]["recently_closed"][0]["completion_basis"], "verified")

    def test_variants_wait_block_abandon_keep_the_history(self):
        for state, extra in (("waiting", {"wait_seconds": 3600}), ("blocked", {}),
                             ("abandoned", {})):
            with self.subTest(state), tempfile.TemporaryDirectory() as tmp:
                def finish(s, w, state=state, extra=extra):
                    return Decision(work=[set_state(w["id"], state, f"chose {state}", **extra)],
                                    sleep=True)

                # Branch right after the second failure instead of changing strategy.
                rt, cognition, checks, conf = self.scenario(tmp, finish, branch_at=4)
                work = self.assert_shared_history(rt, checks)
                self.assertEqual((work["state"], work["strategy_revision"]), (state, 1))
                self.assertIsNone(work["completion_basis"])
                self.assertFalse(conf.exists())
                attempts = rt.work.attempts(work["id"], 10)  # the history is preserved
                self.assertEqual([action_state(a) for a in attempts], ["exited_nonzero"] * 2)
                if state == "waiting":
                    s = cognition.situations[-1]
                    w = only_open(s)
                    self.assertEqual(w["recovery"]["latest_failure"]["action_id"], attempts[-1]["id"])
                    self.assertEqual(w["recovery"]["repeated_identical_failures"], 2)
                else:  # closed or blocked work still carries its failures in the action log
                    self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
