"""Pacing: a failing or spinning cognition must not run hot.

Failed cycles back off (never an immediate retry, never "sleep until woken");
cycles that change nothing and do not rest end in a rest the runtime takes itself.
"""

import threading
import time
import unittest

from kairo import Action, Decision, Memory, Runtime, State
from kairo.runtime import (FAILURE_BACKOFF, FAILURE_BACKOFF_MAX, STALL_LIMIT, STALL_REST,
                           STALL_WAKE)
from kairo.situation import build_situation


class Scripted:
    name = "test"

    def __init__(self, script):
        self.script, self.calls = script, 0

    def decide(self, context):
        self.calls += 1
        return self.script(self.calls)


def failing(n):
    raise RuntimeError("provider down")


def runtime(script, **kwargs) -> Runtime:
    r = Runtime(Memory(), cognition=Scripted(script), **kwargs)
    r.start()
    return r


def delay(r: Runtime) -> float:
    return r.status()["wake_at"] - time.time()


class FailureBackoffTest(unittest.TestCase):
    def test_retry_delay_doubles_and_is_capped(self):
        r = runtime(failing, reassess_after=300)
        seen = []
        with self.assertLogs("kairo", "ERROR"):
            for _ in range(8):
                report = r.cycle()
                self.assertIs(r.state, State.SLEEPING)
                seen.append(report.cognition["retry_after"])
                r.wake("test")
        self.assertEqual(seen[:4], [FAILURE_BACKOFF * m for m in (1, 2, 4, 8)])
        self.assertEqual(seen[-1], FAILURE_BACKOFF_MAX)
        self.assertEqual(report.cognition["consecutive_failures"], 8)

    def test_a_failure_never_sleeps_until_woken(self):
        # Without a default reassessment a failed cycle used to sleep forever.
        r = runtime(failing, reassess_after=None)
        with self.assertLogs("kairo", "ERROR"):
            r.cycle()
        self.assertIs(r.state, State.SLEEPING)
        self.assertAlmostEqual(delay(r), FAILURE_BACKOFF, delta=5)
        self.assertIn("retry in", r.reason)

    def test_a_usable_decision_resets_the_backoff(self):
        def script(n):
            if n in (1, 2, 4):
                raise RuntimeError("provider down")
            return Decision(sleep=True)

        r = runtime(script, reassess_after=300)
        with self.assertLogs("kairo", "ERROR"):
            for expected in (FAILURE_BACKOFF, FAILURE_BACKOFF * 2, None, FAILURE_BACKOFF):
                report = r.cycle()
                self.assertEqual(report.cognition.get("retry_after"), expected)
                r.wake("test")

    def test_a_wake_interrupts_the_backoff(self):
        r = runtime(failing, reassess_after=300)
        with self.assertLogs("kairo", "ERROR"):
            r.cycle()
        self.assertTrue(r.request_wake("operator"))
        self.assertIs(r.state, State.AWAKE)

    def test_cognition_is_shown_the_backoff(self):
        r = runtime(failing, reassess_after=300)
        with self.assertLogs("kairo", "ERROR"):
            r.cycle()
        cycle = build_situation(r.context())["history"]["cycles"]["items"][-1]
        self.assertEqual(cycle["consecutive_failures"], 1)
        self.assertEqual(cycle["retry_after_seconds"], FAILURE_BACKOFF)


class StallTest(unittest.TestCase):
    def test_awake_cycles_that_change_nothing_end_in_a_rest(self):
        r = runtime(lambda n: Decision(sleep=False, reason="still thinking"), reassess_after=60)
        with self.assertLogs("kairo", "WARNING"):
            for n in range(1, STALL_LIMIT + 1):
                self.assertIs(r.state, State.AWAKE)
                report = r.cycle()
        self.assertIs(r.state, State.SLEEPING)
        self.assertEqual(report.cognition["forced_rest"]["stalled_cycles"], STALL_LIMIT)
        self.assertAlmostEqual(delay(r), STALL_REST, delta=5)
        self.assertIn("rested by the runtime", r.reason)
        cycle = build_situation(r.context())["history"]["cycles"]["items"][-1]
        self.assertEqual(cycle["rested_by_runtime"]["stalled_cycles"], STALL_LIMIT)

    def test_the_rest_is_at_least_the_default_reassessment(self):
        r = runtime(lambda n: Decision(sleep=False), reassess_after=STALL_REST * 4)
        with self.assertLogs("kairo", "WARNING"):
            for _ in range(STALL_LIMIT):
                r.cycle()
        self.assertAlmostEqual(delay(r), STALL_REST * 4, delta=5)

    def test_sleeping_for_an_instant_is_not_rest(self):
        r = runtime(lambda n: Decision(sleep=True, wake_after=STALL_WAKE / 10))
        with self.assertLogs("kairo", "WARNING"):
            for _ in range(STALL_LIMIT):
                report = r.cycle()
                if r.state is State.SLEEPING and "forced_rest" not in report.cognition:
                    r.wake("reassessment due")
        self.assertIn("forced_rest", report.cognition)
        self.assertAlmostEqual(delay(r), STALL_REST, delta=5)

    def test_progress_resets_the_count(self):
        # An action, an applied work request or a reply is progress.
        def script(n):
            if n % (STALL_LIMIT - 1) == 0:
                kind = (n // (STALL_LIMIT - 1)) % 3
                if kind == 0:
                    return Decision(sleep=False, actions=[Action("process.run", {"argv": ["true"]})])
                if kind == 1:
                    return Decision(sleep=False, replies=["still here"])
                return Decision(sleep=False, work=[{
                    "op": "create", "ref": f"w{n}", "objective": f"objective {n}", "why": "test",
                    "directive_id": None, "strategy": "", "next_step": ""}])
            return Decision(sleep=False)

        r = runtime(script)
        for _ in range(STALL_LIMIT * 4):
            report = r.cycle()
            self.assertNotIn("forced_rest", report.cognition)
            self.assertIs(r.state, State.AWAKE)

    def test_ordinary_sleep_is_never_a_stall(self):
        r = runtime(lambda n: Decision(sleep=True, wake_after=None), reassess_after=None)
        for _ in range(STALL_LIMIT * 2):
            report = r.cycle()
            self.assertNotIn("forced_rest", report.cognition)
            self.assertIsNone(r.status()["wake_at"])  # sleep until woken is still allowed
            r.wake("test")

    def test_rejected_requests_alone_are_not_progress(self):
        bad = {"op": "update", "work_id": "missing", "understanding": "x", "strategy": None,
               "next_step": None}
        r = runtime(lambda n: Decision(sleep=False, work=[bad]))
        with self.assertLogs("kairo", "WARNING"):
            for _ in range(STALL_LIMIT):
                report = r.cycle()
        self.assertIn("forced_rest", report.cognition)

    def test_a_spinning_loop_makes_a_bounded_number_of_provider_calls(self):
        cognition = Scripted(lambda n: Decision(sleep=False))
        r = Runtime(Memory(), cognition=cognition, reassess_after=300)
        thread = threading.Thread(target=r.run_forever, daemon=True)
        with self.assertLogs("kairo", "WARNING"):
            thread.start()
            self.assertTrue(r.wait_for(State.SLEEPING, 5))
            time.sleep(0.2)
        self.assertEqual(cognition.calls, STALL_LIMIT)
        r.request_stop()
        thread.join(5)


if __name__ == "__main__":
    unittest.main()
