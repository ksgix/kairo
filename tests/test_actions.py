import sys
import tempfile
import unittest
from pathlib import Path

from kairo import Action, ActionResult, Environment, Outcome, Verification, verify


class ActionTest(unittest.TestCase):
    def test_action_is_structured_data(self):
        a = Action("process.run", {"argv": ["true"]}, reason="probe")
        self.assertEqual(a.kind, "process.run")
        self.assertEqual(a.params, {"argv": ["true"]})
        self.assertTrue(a.id)
        self.assertNotEqual(a.id, Action("process.run").id)
        with self.assertRaises(AttributeError):
            a.kind = "other"  # frozen


class EnvironmentTest(unittest.TestCase):
    def test_observe_describes_host(self):
        obs = Environment().observe()
        for key in ("hostname", "platform", "user", "uid", "cwd", "python"):
            self.assertIn(key, obs)

    def test_run_process_with_argv(self):
        a = Action("process.run", {"argv": [sys.executable, "-c", "print('hi')"]})
        r = Environment().execute(a)
        self.assertTrue(r.executed)
        self.assertEqual(r.action_id, a.id)
        self.assertEqual(r.output["returncode"], 0)
        self.assertEqual(r.output["stdout"].strip(), "hi")

    def test_argv_is_not_shell_interpreted(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            a = Action("process.run", {"argv": ["echo", f"x; touch {marker}"]})
            r = Environment().execute(a)
            self.assertTrue(r.executed)
            self.assertFalse(marker.exists())

    def test_nonzero_exit_is_still_executed(self):
        r = Environment().execute(Action("process.run", {"argv": ["false"]}))
        self.assertTrue(r.executed)
        self.assertNotEqual(r.output["returncode"], 0)

    def test_rejects_raw_shell_text(self):
        r = Environment().execute(Action("process.run", {"argv": "ls -la"}))
        self.assertFalse(r.executed)
        self.assertIn("argv", r.error)

    def test_unknown_kind_and_missing_binary(self):
        env = Environment()
        self.assertFalse(env.execute(Action("teleport")).executed)
        r = env.execute(Action("process.run", {"argv": ["/nonexistent/kairo-binary"]}))
        self.assertFalse(r.executed)
        self.assertTrue(r.error)


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.action = Action("process.run", {"argv": ["true"]})

    def test_failed_execution_is_failure(self):
        v = verify(self.action, ActionResult(self.action.id, executed=False, error="boom"))
        self.assertIs(v.outcome, Outcome.FAILURE)
        self.assertIn("boom", v.detail)

    def test_no_verifier_is_unverifiable_not_success(self):
        v = verify(self.action, ActionResult(self.action.id, executed=True))
        self.assertIs(v.outcome, Outcome.UNVERIFIABLE)

    def test_verifier_decides_world_state(self):
        class ReturnCodeVerifier:
            def verify(self, action, result):
                ok = result.output.get("returncode") == 0
                return Verification(Outcome.SUCCESS if ok else Outcome.FAILURE,
                                    evidence={"returncode": result.output.get("returncode")})

        executed_but_wrong = ActionResult(self.action.id, executed=True, output={"returncode": 3})
        v = verify(self.action, executed_but_wrong, ReturnCodeVerifier())
        self.assertIs(v.outcome, Outcome.FAILURE)
        self.assertEqual(v.evidence, {"returncode": 3})

        good = ActionResult(self.action.id, executed=True, output={"returncode": 0})
        self.assertIs(verify(self.action, good, ReturnCodeVerifier()).outcome, Outcome.SUCCESS)


if __name__ == "__main__":
    unittest.main()
