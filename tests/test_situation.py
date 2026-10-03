"""The situation model: what cognition is shown about Kairo each cycle.

Most tests drive a real Runtime and inspect the situation built from its
context, so they check what cognition would actually see.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from kairo import Action, Decision, Environment, Memory, Outcome, Runtime, State, Verification
from kairo.cognition import Context
from kairo.environment import ACTIONS
from kairo.redact import MARKER, secret_values
from kairo.situation import LIMITS, Limits, build_situation, render_situation
from test_continuous import SRC, TIMEOUT


class Recorder:
    """Cognition double: answers from a script and keeps the situation it was shown."""

    name = "recorder"

    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.situations = []

    def decide(self, context):
        self.situations.append(build_situation(context))
        return self.decisions.pop(0) if self.decisions else Decision(sleep=True)


class Verdict:
    def __init__(self, outcome):
        self.outcome = outcome

    def verify(self, action, result):
        return Verification(self.outcome)


def situation_of(runtime, limits=LIMITS):
    return build_situation(runtime.context(), limits)


def run_action(runtime, argv):
    return runtime.act(Action("process.run", {"argv": argv}, reason="test"))


class SituationCase(unittest.TestCase):
    def runtime(self, path=":memory:", **kwargs):
        memory = Memory(path)
        self.addCleanup(memory.close)
        return Runtime(memory, **kwargs)


class IdentityAndStateTest(SituationCase):
    def test_identity(self):
        rt = self.runtime()
        rt.start()
        kairo = situation_of(rt)["kairo"]
        self.assertEqual(kairo["identity"], rt.identity["id"])
        self.assertEqual(kairo["starts"], 1)
        self.assertIn("persistent autonomous runtime", kairo["what"])
        self.assertIn("age_seconds", kairo["born"])

    def test_runtime_state_and_cycles(self):
        rt = self.runtime(cognition=Recorder(Decision(sleep=False), Decision(sleep=True)))
        rt.start()
        now = situation_of(rt)["now"]
        self.assertEqual(now["lifecycle_state"], "awake")
        self.assertEqual(now["wake_reason"], "first start")
        self.assertLessEqual(now["in_state_since"]["age_seconds"], 5)
        self.assertEqual(now["process"]["cycles_completed"], 0)
        self.assertIsNone(now["previous_process"])
        self.assertIsNone(now["previous_cycle"])
        rt.cycle()
        rt.cycle()
        self.assertEqual(rt.cognition.situations[1]["now"]["process"]["cycles_completed"], 1)
        self.assertEqual(situation_of(rt)["now"]["lifecycle_state"], "sleeping")

    def test_wake_reason_and_what_happened_before(self):
        cognition = Recorder(Decision(sleep=True, wake_after=600, reason="All quiet; recheck soon."))
        rt = self.runtime(cognition=cognition)
        rt.start()
        rt.cycle()
        rt.request_wake("operator asked for a check")
        rt.cycle()
        s = cognition.situations[1]
        self.assertEqual(s["now"]["wake_reason"], "operator asked for a check")
        self.assertEqual(s["now"]["previous_cycle"]["ended_in_state"], "sleeping")
        [before] = s["history"]["cycles"]["items"]
        self.assertEqual(before["assessment"], "All quiet; recheck soon.")
        self.assertEqual((before["chose_sleep"], before["wake_after_seconds"]), (True, 600))


class DirectivesAndTodoTest(SituationCase):
    def test_directives(self):
        rt = self.runtime()
        keep = rt.directives.add("Keep the host healthy.")
        old = rt.directives.add("Migrate the old service.")
        rt.directives.set_active(old.id, False)
        rt.todo.add("check backups", directive_id=keep.id)
        rt.todo.add("unrelated note")
        d = situation_of(rt)["directives"]
        [active] = d["active"]
        self.assertEqual((active["id"], active["statement"]), (keep.id, "Keep the host healthy."))
        self.assertEqual(active["open_todo_items"], 1)
        self.assertIn("age_seconds", active["since"])
        self.assertEqual(d["inactive"], 1)
        self.assertIn("not tasks", d["meaning"])

    def test_todo_open_done_and_bounds(self):
        rt = self.runtime()
        items = [rt.todo.add(f"item {i}") for i in range(5)]
        rt.todo.complete(items[0].id)
        t = situation_of(rt, Limits(open_todo=2))["todo"]
        self.assertEqual([i["description"] for i in t["open"]], ["item 3", "item 4"])
        self.assertEqual(t["open_omitted"], 2)
        [done] = t["recently_done"]
        self.assertEqual(done["description"], "item 0")
        self.assertIn("age_seconds", done["done"])
        self.assertIn("Not Kairo's purpose", t["meaning"])

    def test_records_without_timestamps_are_unknown_not_invented(self):
        rt = self.runtime()
        rt.memory.put("directive", "legacy", {"statement": "old", "active": True, "id": "legacy"})
        [d] = situation_of(rt)["directives"]["active"]
        self.assertEqual(d["since"], {"at": "unknown"})


class HistoryTest(SituationCase):
    def test_action_states_and_open_threads(self):
        rt = self.runtime(verifiers={"process.run": Verdict(Outcome.SUCCESS)})
        rt.start()
        ok = run_action(rt, ["true"])
        rt.verifiers["process.run"] = Verdict(Outcome.FAILURE)
        wrong = run_action(rt, ["true"])
        del rt.verifiers["process.run"]
        unchecked = run_action(rt, ["echo", "hello"])
        broken = run_action(rt, ["/nonexistent/binary"])
        s = situation_of(rt)
        states = {a["id"]: a["state"] for a in s["history"]["actions"]["items"]}
        self.assertEqual(states, {
            ok.action.id: "verified_successful", wrong.action.id: "verified_failed",
            unchecked.action.id: "executed_unverified", broken.action.id: "failed_to_execute",
        })
        item = s["history"]["actions"]["items"][2]
        self.assertEqual((item["stdout"], item["returncode"], item["purpose"]), ("hello\n", 0, "test"))
        threads = s["open_threads"]
        self.assertEqual({a["id"]: a["failure"] for a in threads["actions_failed"]},
                         {wrong.action.id: "verification_failed", broken.action.id: "not_found"})
        self.assertEqual(threads["actions_outcome_unknown"], [])
        self.assertIn("not a task list", threads["meaning"])

    def test_interrupted_action_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            memory = Memory(db)
            memory.put("action", "a1", {"id": "a1", "kind": "process.run", "status": "started",
                                        "params": {"argv": ["sleep", "100"]}, "reason": "long job",
                                        "started_at": 1.0})
            memory.put("runtime", "lifecycle", {"state": "awake", "reason": "x", "at": 2.0})
            memory.close()
            rt = self.runtime(db)
            with self.assertLogs("kairo", "WARNING"):
                rt.start()
            s = situation_of(rt)
        self.assertTrue(s["now"]["wake_reason"].startswith("recovered"))
        self.assertIs(s["now"]["previous_process"]["ended_cleanly"], False)
        [a] = s["history"]["actions"]["items"]
        self.assertEqual((a["state"], a["finished"]), ("interrupted", None))
        # Interrupted is an unknown outcome, not a failure.
        self.assertEqual(s["open_threads"]["actions_outcome_unknown"],
                         [{"id": "a1", "state": "interrupted", "work_id": None}])
        self.assertEqual(s["open_threads"]["actions_failed"], [])

    def test_failed_cycle_is_recorded_as_fact(self):
        class Failing:
            name = "failing"

            def decide(self, context):
                raise RuntimeError("provider down")

        rt = self.runtime(cognition=Failing())
        rt.start()
        with self.assertLogs("kairo", "ERROR"):
            rt.cycle()
        rt.request_wake("retry")
        s = situation_of(rt)
        [c] = s["history"]["cycles"]["items"]
        self.assertEqual((c["cognition"], c["failure"]), ("failed", "provider_error"))
        self.assertIn("provider down", c["failure_detail"])
        self.assertNotIn("assessment", c)
        self.assertEqual(s["open_threads"]["previous_cycle_failed"]["failure"], "provider_error")

    def test_unanswered_messages(self):
        rt = self.runtime()
        rt.chat.post("human", "first")
        rt.chat.post("kairo", "answered")
        late = rt.chat.post("human", "are you there?")
        s = situation_of(rt)
        self.assertEqual([u["id"] for u in s["open_threads"]["unanswered_human_messages"]], [late.id])
        self.assertEqual([m["from"] for m in s["history"]["chat"]["items"]],
                         ["human", "kairo", "human"])

    def test_continuity_across_cycles_in_run_forever(self):
        marker = Decision(actions=[Action("process.run", {"argv": ["echo", "disk ok"]},
                                          reason="check disk")],
                          sleep=False, reason="Checking disk before deciding.")
        cognition = Recorder(marker, Decision(sleep=True, reason="Disk fine."))
        rt = self.runtime(cognition=cognition)
        thread = threading.Thread(target=rt.run_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (rt.request_stop(), thread.join(TIMEOUT)))
        self.assertTrue(rt.wait_for(State.SLEEPING, TIMEOUT))
        second = cognition.situations[1]
        [cycle] = second["history"]["cycles"]["items"]
        self.assertEqual(cycle["assessment"], "Checking disk before deciding.")
        [action] = second["history"]["actions"]["items"]
        self.assertEqual(cycle["requested_actions"], [action["id"]])
        self.assertEqual((action["purpose"], action["stdout"]), ("check disk", "disk ok\n"))
        self.assertEqual(second["now"]["wake_reason"], "first start")  # still the same awake period


class ReviewRegressionTest(SituationCase):
    """Defects found in the Phase 4 review."""

    def test_open_threads_do_not_list_every_unverified_action_forever(self):
        cognition = Recorder(
            Decision(actions=[Action("process.run", {"argv": ["echo", "one"]})], sleep=False),
            Decision(actions=[Action("process.run", {"argv": ["echo", "two"]})], sleep=False),
            Decision(sleep=True))
        rt = self.runtime(cognition=cognition)
        rt.start()
        rt.cycle()
        rt.cycle()
        rt.cycle()
        second, third = cognition.situations[1], cognition.situations[2]
        first_id = second["history"]["actions"]["items"][0]["id"]
        self.assertEqual(second["open_threads"]["new_action_results"], [first_id])
        [latest] = third["open_threads"]["new_action_results"]
        self.assertNotEqual(latest, first_id)  # already-seen results are not "open" again
        self.assertEqual(third["history"]["actions"]["items"][0]["state"], "executed_unverified")

    def test_previous_process_reason_is_not_presented_as_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            first = self.runtime(db, cognition=Recorder(
                Decision(sleep=True, reason="My theory: the disk is failing.")))
            first.start()
            first.cycle()  # sleeps with cognition's reason; then the process dies
            first.memory.close()
            s = situation_of(self.runtime(db))
        self.assertNotIn("disk is failing", json.dumps(s["now"]))
        self.assertIs(s["now"]["previous_process"]["ended_cleanly"], False)
        [cycle] = s["history"]["cycles"]["items"]
        self.assertEqual(cycle["assessment"], "My theory: the disk is failing.")  # labelled

    def test_state_times_are_honest(self):
        rt = self.runtime()
        created = situation_of(rt)["now"]
        self.assertIsNone(created["wake_reason"])
        self.assertIsNone(created["process"])  # not started: not "unknown"
        rt.start()
        rt.cycle()  # no cognition: sleeps
        now = situation_of(rt)["now"]
        self.assertEqual(now["lifecycle_state"], "sleeping")
        self.assertIn("age_seconds", now["in_state_since"])
        self.assertIn("age_seconds", now["previous_cycle"]["ended"])

    def test_future_timestamps_are_not_fresh(self):
        ctx = Context(environment={}, directives=[], todo=[], messages=[],
                      recent_cycles=[{"at": 5000.0, "state": "sleeping", "cognition": {}}],
                      runtime={"now": 1000.0})
        ended = build_situation(ctx)["now"]["previous_cycle"]["ended"]
        self.assertIsNone(ended["age_seconds"])
        self.assertIn("clock", ended["note"])

    def test_budget_drops_the_oldest_history_across_kinds(self):
        from kairo.chat import Message
        old_chat = [Message("human", f"msg{i} " + "c" * 1800, at=1000 + i) for i in range(20)]
        new_actions = [{"id": f"a{i}", "kind": "process.run", "status": "finished",
                        "started_at": 2000 + i, "finished_at": 2000 + i,
                        "result": {"executed": True, "output": {"stdout": "o" * 1400}}}
                       for i in range(15)]
        s = build_situation(Context(environment={}, directives=[], todo=[], messages=old_chat,
                                    recent_actions=new_actions, runtime={"now": 3000.0}),
                            Limits(budget=40_000))
        self.assertGreater(s["context"]["trimmed_for_budget"], 0)
        kept = [a["id"] for a in s["history"]["actions"]["items"]]
        self.assertEqual(kept, [f"a{i}" for i in range(15)])  # newer than every message
        self.assertLess(len(s["history"]["chat"]["items"]), 20)
        self.assertEqual(s["history"]["chat"]["items"][-1]["text"][:5], "msg19")

    def test_partial_identity_record_does_not_stop_kairo(self):
        rt = self.runtime(cognition=Recorder())
        rt.memory.put("runtime", "identity", {"id": "kairo-1"})  # no born_at, no starts
        rt = Runtime(rt.memory, cognition=Recorder())
        rt.start()
        report = rt.cycle()
        self.assertIs(report.state, State.SLEEPING)
        kairo = rt.cognition.situations[0]["kairo"]
        self.assertEqual((kairo["identity"], kairo["starts"], kairo["born"]),
                         ("kairo-1", 1, {"at": "unknown"}))
        self.assertEqual(rt.status()["identity"], "kairo-1")


class StableHost(Environment):
    """A host whose facts are identical for both processes of a restart test,
    whatever directory the suite runs from. The real observation includes the
    working directory, which can contain values Kairo redacts (an environment
    variable named like a secret, e.g. a session id in a temporary path).

    Known runtime issue, deliberately not hidden or changed here: the previous
    observation is stored redacted, the fresh one is compared unredacted, so a
    host fact containing a redacted value is reported as changed on every
    comparison (situation._environment)."""

    def observe(self):
        return {"hostname": "test-host", "user": "test-user", "cwd": "/srv/kairo-test",
                "python": "3.12"}


class RestartTest(SituationCase):
    def restart_situation(self):
        """Two processes on one database: the first cycles and stops cleanly; the
        situation the second one starts with."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            first = self.runtime(db, environment=StableHost())
            first.start()
            first.cycle()  # no cognition: observes, records, sleeps
            first.stop()
            first.memory.close()
            second = self.runtime(db, environment=StableHost())
            second.start()
            return situation_of(second)

    def test_restart_after_clean_stop(self):
        s = self.restart_situation()
        self.assertEqual(s["kairo"]["starts"], 2)
        self.assertEqual(s["now"]["wake_reason"], "started after clean stop")
        self.assertIs(s["now"]["previous_process"]["ended_cleanly"], True)
        self.assertEqual(s["now"]["process"]["cycles_completed"], 0)
        comparison = s["environment"]["since_previous_observation"]
        self.assertEqual(comparison["changed"], {})
        self.assertIn("age_seconds", comparison["previous_observation"])

    def test_restart_does_not_depend_on_where_the_suite_runs(self):
        """Regression: run from a directory whose path contains the value of a
        secret-named environment variable (as under a tool's session temp dir),
        the restart is still seen as unchanged."""
        value = "synthetic-session-0123456789"
        with tempfile.TemporaryDirectory(prefix=f"{value}-") as cwd, \
                mock.patch.dict(os.environ, {"KAIRO_TEST_SESSION_ID": value}):
            here = os.getcwd()
            os.chdir(cwd)
            try:
                self.assertIn(value, secret_values())  # the condition is really present:
                self.assertIn(value, Environment().observe()["cwd"])  # a redacted value in cwd
                s = self.restart_situation()
            finally:
                os.chdir(here)  # before the directory is removed
        self.assertEqual(s["kairo"]["starts"], 2)
        self.assertEqual(s["environment"]["since_previous_observation"]["changed"], {})


class EnvironmentAndCapabilitiesTest(SituationCase):
    def test_environment_is_fresh_and_changes_are_reported(self):
        class Host(Environment):
            load = "low"

            def observe(self):
                return {"hostname": "h", "load": self.load}

        env = Host()
        rt = self.runtime(environment=env)
        rt.start()
        rt.cycle()
        rt.wake("check")
        env.load = "high"
        e = situation_of(rt)["environment"]
        self.assertEqual(e["age_seconds"], 0)
        self.assertEqual(e["facts"], {"hostname": "h", "load": "high"})
        self.assertEqual(e["since_previous_observation"]["changed"],
                         {"load": {"before": "low", "now": "high"}})

    def test_unobservable_environment_is_explicit(self):
        class Blind(Environment):
            def observe(self):
                raise OSError("no /proc")

        rt = self.runtime(environment=Blind())
        rt.start()
        with self.assertLogs("kairo", "WARNING"):
            e = situation_of(rt)["environment"]
        self.assertIsNone(e["facts"])
        self.assertIn("observation failed", e["unavailable"])

    def test_capabilities_match_runtime(self):
        rt = self.runtime(verifiers={"process.run": Verdict(Outcome.SUCCESS)})
        caps = situation_of(rt)["capabilities"]
        self.assertEqual(set(caps["actions"]), set(rt.environment.actions()))
        spec = caps["actions"]["process.run"]
        self.assertEqual(spec["params"], ACTIONS["process.run"]["params"])
        self.assertIs(spec["verified_automatically"], True)
        self.assertIs(situation_of(self.runtime())["capabilities"]["actions"]["process.run"]
                      ["verified_automatically"], False)


class KnowledgeTest(unittest.TestCase):
    def ctx(self, **kw):
        return Context(environment={}, directives=[], todo=[], messages=[],
                       runtime={"now": 1000.0}, **kw)

    def test_empty_knowledge_is_explicit(self):
        k = build_situation(self.ctx())["knowledge"]
        self.assertEqual((k["source"], k["items"]), ("none", []))

    def test_knowledge_items_are_bounded_and_redacted(self):
        secret = "hunter2-hunter2-hunter2"
        with mock.patch.dict(os.environ, {"DB_PASSWORD": secret}):
            k = build_situation(self.ctx(knowledge=[
                {"fact": f"db password is {secret}", "blob": "y" * 9000}]))["knowledge"]
        self.assertNotIn(secret, json.dumps(k))
        self.assertIn(MARKER, k["items"][0]["fact"])
        self.assertLess(len(k["items"][0]["blob"]), LIMITS.text + 100)


class BoundsTest(SituationCase):
    def test_old_history_is_excluded_and_counted(self):
        rt = self.runtime()
        for i in range(LIMITS.messages + 7):
            rt.chat.post("human", f"message {i}")
        chat = situation_of(rt)["history"]["chat"]
        texts = [m["text"] for m in chat["items"]]
        self.assertEqual(len(texts), LIMITS.messages)
        self.assertEqual(texts[-1], f"message {LIMITS.messages + 6}")
        self.assertNotIn("message 0", texts)
        self.assertEqual(chat["omitted_older"], 7)

    def test_action_output_is_capped(self):
        rt = self.runtime()
        rt.start()
        run_action(rt, [sys.executable, "-c", "print('z' * 20000)"])
        [a] = situation_of(rt)["history"]["actions"]["items"]
        self.assertLess(len(a["stdout"]), LIMITS.action_output + 60)
        self.assertIn("[truncated", a["stdout"])
        self.assertLess(len(rt.memory.all("action")[0]["result"]["output"]["stdout"]), 16_100)

    def test_total_budget_trims_oldest_history_first(self):
        rt = self.runtime()
        rt.start()
        for i in range(8):
            run_action(rt, [sys.executable, "-c", f"print('{i}' * 5000)"])
        limits = Limits(budget=12_000)
        s = build_situation(rt.context(), limits)
        self.assertGreater(s["context"]["trimmed_for_budget"], 0)
        self.assertLess(len(render_situation(s)), limits.budget + 2000)  # + the context section
        kept = s["history"]["actions"]["items"]
        self.assertTrue(kept and kept[-1]["stdout"].startswith("7"))  # newest survives
        self.assertEqual(s["history"]["actions"]["omitted_older"], 8 - len(kept))


class SecurityTest(SituationCase):
    def test_secrets_are_redacted_everywhere(self):
        secret = "sk-live-0123456789abcdef"
        with mock.patch.dict(os.environ, {"PROVIDER_API_KEY": secret}):
            rt = self.runtime()
            rt.start()
            rt.directives.add(f"Rotate key {secret} monthly")
            rt.chat.post("human", f"the key is {secret}")
            run_action(rt, ["sh", "-c", "echo PROVIDER_API_KEY=$PROVIDER_API_KEY"])
            text = render_situation(situation_of(rt))
        self.assertNotIn(secret, text)
        self.assertIn("PROVIDER_API_KEY=" + MARKER, text)
        self.assertGreaterEqual(json.loads(text)["context"]["redaction_markers"], 3)

    def test_environment_variables_are_not_exposed(self):
        with mock.patch.dict(os.environ, {"HARMLESS_LOOKING_VAR": "value-not-for-context"}):
            rt = self.runtime()
            text = render_situation(situation_of(rt))
        self.assertNotIn("value-not-for-context", text)

    def test_unknown_objects_are_never_stringified(self):
        class Credential:
            def __repr__(self):
                return "Credential(token=abc123abc123)"

            __str__ = __repr__

        ctx = Context(environment={"weird": Credential()}, directives=[], todo=[], messages=[],
                      runtime={"now": 1.0})
        text = render_situation(build_situation(ctx))
        self.assertNotIn("abc123abc123", text)
        self.assertIn("<Credential>", text)


class RobustnessTest(SituationCase):
    def test_empty_state(self):
        s = build_situation(Context(environment={}, directives=[], todo=[], messages=[]))
        self.assertEqual(s["directives"]["active"], [])
        self.assertEqual(s["todo"]["open"], [])
        for part in ("cycles", "actions", "chat"):
            self.assertEqual(s["history"][part]["items"], [])
        self.assertIsNone(s["now"]["previous_cycle"])
        self.assertIsNone(s["environment"]["since_previous_observation"])
        self.assertEqual(s["context"]["unavailable_sections"], [])

    def test_malformed_records_do_not_crash_the_runtime(self):
        cognition = Recorder()
        rt = self.runtime(cognition=cognition)
        rt.memory.put("action", "bad", {"id": "bad", "result": "garbage", "verification": 5})
        rt.memory.put("cycle", "bad", {"cognition": ["not", "a", "dict"], "at": "yesterday"})
        rt.memory.put("message", "bad", {"no": "fields"})
        rt.memory.put("todo", "bad", {"description": 5})  # known field, wrong type
        rt.start()
        with self.assertLogs("kairo", "WARNING"):
            report = rt.cycle()
        self.assertIs(report.state, State.SLEEPING)  # the cycle completed normally
        s = cognition.situations[0]
        self.assertEqual(set(s["context"]["unreadable_records"]), {"chat", "todo", "done_todo"})
        self.assertIn("history.actions", s["context"]["unavailable_sections"])
        self.assertIn("history.cycles", s["context"]["unavailable_sections"])
        self.assertIn("unavailable", s["history"]["actions"])
        self.assertEqual(s["kairo"]["identity"], rt.identity["id"])  # the rest still renders

    def test_rendering_is_deterministic(self):
        rt = self.runtime()
        rt.start()
        rt.directives.add("Keep the host healthy.")
        rt.chat.post("human", "hello")
        run_action(rt, ["echo", "x"])
        ctx = rt.context()
        self.assertEqual(render_situation(build_situation(ctx)),
                         render_situation(build_situation(ctx)))

    def test_situation_does_not_need_a_provider(self):
        code = ("import sys, kairo.runtime, kairo.situation\n"
                "assert 'kairo.claude' not in sys.modules, 'situation pulled in a provider'\n"
                "rt = kairo.runtime.Runtime(kairo.Memory())\n"
                "print(len(kairo.situation.render_situation("
                "kairo.situation.build_situation(rt.context()))))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": str(SRC)}, timeout=TIMEOUT)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertGreater(int(out.stdout), 100)

    def test_situation_cli_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            out = subprocess.run([sys.executable, "-m", "kairo", "--situation", "--db", str(db)],
                                 capture_output=True, text=True, timeout=TIMEOUT,
                                 env={**os.environ, "PYTHONPATH": str(SRC)})
            self.assertEqual(out.returncode, 0, out.stderr)
            s = json.loads(out.stdout)
            self.assertEqual(s["now"]["lifecycle_state"], "created")  # nothing was started
            memory = Memory(db)
            self.addCleanup(memory.close)
            self.assertEqual(memory.count("cycle"), 0)  # and nothing was recorded


if __name__ == "__main__":
    unittest.main()
