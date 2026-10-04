"""Continuous operation: run_forever, sleep/wake, stop, restart and recovery.

Every wait is bounded by TIMEOUT and synchronises on runtime state or on
cognition calls, never on fixed sleeps, so the tests cannot hang.
"""

import logging
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from kairo import Action, Decision, Environment, LifecycleError, Memory, Runtime, State

TIMEOUT = 5.0
SRC = Path(__file__).resolve().parent.parent / "src"


class Cognition:
    """Test double: ``script(context, call_number)`` returns a Decision.
    Records every context and lets tests wait for a number of calls."""

    name = "test"

    def __init__(self, script):
        self.script = script
        self.contexts = []
        self._cond = threading.Condition()

    def decide(self, context):
        with self._cond:
            self.contexts.append(context)
            n = len(self.contexts)
            self._cond.notify_all()
        return self.script(context, n)

    def wait_calls(self, n):
        with self._cond:
            assert self._cond.wait_for(lambda: len(self.contexts) >= n, TIMEOUT), \
                f"expected {n} cognition calls, got {len(self.contexts)}"


def always_sleep(context, n):
    return Decision(sleep=True, reason="nothing worthwhile")


class LoopCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "kairo.db"

    def open(self, **kwargs) -> Runtime:
        memory = Memory(self.db)
        self.addCleanup(memory.close)
        return Runtime(memory, **kwargs)

    def launch(self, runtime: Runtime) -> threading.Thread:
        thread = threading.Thread(target=runtime.run_forever, daemon=True)
        thread.start()

        def shutdown():
            runtime.request_stop()
            thread.join(TIMEOUT)

        self.addCleanup(shutdown)
        return thread

    def stop(self, runtime: Runtime, thread: threading.Thread) -> None:
        runtime.stop()
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive(), "run_forever did not terminate")
        self.assertIs(runtime.state, State.STOPPED)


class RunForeverTest(LoopCase):
    def test_starts_sleeps_and_stays_alive_without_cognition(self):
        runtime = self.open()
        thread = self.launch(runtime)
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        self.assertEqual(runtime.reason, "no cognition provider configured")
        thread.join(0.1)
        self.assertTrue(thread.is_alive(), "a sleeping runtime must remain alive")
        # It observed before sleeping.
        self.assertIn("hostname", runtime.memory.get("runtime", "last_cycle")["observation"])
        # And it can be woken and go back to sleep, still without crashing.
        self.assertTrue(runtime.request_wake("check again"))
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        self.assertTrue(thread.is_alive())
        self.stop(runtime, thread)

    def test_explicit_wake_resumes_operation(self):
        cognition = Cognition(always_sleep)
        runtime = self.open(cognition=cognition)
        thread = self.launch(runtime)
        cognition.wait_calls(1)
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        runtime.request_wake("operator request")
        cognition.wait_calls(2)
        self.assertEqual(cognition.contexts[1].wake_reason, "operator request")
        self.stop(runtime, thread)

    def test_incoming_message_wakes_sleeping_runtime(self):
        cognition = Cognition(always_sleep)
        runtime = self.open(cognition=cognition)
        thread = self.launch(runtime)
        cognition.wait_calls(1)
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        runtime.receive("How is the server?")
        cognition.wait_calls(2)
        ctx = cognition.contexts[1]
        self.assertEqual(ctx.wake_reason, "message received")
        self.assertEqual([m.text for m in ctx.messages], ["How is the server?"])
        self.stop(runtime, thread)

    def test_message_during_a_cycle_is_not_slept_through(self):
        def script(context, n):
            if n == 1:
                runtime.receive("arrived while thinking")
            return Decision(sleep=True)

        cognition = Cognition(script)
        runtime = self.open(cognition=cognition)
        thread = self.launch(runtime)
        cognition.wait_calls(2)
        self.assertEqual(cognition.contexts[1].messages[-1].text, "arrived while thinking")
        self.stop(runtime, thread)

    def test_wakes_between_the_loops_check_and_its_sleep(self):
        # Regression: the loop saw 'sleeping', then two operator inputs arrived before
        # it took the lock: the first woke Kairo, the second left a pending wake. The
        # loop then tried to wake an awake runtime and its thread died. The
        # interleaving is forced: another thread wakes Kairo twice at exactly that point.
        class LateLoop(Runtime):
            injected = False

            def _sleep_until_woken(self):
                if not self.injected:
                    self.injected = True
                    other = threading.Thread(target=lambda: (
                        self.request_wake("message received"),
                        self.request_wake("directive added by the operator")))
                    other.start()
                    other.join(TIMEOUT)
                super()._sleep_until_woken()

        cognition = Cognition(always_sleep)
        memory = Memory(self.db)
        self.addCleanup(memory.close)
        runtime = LateLoop(memory, cognition=cognition)
        thread = self.launch(runtime)
        cognition.wait_calls(2)
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        self.assertTrue(thread.is_alive())
        # One cycle covers both wakes, as for any wake requested before a cycle starts,
        # and nothing is left pending to cause another.
        self.assertIsNone(runtime._wake_pending)
        self.assertEqual([c.wake_reason for c in cognition.contexts],
                         ["first start", "message received"])
        self.stop(runtime, thread)

    def test_self_wake_for_reassessment(self):
        cognition = Cognition(lambda c, n: Decision(sleep=True, wake_after=0.01))
        runtime = self.open(cognition=cognition)
        thread = self.launch(runtime)
        cognition.wait_calls(3)
        self.assertEqual(cognition.contexts[2].wake_reason, "reassessment due")
        self.stop(runtime, thread)

    def test_default_reassessment_interval(self):
        cognition = Cognition(always_sleep)
        runtime = self.open(cognition=cognition, reassess_after=0.01)
        thread = self.launch(runtime)
        cognition.wait_calls(3)
        self.assertEqual(cognition.contexts[2].wake_reason, "reassessment due")
        self.stop(runtime, thread)

    def test_multiple_cycles_and_empty_todo_is_not_idleness(self):
        # Cognition keeps working with an empty to-do list; the runtime must
        # not put itself to sleep because of that.
        cognition = Cognition(lambda c, n: Decision(sleep=n >= 5, reason=f"cycle {n}"))
        runtime = self.open(cognition=cognition)
        thread = self.launch(runtime)
        cognition.wait_calls(5)
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        self.assertEqual(len(cognition.contexts), 5)
        self.assertTrue(all(ctx.todo == [] for ctx in cognition.contexts))
        self.stop(runtime, thread)

    def test_failing_cognition_does_not_end_the_loop(self):
        def script(context, n):
            if n == 1:
                raise RuntimeError("provider down")
            return Decision(sleep=True)

        cognition = Cognition(script)
        runtime = self.open(cognition=cognition)
        with self.assertLogs("kairo", "ERROR"):
            thread = self.launch(runtime)
            cognition.wait_calls(1)
            self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        self.assertIn("provider down", runtime.reason)
        runtime.request_wake("retry")
        cognition.wait_calls(2)
        self.stop(runtime, thread)

    def test_only_one_loop_per_runtime(self):
        runtime = self.open()
        thread = self.launch(runtime)
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        with self.assertRaises(LifecycleError):
            runtime.run_forever()
        self.stop(runtime, thread)


class StopTest(LoopCase):
    def test_clean_stop_leaves_valid_database(self):
        runtime = self.open(cognition=Cognition(always_sleep))
        thread = self.launch(runtime)
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        self.stop(runtime, thread)
        runtime.memory.close()

        db = sqlite3.connect(self.db)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        reopened = Memory(self.db)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get("runtime", "lifecycle")["state"], "stopped")

    def test_stop_mid_cycle_takes_on_no_new_work(self):
        class StopsDuringFirstAction(Environment):
            executed = []

            def execute(self, action):
                self.executed.append(action.id)
                runtime.request_stop()
                return super().execute(action)

        first = Action("process.run", {"argv": ["true"]})
        second = Action("process.run", {"argv": ["true"]})
        env = StopsDuringFirstAction()
        runtime = self.open(environment=env,
                            cognition=Cognition(lambda c, n: Decision(actions=[first, second])))
        thread = self.launch(runtime)
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())
        self.assertIs(runtime.state, State.STOPPED)
        self.assertEqual(env.executed, [first.id])

    def test_stop_before_loop_starts_is_honoured(self):
        runtime = self.open()
        runtime.request_stop()
        runtime.run_forever()  # returns instead of blocking
        self.assertIs(runtime.state, State.STOPPED)


class RestartTest(LoopCase):
    def test_restart_reconstructs_the_same_kairo(self):
        first = self.open(cognition=Cognition(always_sleep))
        directive = first.directives.add("Keep the host healthy.")
        item = first.todo.add("look at /var/log")
        first.receive("remember me")
        thread = self.launch(first)
        self.assertTrue(first.wait_for(State.SLEEPING, TIMEOUT))
        self.stop(first, thread)
        first.memory.close()

        cognition = Cognition(always_sleep)
        second = self.open(cognition=cognition)
        self.assertEqual(second.identity["id"], first.identity["id"])
        self.assertEqual(second.previous["state"], "stopped")
        thread = self.launch(second)
        cognition.wait_calls(1)
        ctx = cognition.contexts[0]
        self.assertEqual(ctx.wake_reason, "started after clean stop")
        self.assertEqual([d.id for d in ctx.directives], [directive.id])
        self.assertEqual([t.id for t in ctx.todo], [item.id])
        self.assertEqual([m.text for m in ctx.messages], ["remember me"])
        self.assertEqual(second.identity["starts"], 2)
        self.stop(second, thread)

    def test_recovery_after_unclean_exit(self):
        first = self.open()
        first.start()
        first.cycle()  # now sleeping; the process then "dies" without stop()
        self.assertIs(first.state, State.SLEEPING)
        first.memory.close()

        cognition = Cognition(always_sleep)
        second = self.open(cognition=cognition)
        thread = self.launch(second)
        cognition.wait_calls(1)
        self.assertEqual(cognition.contexts[0].wake_reason,
                         "recovered: previous process ended while sleeping")
        self.stop(second, thread)

    def test_executed_actions_are_not_replayed_after_restart(self):
        marker = self.db.parent / "marker"
        append = Action("process.run", {"argv": [
            sys.executable, "-c", f"open({str(marker)!r}, 'a').write('x\\n')"]})

        first = self.open(cognition=Cognition(
            lambda c, n: Decision(actions=[append] if n == 1 else [], sleep=True)))
        thread = self.launch(first)
        self.assertTrue(first.wait_for(State.SLEEPING, TIMEOUT))
        self.stop(first, thread)
        first.memory.close()
        self.assertEqual(marker.read_text(), "x\n")

        cognition = Cognition(always_sleep)
        second = self.open(cognition=cognition)
        thread = self.launch(second)
        cognition.wait_calls(1)
        second.request_wake("again")
        cognition.wait_calls(2)
        self.stop(second, thread)

        self.assertEqual(marker.read_text(), "x\n", "action was executed again")
        # Cognition is told what was already done instead.
        [record] = cognition.contexts[0].recent_actions
        self.assertEqual((record["id"], record["status"]), (append.id, "finished"))
        self.assertEqual(record["verification"]["outcome"], "unverifiable")

    def test_interrupted_action_is_marked_not_rerun(self):
        marker = self.db.parent / "marker"
        memory = Memory(self.db)
        # What a process killed mid-action leaves behind.
        memory.put("action", "a1", {"id": "a1", "kind": "process.run",
                                    "params": {"argv": ["touch", str(marker)]},
                                    "reason": "", "status": "started"})
        memory.put("runtime", "lifecycle", {"state": "awake", "reason": "x"})
        memory.close()

        cognition = Cognition(always_sleep)
        runtime = self.open(cognition=cognition)
        with self.assertLogs("kairo", "WARNING"):
            thread = self.launch(runtime)
            cognition.wait_calls(1)
        self.stop(runtime, thread)
        self.assertFalse(marker.exists())
        self.assertEqual(runtime.memory.get("action", "a1")["status"], "interrupted")
        self.assertEqual(cognition.contexts[0].recent_actions[0]["status"], "interrupted")
        self.assertTrue(cognition.contexts[0].wake_reason.startswith("recovered"))


class ForegroundProcessTest(unittest.TestCase):
    """``python -m kairo --run`` as a real process, stopped by signal."""

    def run_and_signal(self, sig):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "kairo.db"
            env = {**os.environ, "PYTHONPATH": str(SRC)}
            proc = subprocess.Popen(
                [sys.executable, "-m", "kairo", "--run", "--db", str(db),
                 "--socket", str(Path(tmp) / "kairo.sock"), "--reassess", "0"],
                env=env, stderr=subprocess.PIPE, text=True)
            try:
                timer = threading.Timer(TIMEOUT, proc.kill)
                timer.start()
                for line in proc.stderr:  # blocks until the runtime logs its sleep
                    if "sleeping:" in line:
                        break
                timer.cancel()
                self.assertIsNone(proc.poll(), "process exited instead of staying alive")
                proc.send_signal(sig)
                self.assertEqual(proc.wait(TIMEOUT), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stderr.close()

            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                conn.close()
            memory = Memory(db)
            try:
                self.assertEqual(memory.get("runtime", "lifecycle")["state"], "stopped")
            finally:
                memory.close()

    def test_sigint_stops_cleanly(self):
        self.run_and_signal(signal.SIGINT)

    def test_sigterm_stops_cleanly(self):
        self.run_and_signal(signal.SIGTERM)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
