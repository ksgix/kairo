"""The configuration file, the daily call budget, and waiting for a usage limit.

Three parts of one concern: what a cycle of cognition costs the operator, and
who decides how much of it Kairo may spend.

The file is a convenience over the flags and never a second source of truth: a
flag given on the command line wins, and anything the file says that Kairo would
have to guess at is an error instead.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from kairo.__main__ import main
from kairo.claude import _is_usage_limit, _resets_at
from kairo.cognition import Attempt, Cognition, CognitionError, CognitionResult, Context
from kairo.config import Config, ConfigError, default_path, load
from kairo.memory import Memory
from kairo.runtime import FAILURE_BACKOFF, Runtime, State

TIMEOUT = 10.0


class TempCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="kairo-config-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.db = self.dir / "kairo.db"

    def write(self, text, name="kairo.toml"):
        path = self.dir / name
        path.write_text(text)
        return path


# -- the file ----------------------------------------------------------------------


class FileTest(TempCase):
    def test_a_missing_file_is_simply_no_configuration(self):
        self.assertEqual(load(self.dir / "kairo.toml", required=False), Config())

    def test_a_file_asked_for_by_name_must_exist(self):
        with self.assertRaises(ConfigError) as caught:
            load(self.dir / "gone.toml", required=True)
        self.assertIn("no such configuration file", str(caught.exception))

    def test_every_setting_is_read(self):
        path = self.write("""
            [cognition]
            providers = "claude,claude@spare"
            model = "opus"
            timeout = 120
            calls_per_day = 40

            [cognition.options.claude]
            executable = "/usr/local/bin/claude"

            [runtime]
            reassess = 600
        """)
        config = load(path, required=True)
        self.assertEqual(
            (config.providers, config.model, config.timeout, config.calls_per_day,
             config.reassess),
            ("claude,claude@spare", "opus", 120.0, 40, 600.0))
        self.assertEqual(config.provider_opts(),
                         ["claude.executable=/usr/local/bin/claude"])

    def test_a_typo_stops_kairo_rather_than_leaving_a_default_in_place(self):
        # The whole point of the file is that the operator's intent is obeyed;
        # a setting silently ignored would be worse than no file at all.
        for text, expected in [
            ('[cognition]\nmodle = "opus"\n', "unknown setting"),
            ('[cognitoin]\nmodel = "opus"\n', "unknown section"),
            ('[cognition]\nmodel = 5\n', "cognition.model must be a string"),
            ('[cognition]\ntimeout = "soon"\n', "cognition.timeout must be a number"),
            ('[cognition]\ntimeout = 0\n', "timeout must be > 0"),
            ('[cognition]\ncalls_per_day = -1\n', "must be >= 0"),
            ('[runtime]\nreassess = -5\n', "must be >= 0"),
            ('[cognition]\nproviders = true\n', "providers must be a string"),
            ("not toml at all\n", "not valid TOML"),
            ('[cognition]\noptions = 5\n', "must be a table"),
        ]:
            with self.subTest(text=text), self.assertRaises(ConfigError) as caught:
                load(self.write(text, "one.toml"), required=True)
            self.assertIn(expected, str(caught.exception))

    def test_the_default_place_is_beside_the_database(self):
        self.assertEqual(default_path(self.db), self.dir / "kairo.toml")


class PrecedenceTest(TempCase):
    """What the file configures, and what beats it."""

    def situation(self, *argv):
        import contextlib
        import io
        import json
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--db", str(self.db), "--situation", *argv]), 0)
        return json.loads(out.getvalue())

    def test_the_file_is_used_when_the_flag_is_absent(self):
        self.write("[runtime]\nreassess = 600\n\n[cognition]\ncalls_per_day = 40\n")
        now = self.situation()["now"]
        self.assertEqual(now["default_reassess_after_seconds"], 600.0)
        self.assertEqual(now["provider_call_budget"]["calls_per_day"], 40)

    def test_a_flag_wins_over_the_file(self):
        self.write("[runtime]\nreassess = 600\n\n[cognition]\ncalls_per_day = 40\n")
        now = self.situation("--reassess", "5", "--calls-per-day", "7")["now"]
        self.assertEqual(now["default_reassess_after_seconds"], 5.0)
        self.assertEqual(now["provider_call_budget"]["calls_per_day"], 7)

    def test_without_a_file_or_flags_the_defaults_hold(self):
        now = self.situation()["now"]
        self.assertEqual(now["default_reassess_after_seconds"], 300.0)
        self.assertNotIn("provider_call_budget", now)  # no budget: nothing to say

    def test_a_named_file_elsewhere_is_read(self):
        other = self.write("[runtime]\nreassess = 42\n", "elsewhere.toml")
        now = self.situation("--config", str(other))["now"]
        self.assertEqual(now["default_reassess_after_seconds"], 42.0)


# -- the budget --------------------------------------------------------------------


class SleepingCognition(Cognition):
    """Decides to sleep, and counts how often it was asked."""

    def __init__(self):
        self.asked = 0

    def decide(self, context: Context) -> CognitionResult:
        self.asked += 1
        from kairo.cognition import Decision
        return CognitionResult(Decision(sleep=True, reason="nothing to do", wake_after=60.0),
                               "test", {"position": 0, "reason": "first_in_order"},
                               [Attempt("test", "decided", False, 0.0)])


class BudgetTest(TempCase):
    def turn(self, runtime):
        """One cycle, waking first: a cycle that chose to sleep leaves it asleep."""
        if runtime.state is State.SLEEPING:
            runtime.wake("test")
        return runtime.cycle()

    def runtime(self, calls_per_day):
        memory = Memory(self.db)
        self.addCleanup(memory.close)
        cognition = SleepingCognition()
        return Runtime(memory, cognition=cognition, calls_per_day=calls_per_day), cognition

    def test_without_a_budget_nothing_is_counted_or_shown(self):
        runtime, cognition = self.runtime(None)
        runtime.start()
        for _ in range(3):
            self.turn(runtime)
        self.assertIsNone(runtime.budget())
        self.assertEqual(cognition.asked, 3)

    def test_the_provider_is_not_asked_once_the_budget_is_spent(self):
        runtime, cognition = self.runtime(2)
        runtime.start()
        self.turn(runtime)
        self.turn(runtime)
        self.assertEqual(cognition.asked, 2)
        report = self.turn(runtime)
        self.assertEqual(cognition.asked, 2, "asked after the budget was spent")
        self.assertEqual(report.cognition["result"], "over_budget")
        self.assertIn("budget is spent", report.note)
        self.assertIs(runtime.state, State.SLEEPING)

    def test_it_sleeps_until_the_day_turns_not_for_a_moment(self):
        runtime, _ = self.runtime(1)
        runtime.start()
        self.turn(runtime)
        self.turn(runtime)  # over budget
        budget = runtime.budget()
        self.assertEqual(budget["remaining"], 0)
        # The wake is the next UTC midnight, which is never more than a day off.
        self.assertGreater(budget["renews_at"], time.time())
        self.assertLessEqual(budget["renews_at"] - time.time(), 86400)

    def test_a_restart_does_not_hand_kairo_a_fresh_allowance(self):
        # Counted from the records, so stopping and starting is not a way round it.
        runtime, _ = self.runtime(5)
        runtime.start()
        self.turn(runtime)
        self.turn(runtime)
        runtime.stop()
        again, cognition = self.runtime(5)
        again.start()
        self.assertEqual(again.budget()["calls_today"], 2)
        self.assertEqual(again.budget()["remaining"], 3)

    def test_a_spent_budget_never_stops_a_release_being_checked(self):
        """A deployment's dry cycle calls no provider, so it spends nothing. If
        the budget applied to it, Kairo could not deploy a fix on the day it ran
        out of calls -- exactly when it most needs to change something."""
        import argparse
        from kairo.__main__ import _preflight_args
        args = argparse.Namespace(cognition="claude", implementations="none", probe=[],
                                  cognition_timeout=300.0, provider_opt=[], model=None,
                                  calls_per_day=40)
        self.assertNotIn("--calls-per-day", _preflight_args(args, self.dir))

    def test_kairo_is_told_what_is_left(self):
        from kairo.situation import build_situation
        runtime, _ = self.runtime(10)
        runtime.start()
        self.turn(runtime)
        shown = build_situation(runtime.context())["now"]["provider_call_budget"]
        self.assertEqual((shown["calls_per_day"], shown["used_today"], shown["remaining_today"]),
                         (10, 1, 9))
        self.assertIn("renews_at", shown)


# -- waiting for a usage limit ------------------------------------------------------


class UsageLimitTest(unittest.TestCase):
    def test_a_limit_message_is_recognised(self):
        self.assertTrue(_is_usage_limit("Claude AI usage limit reached|1760003600"))
        self.assertTrue(_is_usage_limit("5-hour limit reached"))
        self.assertFalse(_is_usage_limit("overloaded_error"))

    def test_the_reset_time_is_read_when_the_provider_gives_one(self):
        now = 1_760_000_000.0
        for text, expected in [
            ("Claude AI usage limit reached|1760003600", 1760003600.0),
            ("limit reached: 1760003600000", 1760003600.0),   # milliseconds
            ("usage limit reached, retry after 1800 seconds", now + 1800),
        ]:
            with self.subTest(text=text):
                self.assertEqual(_resets_at(text, now), expected)

    def test_nothing_is_guessed_from_a_time_that_makes_no_sense(self):
        now = 1_760_000_000.0
        for text in ["Claude AI usage limit reached",          # no time at all
                     "usage limit reached|1000000000",         # already past
                     "usage limit reached|1799999999",         # months away
                     "usage limit reached, retry after 0 seconds"]:
            with self.subTest(text=text):
                self.assertIsNone(_resets_at(text, now))


class LimitedCognition(Cognition):
    """Fails every cycle with a usage limit that resets at a fixed time."""

    def __init__(self, retry_at):
        self.retry_at = retry_at

    def decide(self, context: Context) -> CognitionResult:
        return CognitionResult(None, None, None,
                               [Attempt("claude", "rate_limited", True, 0.0,
                                        "usage limit", retry_at=self.retry_at)],
                               failure="rate_limited")


class WaitForResetTest(TempCase):
    def runtime(self, retry_at):
        memory = Memory(self.db)
        self.addCleanup(memory.close)
        return Runtime(memory, cognition=LimitedCognition(retry_at))

    def test_it_waits_for_the_reset_instead_of_retrying_blind(self):
        reset = time.time() + 3000
        runtime = self.runtime(reset)
        runtime.start()
        report = runtime.cycle()
        self.assertEqual(report.cognition["failure"], "rate_limited")
        # Not the 60s first backoff: the wait is to the reset the provider named.
        self.assertAlmostEqual(report.cognition["retry_after"], 3030, delta=5)
        self.assertIn("resets", runtime.reason)

    def test_an_unknown_reset_keeps_the_ordinary_backoff(self):
        runtime = self.runtime(None)
        runtime.start()
        report = runtime.cycle()
        self.assertEqual(report.cognition["retry_after"], FAILURE_BACKOFF)

    def test_a_reset_already_past_never_shortens_the_backoff(self):
        # A provider's claim is not trusted into a hot retry loop.
        runtime = self.runtime(time.time() - 10_000)
        runtime.start()
        report = runtime.cycle()
        self.assertEqual(report.cognition["retry_after"], FAILURE_BACKOFF)

    def test_the_error_carries_the_time_from_the_provider_to_the_runtime(self):
        error = CognitionError("rate_limited", "usage limit", retry_at=123.0)
        self.assertEqual((error.category, error.retry_at), ("rate_limited", 123.0))
        result = CognitionResult(None, None, None, [
            Attempt("a", "rate_limited", True, 0.0, retry_at=100.0),
            Attempt("b", "rate_limited", True, 0.0, retry_at=200.0)], failure="rate_limited")
        self.assertEqual(result.retry_at, 200.0)  # the last one to free up decides


if __name__ == "__main__":
    unittest.main()
