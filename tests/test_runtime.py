import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from kairo import (
    Action, Decision, Environment, LifecycleError, Memory, Outcome, Runtime, Sender, State, Verification,
)
from kairo.__main__ import main


class ScriptedCognition:
    """Test double: returns pre-written decisions and records what it was shown."""

    name = "scripted"

    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.seen = []

    def decide(self, context):
        self.seen.append(context)
        return self.decisions.pop(0)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.memory = Memory()
        self.addCleanup(self.memory.close)
        self.runtime = Runtime(self.memory)

    def test_initializes_in_created_state(self):
        self.assertIs(self.runtime.state, State.CREATED)
        self.assertIsNone(self.runtime.cognition)

    def test_start_and_stop(self):
        self.runtime.start()
        self.assertIs(self.runtime.state, State.AWAKE)
        self.runtime.stop()
        self.assertIs(self.runtime.state, State.STOPPED)
        self.assertEqual(self.memory.get("runtime", "lifecycle")["state"], "stopped")

    def test_restart_after_stop(self):
        self.runtime.start()
        self.runtime.stop()
        self.runtime.start()
        self.assertIs(self.runtime.state, State.AWAKE)

    def test_stop_from_sleep(self):
        self.runtime.start()
        self.runtime.sleep("idle")
        self.runtime.stop()
        self.assertIs(self.runtime.state, State.STOPPED)

    def test_sleep_wake_transitions(self):
        self.runtime.start()
        self.runtime.sleep("nothing worthwhile")
        self.assertIs(self.runtime.state, State.SLEEPING)
        self.assertEqual(self.runtime.reason, "nothing worthwhile")
        record = self.memory.get("runtime", "lifecycle")
        self.assertEqual((record["state"], record["reason"]), ("sleeping", "nothing worthwhile"))
        self.runtime.wake("reassess")
        self.assertIs(self.runtime.state, State.AWAKE)

    def test_invalid_transitions_raise(self):
        with self.assertRaises(LifecycleError):
            self.runtime.stop()
        with self.assertRaises(LifecycleError):
            self.runtime.sleep("x")
        self.runtime.start()
        with self.assertRaises(LifecycleError):
            self.runtime.start()
        with self.assertRaises(LifecycleError):
            self.runtime.wake("x")
        self.runtime.stop()
        with self.assertRaises(LifecycleError):
            self.runtime.cycle()

    def test_message_wakes_sleeping_runtime(self):
        self.runtime.start()
        self.runtime.sleep("idle")
        msg = self.runtime.receive("status?")
        self.assertIs(self.runtime.state, State.AWAKE)
        self.assertEqual(self.runtime.reason, "message received")
        self.assertEqual(self.runtime.chat.all(), [msg])


class NoCognitionTest(unittest.TestCase):
    def test_cycle_without_provider_sleeps_instead_of_crashing(self):
        memory = Memory()
        self.addCleanup(memory.close)
        runtime = Runtime(memory)
        runtime.directives.add("Keep the host healthy.")
        runtime.start()
        report = runtime.cycle()
        self.assertIs(report.state, State.SLEEPING)
        self.assertEqual(report.steps, [])
        self.assertIn("no cognition", runtime.reason)
        # Waking and cycling again is still valid.
        runtime.wake("check again")
        self.assertIs(runtime.cycle().state, State.SLEEPING)
        runtime.stop()


class CycleTest(unittest.TestCase):
    def setUp(self):
        self.memory = Memory()
        self.addCleanup(self.memory.close)

    def test_nothing_listed_does_not_imply_sleep(self):
        cognition = ScriptedCognition(Decision(reason="still exploring"))
        runtime = Runtime(self.memory, cognition=cognition)
        runtime.start()
        report = runtime.cycle()
        self.assertEqual(cognition.seen[0].open_work, [])
        self.assertIs(report.state, State.AWAKE)

    def test_cycle_passes_context_and_executes_verified_actions(self):
        class ExitZero:
            def verify(self, action, result):
                return Verification(
                    Outcome.SUCCESS if result.output["returncode"] == 0 else Outcome.FAILURE)

        action = Action("process.run", {"argv": ["true"]}, reason="probe")
        cognition = ScriptedCognition(
            Decision(actions=[action, Action("unknown.kind")], replies=["Probed."],
                     sleep=True, reason="done for now"))
        runtime = Runtime(self.memory, cognition=cognition,
                          verifiers={"process.run": ExitZero()})
        d = runtime.directives.add("Keep the host healthy.")
        runtime.receive("hello")
        runtime.start()

        report = runtime.cycle()

        ctx = cognition.seen[0]
        self.assertEqual([x.id for x in ctx.directives], [d.id])
        self.assertEqual([m.text for m in ctx.messages], ["hello"])
        self.assertIn("hostname", ctx.environment)

        self.assertEqual([s.verification.outcome for s in report.steps],
                         [Outcome.SUCCESS, Outcome.FAILURE])
        self.assertEqual(report.steps[0].result.action_id, action.id)
        self.assertEqual(runtime.chat.all()[-1].sender, Sender.KAIRO)
        self.assertIs(runtime.state, State.SLEEPING)
        self.assertEqual(runtime.reason, "done for now")

    def test_failing_provider_does_not_crash_runtime(self):
        class Broken:
            name = "broken"

            def decide(self, context):
                raise ConnectionError("provider unreachable")

        runtime = Runtime(self.memory, cognition=Broken())
        runtime.start()
        with self.assertLogs("kairo", "ERROR"):
            report = runtime.cycle()
        self.assertIs(report.state, State.SLEEPING)
        self.assertIn("provider unreachable", runtime.reason)
        runtime.stop()


class UntrustedCognitionOutputTest(unittest.TestCase):
    """Cognition output and executors are untrusted: faults must not end the runtime."""

    def setUp(self):
        self.memory = Memory()
        self.addCleanup(self.memory.close)

    def run_one(self, decision, **kwargs):
        runtime = Runtime(self.memory, cognition=ScriptedCognition(decision), **kwargs)
        runtime.start()
        with self.assertLogs("kairo", "ERROR"):
            report = runtime.cycle()
        return runtime, report

    def test_malformed_decisions_are_cognition_errors(self):
        for bad in (None, {"sleep": True}, Decision(actions=[{"kind": "process.run"}])):
            with self.subTest(bad=bad):
                runtime, report = self.run_one(bad)
                self.assertIs(report.state, State.SLEEPING)
                self.assertTrue(runtime.reason.startswith("cognition error"))
                self.assertEqual(self.memory.all("action"), [])

    def test_executor_exception_becomes_failed_step(self):
        class Exploding(Environment):
            def execute(self, action):
                raise RuntimeError("executor bug")

        bad = Action("process.run", {"argv": ["true"]})
        runtime, report = self.run_one(Decision(actions=[bad], sleep=True),
                                       environment=Exploding())
        [step] = report.steps
        self.assertFalse(step.result.executed)
        self.assertIn("executor raised", step.result.error)
        self.assertIs(step.verification.outcome, Outcome.FAILURE)
        self.assertEqual(self.memory.get("action", bad.id)["status"], "finished")
        self.assertIs(runtime.state, State.SLEEPING)

    def test_verifier_exception_is_unverifiable(self):
        class Broken:
            def verify(self, action, result):
                raise ValueError("verifier bug")

        action = Action("process.run", {"argv": ["true"]})
        _, report = self.run_one(Decision(actions=[action], sleep=True),
                                 verifiers={"process.run": Broken()})
        self.assertTrue(report.steps[0].result.executed)
        self.assertIs(report.steps[0].verification.outcome, Outcome.UNVERIFIABLE)
        self.assertIn("verifier bug", report.steps[0].verification.detail)

    def test_run_forever_survives_faulty_cycles(self):
        import threading
        bad = Action("process.run", {"argv": ["true"], "cwd": 42})
        cognition = ScriptedCognition(None, Decision(actions=[bad], sleep=True),
                                      Decision(sleep=True))
        runtime = Runtime(self.memory, cognition=cognition)
        thread = threading.Thread(target=runtime.run_forever, daemon=True)
        with self.assertLogs("kairo", "ERROR"):
            thread.start()
            for _ in range(2):
                self.assertTrue(runtime.wait_for(State.SLEEPING, 5))
                runtime.request_wake("again")
            self.assertTrue(runtime.wait_for(State.SLEEPING, 5))
        self.assertEqual(len(cognition.seen), 3)
        self.assertTrue(thread.is_alive())
        runtime.stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())


class CliTest(unittest.TestCase):
    def test_negative_reassess_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as exit_:
                main(["--run", "--reassess", "-1", "--db", str(Path(tmp) / "k.db"),
                      "--socket", str(Path(tmp) / "k.sock")])
        self.assertEqual(exit_.exception.code, 2)


class NoExternalServicesTest(unittest.TestCase):
    def test_full_lifecycle_with_network_disabled(self):
        def no_network(*args, **kwargs):
            raise AssertionError("runtime attempted a network connection")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(socket.socket, "connect", no_network), \
                mock.patch.object(socket, "create_connection", no_network):
            db = Path(tmp) / "kairo.db"
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--db", str(db)]), 0)
            status = json.loads(out.getvalue())
            self.assertEqual(status["state"], "sleeping")
            self.assertIsNone(status["cognition"])
            self.assertTrue(db.exists())

            memory = Memory(db)
            self.addCleanup(memory.close)
            self.assertEqual(memory.get("runtime", "lifecycle")["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
