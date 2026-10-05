"""Probes (the runtime's own senses) and quiet timer wakes.

A probe is a fixed operator-configured command the runtime runs at every
observation. With probes configured and no work active, a timer wake at which
nothing the runtime observes has changed since cognition last chose to do
nothing does not consult cognition again.
"""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kairo import Action, Decision, Environment, Memory, Runtime, State
from kairo import environment as environment_module
from kairo.environment import MAX_PROBES, PROBE_OUTPUT, parse_probe
from kairo.runtime import REASSESS
from kairo.situation import build_situation, render_situation

SRC = Path(__file__).resolve().parent.parent / "src"


class Scripted:
    name = "test"

    def __init__(self, script=lambda n: Decision(sleep=True, wake_after=600)):
        self.script, self.calls, self.contexts = script, 0, []

    def decide(self, context):
        self.calls += 1
        self.contexts.append(context)
        return self.script(self.calls)


class QuietCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.state.write_text("up\n")
        # Every observation is a fresh one: these tests change the world between cycles.
        patch = mock.patch.object(environment_module, "PROBE_REUSE", 0.0)
        patch.start()
        self.addCleanup(patch.stop)

    def runtime(self, cognition, probes=True, **kwargs) -> Runtime:
        env = Environment(probes={"state": ["cat", str(self.state)]} if probes else None)
        rt = Runtime(Memory(), env, cognition=cognition, reassess_after=300, **kwargs)
        rt.start()
        return rt

    def timer(self, rt: Runtime):
        """The sleep's own deadline passes; then one pass of the loop."""
        self.assertIs(rt.state, State.SLEEPING)
        rt.wake(REASSESS)
        return rt.cycle()


class ProbeTest(QuietCase):
    def test_a_probe_is_part_of_the_observation(self):
        rt = self.runtime(Scripted())
        self.assertEqual(rt.environment.observe()["probe.state"], {"exit": 0, "output": "up"})

    def test_cognition_sees_the_probe_and_what_changed(self):
        cognition = Scripted()
        rt = self.runtime(cognition)
        rt.cycle()
        self.state.write_text("down\n")
        rt.wake("test")
        rt.cycle()
        env = build_situation(cognition.contexts[-1])["environment"]
        self.assertEqual(env["facts"]["probe.state"], {"exit": 0, "output": "down"})
        self.assertEqual(env["since_previous_observation"]["changed"]["probe.state"],
                         {"before": {"exit": 0, "output": "up"},
                          "now": {"exit": 0, "output": "down"}})
        self.assertIn("untrusted", env["probes"])

    def test_a_probe_that_cannot_run_is_an_observation(self):
        env = Environment(probes={"gone": ["/nonexistent/kairo-probe"],
                                  "fails": [sys.executable, "-c", "import sys; sys.exit(3)"]})
        seen = env.observe()
        self.assertEqual(seen["probe.gone"], {"exit": None, "failure": "not_found"})
        self.assertEqual(seen["probe.fails"]["exit"], 3)

    def test_a_slow_probe_is_stopped(self):
        with mock.patch.object(environment_module, "PROBE_TIMEOUT", 0.2):
            seen = Environment(probes={"slow": ["sleep", "30"]}).observe()
        self.assertEqual(seen["probe.slow"], {"exit": None, "failure": "timed_out"})

    def test_probe_output_is_bounded_and_redacted(self):
        secret = "sk-live-0123456789abcdef"
        with mock.patch.dict("os.environ", {"PROVIDER_API_KEY": secret}):
            env = Environment(probes={
                "big": [sys.executable, "-c", "print('x' * 100000)"],
                "leak": [sys.executable, "-c", f"print('key {secret}')"]})
            seen = env.observe()
        self.assertLessEqual(len(seen["probe.big"]["output"]), PROBE_OUTPUT)
        self.assertNotIn(secret, str(seen))

    def test_results_are_reused_briefly(self):
        # Operator reads (status pages) observe too; they must not rerun every probe.
        with mock.patch.object(environment_module, "PROBE_REUSE", 60.0):
            env = Environment(probes={"state": ["cat", str(self.state)]})
            first = env.observe()
            self.state.write_text("down\n")
            self.assertEqual(env.observe(), first)

    def test_probe_settings_are_validated(self):
        self.assertEqual(parse_probe("web=systemctl is-active 'my service'"),
                         ("web", ["systemctl", "is-active", "my service"]))
        for bad in ("noequals", "Bad=true", "x=", "x='unbalanced"):
            with self.assertRaises(ValueError):
                parse_probe(bad)
        with self.assertRaises(ValueError):
            Environment(probes={f"p{i}": ["true"] for i in range(MAX_PROBES + 1)})

    def test_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run(
                [sys.executable, "-m", "kairo", "--db", f"{tmp}/k.db", "--situation",
                 "--probe", f"state=cat {self.state}"],
                capture_output=True, text=True, env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn('"probe.state"', out.stdout)
            bad = subprocess.run(
                [sys.executable, "-m", "kairo", "--db", f"{tmp}/k.db", "--situation",
                 "--probe", "x=true", "--probe", "x=false"],
                capture_output=True, text=True, env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"})
            self.assertEqual(bad.returncode, 2)


class QuietWakeTest(QuietCase):
    def test_an_unchanged_timer_wake_does_not_consult_cognition(self):
        cognition = Scripted()
        rt = self.runtime(cognition)
        rt.cycle()
        cycles = rt.memory.count("cycle")
        for n in (1, 2, 3):
            report = self.timer(rt)
            self.assertEqual(report.cognition, {"result": "skipped", "skipped": n})
            self.assertIs(rt.state, State.SLEEPING)
            self.assertAlmostEqual(rt.status()["wake_at"] - time.time(), 600, delta=5)
        self.assertEqual(cognition.calls, 1)
        self.assertEqual(rt.memory.count("cycle"), cycles)  # the cycle log stays cognition's
        self.assertEqual(rt.status()["quiet_wakes"], 3)
        self.assertIn("cognition not consulted", rt.reason)

    def test_a_change_in_a_probe_reaches_cognition(self):
        cognition = Scripted()
        rt = self.runtime(cognition)
        rt.cycle()
        self.timer(rt)
        self.timer(rt)
        self.state.write_text("down\n")
        report = self.timer(rt)
        self.assertEqual(report.cognition["result"], "decided")
        self.assertEqual(cognition.calls, 2)
        now = build_situation(cognition.contexts[-1])["now"]
        self.assertEqual(now["timer_wakes_without_cognition"]["count"], 2)
        changed = build_situation(cognition.contexts[-1])["environment"][
            "since_previous_observation"]["changed"]
        self.assertEqual(changed["probe.state"]["before"]["output"], "up")  # as it last saw it

    def test_every_other_wake_reaches_cognition(self):
        cognition = Scripted()
        rt = self.runtime(cognition)
        rt.cycle()
        for n, wake in enumerate((lambda: rt.accept_message("hello"),
                                  lambda: rt.request_wake("operator asked"),
                                  lambda: rt.add_directive("Keep it up", "The service.")), 2):
            wake()
            self.assertIs(rt.state, State.AWAKE)
            self.assertEqual(rt.cycle().cognition["result"], "decided")
            self.assertEqual(cognition.calls, n)

    def test_without_probes_cognition_is_always_consulted(self):
        cognition = Scripted()
        rt = self.runtime(cognition, probes=False)
        rt.cycle()
        for _ in range(3):
            self.assertEqual(self.timer(rt).cognition["result"], "decided")
        self.assertEqual(cognition.calls, 4)

    def test_active_work_is_never_left_unattended(self):
        create = {"op": "create", "ref": "w", "objective": "watch the service", "why": "test",
                  "directive_id": None, "strategy": "", "next_step": ""}
        cognition = Scripted(lambda n: Decision(sleep=True, wake_after=600,
                                                work=[create] if n == 1 else []))
        rt = self.runtime(cognition)
        rt.cycle()
        for _ in range(3):
            self.assertEqual(self.timer(rt).cognition["result"], "decided")
        self.assertEqual(cognition.calls, 4)

    def test_a_cycle_that_did_something_is_not_a_baseline(self):
        act = Action("process.run", {"argv": ["true"]})
        cognition = Scripted(lambda n: Decision(sleep=True, wake_after=600,
                                                actions=[act] if n == 1 else []))
        rt = self.runtime(cognition)
        rt.cycle()                      # acted: cognition has not yet seen the result
        self.assertEqual(self.timer(rt).cognition["result"], "decided")
        self.assertEqual(self.timer(rt).cognition["result"], "skipped")

    def test_cognition_is_consulted_again_after_quiet_max(self):
        cognition = Scripted()
        rt = self.runtime(cognition)
        rt.quiet_max = 3600
        rt.cycle()
        self.assertEqual(self.timer(rt).cognition["result"], "skipped")
        rt._quiet["since"] -= 3601
        self.assertEqual(self.timer(rt).cognition["result"], "decided")
        self.assertEqual(self.timer(rt).cognition["result"], "skipped")  # a new baseline

    def test_a_failed_cycle_is_not_a_baseline(self):
        def script(n):
            if n == 2:
                raise RuntimeError("provider down")
            return Decision(sleep=True, wake_after=600)

        cognition = Scripted(script)
        rt = self.runtime(cognition)
        rt.cycle()
        self.state.write_text("down\n")
        with self.assertLogs("kairo", "ERROR"):
            self.assertEqual(self.timer(rt).cognition["result"], "failed")
        self.assertEqual(self.timer(rt).cognition["result"], "decided")

    def test_a_restart_always_consults_cognition(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "k.db"
            for expected in (1, 2):
                cognition = Scripted()
                memory = Memory(db)
                rt = Runtime(memory, Environment(probes={"state": ["cat", str(self.state)]}),
                             cognition=cognition, reassess_after=300)
                rt.start()
                self.assertEqual(rt.cycle().cognition["result"], "decided")
                rt.stop()
                memory.close()

    def test_the_situation_renders_with_probes(self):
        rt = self.runtime(Scripted())
        text = render_situation(build_situation(rt.context()))
        self.assertIn("probe.state", text)


if __name__ == "__main__":
    unittest.main()
