"""Multiple cognition providers: a Kairo-owned Cognition layer asks providers in
a fixed order and falls back only on technical failure.

Fake providers here are deterministic adapters; nothing about model quality is
tested or implied.
"""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kairo import Action, Decision, Memory, Runtime, State
from kairo.claude import ClaudeCognition
from kairo.cognition import (
    FALLBACK, OUTCOMES, TERMINAL, Cognition, CognitionError, Context, as_cognition,
)
from kairo.environment import ACTIONS
from kairo.instructions import INSTRUCTIONS, cognition_request
from kairo.redact import MARKER, protect_files
from kairo.registry import build_cognition, parse_options, parse_order
from kairo.situation import build_situation
from test_cognition import FakeClaude, decision
from test_continuous import SRC, TIMEOUT
from test_work import create, only_open

PY = sys.executable


class Fake:
    """A provider adapter that replays outcomes: a Decision, or a category string
    (raised as CognitionError), or an exception instance (raised as is)."""

    def __init__(self, name, *outcomes, secret_env=()):
        self.name = name
        self.outcomes = list(outcomes)
        self.secret_env = tuple(secret_env)
        self.contexts = []

    @property
    def calls(self):
        return len(self.contexts)

    def decide(self, context):
        self.contexts.append(context)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, str):
            raise CognitionError(outcome, f"{self.name} failed: {outcome}")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def ctx():
    return Context(environment={"hostname": "h"}, directives=[], todo=[], messages=[],
                   runtime={"now": 1000.0}, available_actions=ACTIONS)


def quiet():
    return mock.patch.object(logging.getLogger("kairo.cognition"), "disabled", True)


# -- A, B, C, D, E: the cognition layer -------------------------------------------------


class CognitionLayerTest(unittest.TestCase):
    def test_a_every_outcome_has_a_defined_fallback_rule(self):
        self.assertEqual(OUTCOMES, FALLBACK | TERMINAL)
        self.assertFalse(FALLBACK & TERMINAL)
        self.assertEqual(TERMINAL, {"model_error"})
        for category in sorted(OUTCOMES):
            with self.subTest(category), quiet():
                first, second = Fake("a", category), Fake("b", Decision(sleep=True))
                result = Cognition([first, second]).decide(ctx())
                if category in FALLBACK:
                    self.assertEqual((second.calls, result.provider), (1, "b"))
                    self.assertTrue(result.attempts[0].fallback)
                else:
                    self.assertEqual((second.calls, result.decision, result.failure),
                                     (0, None, category))
                    self.assertFalse(result.attempts[0].fallback)

    def test_a_unknown_categories_and_raw_exceptions_are_provider_errors(self):
        with quiet():
            for outcome in ("made_up", ValueError("adapter bug")):
                result = Cognition([Fake("a", outcome), Fake("b", Decision())]).decide(ctx())
                self.assertEqual((result.attempts[0].outcome, result.provider),
                                 ("provider_error", "b"))

    def test_a_invalid_decision_falls_back_but_model_error_does_not(self):
        with quiet():
            # A contract-invalid answer is no decision: the next provider is asked.
            bad = Cognition([Fake("a", "invalid_decision"), Fake("b", Decision())]).decide(ctx())
            self.assertEqual(bad.provider, "b")
            # A provider returning something that is not a Decision is the same thing.
            odd = Cognition([Fake("a", {"sleep": True}), Fake("b", Decision())]).decide(ctx())
            self.assertEqual((odd.attempts[0].outcome, odd.provider), ("invalid_decision", "b"))
            # A model that ran and declined is never routed around.
            refused = Cognition([Fake("a", "model_error"), Fake("b", Decision())]).decide(ctx())
            self.assertEqual((refused.decision, refused.failure), (None, "model_error"))

    def test_b_c_fixed_order_and_one_attempt_each(self):
        with quiet():
            a, b, c = Fake("a", "timeout"), Fake("b", "unavailable"), Fake("c", Decision())
            result = Cognition([a, b, c]).decide(ctx())
        self.assertEqual([x.provider for x in result.attempts], ["a", "b", "c"])
        self.assertEqual((a.calls, b.calls, c.calls), (1, 1, 1))
        self.assertEqual(result.selection, {"position": 2,
                                            "reason": "fallback_after:b:unavailable"})
        with quiet():
            a2, b2, c2 = Fake("a", Decision()), Fake("b", Decision()), Fake("c", Decision())
            Cognition([a2, b2, c2]).decide(ctx())
        self.assertEqual((a2.calls, b2.calls, c2.calls), (1, 0, 0))

    def test_c_all_failing_asks_each_provider_once(self):
        with quiet():
            a, b = Fake("a", "timeout"), Fake("b", "timeout")
            result = Cognition([a, b]).decide(ctx())
        self.assertEqual((a.calls, b.calls), (1, 1))
        self.assertEqual((result.decision, result.provider, result.failure),
                         (None, None, "timeout"))

    def test_c_provider_names_must_be_unique(self):
        with self.assertRaises(ValueError):
            Cognition([Fake("a", Decision()), Fake("a", Decision())])
        with self.assertRaises(ValueError):
            Cognition([])

    def test_d_a_valid_decision_that_does_nothing_is_still_used(self):
        for valid in (Decision(sleep=True), Decision(), Decision(sleep=True, wake_after=60)):
            b = Fake("b", Decision(replies=["hi"]))
            result = Cognition([Fake("a", valid), b]).decide(ctx())
            self.assertEqual((result.provider, result.decision, b.calls), ("a", valid, 0))
            self.assertEqual(result.selection, {"position": 0, "reason": "first_in_order"})

    def test_same_context_with_only_the_asked_fact_differing(self):
        with quiet():
            a, b = Fake("a", "timeout"), Fake("b", Decision())
            Cognition([a, b]).decide(ctx())
        ca, cb = a.contexts[0], b.contexts[0]
        self.assertEqual(ca.runtime["cognition"], {"provider": "a", "selected": "first_in_order"})
        self.assertEqual(cb.runtime["cognition"],
                         {"provider": "b", "selected": "fallback_after:a:timeout"})
        strip = lambda c: {k: v for k, v in c.runtime.items() if k != "cognition"}  # noqa: E731
        self.assertEqual(strip(ca), strip(cb))
        self.assertIs(ca.environment, cb.environment)  # gathered once, not re-observed

    def test_attempt_details_are_bounded(self):
        with quiet():
            result = Cognition([Fake("a", CognitionError("timeout", "x" * 5000)),
                                Fake("b", Decision())]).decide(ctx())
        self.assertLess(len(result.attempts[0].detail), 400)


# -- F-K: through the real runtime ----------------------------------------------------


class RuntimeIntegrationTest(unittest.TestCase):
    def runtime(self, *providers, **kwargs):
        memory = Memory()
        self.addCleanup(memory.close)
        rt = Runtime(memory, cognition=Cognition(list(providers)), **kwargs)
        rt.start()
        return rt

    def last_cycle(self, rt):
        return rt.memory.all("cycle")[-1]["cognition"]

    def test_f_all_providers_failing_change_nothing(self):
        rt = self.runtime(Fake("a", "unavailable"), Fake("b", "rate_limited"))
        wid = rt.work.apply([create("w", "Keep the host healthy")]).refs["w"]
        before = rt.memory.get("work", wid)
        with self.assertLogs("kairo", "ERROR"):
            report = rt.cycle()
        self.assertIs(report.state, State.SLEEPING)
        self.assertIn("cognition error (rate_limited)", rt.reason)
        self.assertEqual(rt.memory.get("work", wid), before)
        self.assertEqual(rt.memory.all("action"), [])
        cog = self.last_cycle(rt)
        self.assertEqual((cog["result"], cog["provider"], cog["selection"]), ("failed", None, None))
        self.assertEqual([(a["provider"], a["outcome"]) for a in cog["attempts"]],
                         [("a", "unavailable"), ("b", "rate_limited")])
        rt.wake("again")  # still alive and able to cycle
        with self.assertLogs("kairo", "ERROR"):
            rt.cycle()

    def test_g_h_fallback_decision_takes_the_normal_path(self):
        fallback = Fake("b", Decision(work=[create("w", "Measure the disk")],
                                      actions=[Action("process.run", {"argv": ["echo", "ok"]},
                                                      work_id="w")], sleep=True))
        rt = self.runtime(Fake("a", "timeout"), fallback)
        with self.assertLogs("kairo", "ERROR"):
            report = rt.cycle()
        [work] = rt.work.all()
        [step] = report.steps
        self.assertEqual(step.action.work_id, work.id)  # linked through the normal path
        self.assertEqual(rt.work.attempts(work.id, 5)[0]["result"]["output"]["stdout"], "ok\n")
        cog = self.last_cycle(rt)
        self.assertEqual(cog["provider"], "b")
        self.assertEqual(cog["selection"], {"position": 1, "reason": "fallback_after:a:timeout"})
        self.assertEqual([(a["provider"], a["outcome"], a["fallback"]) for a in cog["attempts"]],
                         [("a", "timeout", True), ("b", "decided", False)])
        status = rt.status()
        self.assertEqual(status["cognition"], "a,b")
        self.assertEqual(status["cognition_last"]["provider"], "b")

    def test_g_fallback_cannot_bypass_the_repetition_rule(self):
        failing = [PY, "-c", "import sys; sys.exit(1)"]
        rt = self.runtime(Fake("a", "timeout"), Fake("b", Decision(sleep=False)))
        wid = rt.work.apply([create("w", "Fix it")]).refs["w"]
        rt.cognition = Cognition([Fake("a", Decision(actions=[Action("process.run", {"argv": failing},
                                                                     work_id=wid)], sleep=False))])
        rt.cycle()
        rt.cognition = Cognition([Fake("a", "timeout"),
                                  Fake("b", Decision(actions=[Action("process.run", {"argv": failing},
                                                                     work_id=wid)], sleep=True))])
        with self.assertLogs("kairo", "WARNING"):
            report = rt.cycle()
        self.assertEqual(report.steps, [])  # refused exactly as for any provider
        refused = [r for r in report.cognition["work"]["rejected"] if r["op"] == "action_refused"]
        self.assertEqual(len(refused), 1)

    def test_i_every_cycle_starts_from_the_first_provider(self):
        a = Fake("a", "unavailable", Decision(sleep=True))
        b = Fake("b", Decision(sleep=True))
        rt = self.runtime(a, b)
        with self.assertLogs("kairo", "ERROR"):
            rt.cycle()
        self.assertEqual(self.last_cycle(rt)["provider"], "b")
        rt.wake("again")
        rt.cycle()
        cog = self.last_cycle(rt)
        self.assertEqual((cog["provider"], cog["selection"]["reason"]), ("a", "first_in_order"))
        self.assertEqual((a.calls, b.calls), (2, 1))

    def test_j_swapping_the_order_needs_no_runtime_change(self):
        for order in (("x", "y"), ("y", "x")):
            with self.subTest(order=order):
                providers = {"x": Fake("x", "timeout"), "y": Fake("y", Decision(sleep=True))}
                rt = self.runtime(*(providers[n] for n in order))
                with self.assertLogs("kairo", "ERROR") if order[0] == "x" else _nothing():
                    rt.cycle()
                cog = self.last_cycle(rt)
                self.assertEqual(cog["provider"], "y")
                self.assertEqual([a["provider"] for a in cog["attempts"]],
                                 ["x", "y"] if order[0] == "x" else ["y"])

    def test_k_bare_provider_compatibility(self):
        memory = Memory()
        self.addCleanup(memory.close)
        bare = Fake("solo", Decision(sleep=True))
        rt = Runtime(memory, cognition=bare)
        self.assertIs(rt.cognition, bare)  # what was given is kept
        rt.start()
        rt.cycle()
        cog = memory.all("cycle")[-1]["cognition"]
        self.assertEqual((cog["provider"], cog["selection"]["reason"]), ("solo", "first_in_order"))
        self.assertEqual(rt.status()["cognition"], "solo")
        self.assertIsNone(as_cognition(None))

    def test_provenance_in_situation(self):
        a, b = Fake("a", "timeout", Decision(sleep=True)), Fake("b", Decision(sleep=True))
        rt = self.runtime(a, b)
        with self.assertLogs("kairo", "ERROR"):
            rt.cycle()
        rt.wake("again")
        rt.cycle()
        s = build_situation(a.contexts[-1])
        self.assertEqual(s["now"]["cognition"], {"provider": "a", "selected": "first_in_order"})
        [previous] = s["history"]["cycles"]["items"]
        self.assertEqual(previous["provider"], "b")  # an earlier decision by another provider
        # Nothing more about providers: no order, no other provider's details.
        self.assertNotIn("attempts", json.dumps(s["now"]))


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# -- L: Claude adapter error mapping, through the real adapter and a fake CLI -------------


class ClaudeMappingTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fake = FakeClaude(self.dir)
        env = mock.patch.dict(os.environ, self.fake.env)
        env.start()
        self.addCleanup(env.stop)

    def category(self, step, **kwargs):
        self.fake.plan(step)
        provider = ClaudeCognition(executable=str(self.fake.executable), **kwargs)
        with self.assertRaises(CognitionError) as caught:
            provider.decide(ctx())
        return caught.exception.category

    def envelope(self, status=None, subtype="error_during_execution", result=""):
        return {"type": "result", "subtype": subtype, "is_error": True,
                "api_error_status": status, "result": result}

    def test_l_claude_errors_map_to_outcomes(self):
        cases = {
            "process_failed": {"exit": 1, "stderr": "crashed"},
            "empty_output": {"stdout": ""},
            "invalid_output": {"stdout": "<html>gateway</html>"},
            "invalid_decision": {"decision": {"reason": "incomplete"}},
            "model_error": {"envelope": self.envelope(subtype="error_max_turns")},
            "auth_failed": {"envelope": self.envelope(401)},
            "rate_limited": {"envelope": self.envelope(429)},
            "unavailable": {"envelope": self.envelope(503)},
        }
        for expected, step in cases.items():
            with self.subTest(expected):
                self.assertEqual(self.category(step), expected)
        # Overloaded, an auth hint in the text, and an error report with a non-zero exit.
        self.assertEqual(self.category({"envelope": self.envelope(529)}), "rate_limited")
        self.assertEqual(self.category({"envelope": self.envelope(result="Invalid API key · Please run /login")}),
                         "auth_failed")
        self.assertEqual(self.category({"envelope": self.envelope(401), "exit": 1}), "auth_failed")

    def test_l_unavailable_and_timeout(self):
        missing = ClaudeCognition(executable=str(self.dir / "no-claude"))
        with self.assertRaises(CognitionError) as caught:
            missing.decide(ctx())
        self.assertEqual(caught.exception.category, "unavailable")
        self.assertEqual(self.category({"sleep": 30}, timeout=0.5), "timeout")


# -- M: credentials -----------------------------------------------------------------------


CRED_CLI = r'''#!{python}
import json, os, sys
sys.stdin.read()
json.dump(dict(os.environ), open({log!r}, "w"))
d = {{"reason": "r", "actions": [], "replies": [], "sleep": True, "wake_after": None, "work": []}}
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps(d), "structured_output": d}}))
'''


class CredentialTest(unittest.TestCase):
    secret = "alpha-cred-0123456789abcdef"  # its name below does not look secret

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"ALPHA_PROVIDER_CONF": self.secret,
                                           "ANTHROPIC_API_KEY": "sk-ant-test-0123456789"})
        env.start()
        self.addCleanup(env.stop)

    def test_m_secrets_stay_out_of_kairo_and_other_providers(self):
        log_file = self.dir / "claude-env.json"
        cli = self.dir / "claude"
        cli.write_text(CRED_CLI.format(python=PY, log=str(log_file)))
        cli.chmod(0o755)
        alpha = Fake("alpha", CognitionError("auth_failed", f"rejected credential {self.secret}"),
                     secret_env=("ALPHA_PROVIDER_CONF",))
        claude = ClaudeCognition(executable=str(cli))
        memory = Memory(self.dir / "k.db")
        self.addCleanup(memory.close)
        rt = Runtime(memory, cognition=Cognition([alpha, claude]))
        rt.start()
        with self.assertLogs("kairo", "ERROR") as logs:
            rt.cycle()
        # Claude's subprocess got its own credential but not alpha's.
        env = json.loads(log_file.read_text())
        self.assertNotIn("ALPHA_PROVIDER_CONF", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-test-0123456789")
        # The value never reached logs, context, records or the database.
        self.assertNotIn(self.secret, "\n".join(logs.output))
        self.assertNotIn(self.secret, json.dumps(build_situation(alpha.contexts[0]), default=str))
        self.assertIn(MARKER, memory.all("cycle")[-1]["cognition"]["attempts"][0]["detail"])
        memory.close()
        dump = "\n".join(sqlite3.connect(self.dir / "k.db").iterdump())
        self.assertNotIn(self.secret, dump)

    def test_m_actions_get_no_provider_credentials(self):
        argv = ["sh", "-c", 'echo "alpha=$ALPHA_PROVIDER_CONF claude=$ANTHROPIC_API_KEY"']
        rt = Runtime(Memory(), cognition=Cognition([
            Fake("alpha", Decision(actions=[Action("process.run", {"argv": argv})], sleep=True),
                 secret_env=("ALPHA_PROVIDER_CONF",)),
            ClaudeCognition(executable="/nonexistent")]))
        self.addCleanup(rt.memory.close)
        rt.start()
        [step] = rt.cycle().steps
        self.assertEqual(step.result.output["stdout"], "alpha= claude=\n")
        # An explicit value is redacted even though its name does not look secret.
        rt.cognition = Cognition([Fake("alpha", Decision(actions=[Action(
            "process.run", {"argv": ["echo", self.secret]})]), secret_env=("ALPHA_PROVIDER_CONF",))])
        rt.wake("again")
        rt.cycle()
        stored = json.dumps(rt.memory.all("action")[-1])
        self.assertNotIn(self.secret, stored)

    def test_m_credential_files_are_refused_and_their_secrets_redacted(self):
        creds = self.dir / "provider-creds.json"
        token = "tok-" + "Z9" * 20
        creds.write_text(json.dumps({"accessToken": token, "plan": "max"}))
        protect_files([creds])
        rt = Runtime(Memory(), cognition=Fake("alpha", Decision(sleep=False)))
        self.addCleanup(rt.memory.close)
        rt.start()
        direct = rt.act(Action("process.run", {"argv": ["cat", str(creds)]}))
        self.assertEqual((direct.result.executed, direct.result.failure), (False, "invalid_params"))
        self.assertIn("credential file", direct.result.error)
        # Reached indirectly (a glob): it runs, but the token is redacted on the way in.
        sneaky = rt.act(Action("process.run", {"argv": ["sh", "-c", f"cat {self.dir}/provider-cr*"]}))
        self.assertTrue(sneaky.result.executed)
        self.assertNotIn(token, json.dumps(rt.memory.all("action")))
        self.assertIn(MARKER, rt.memory.all("action")[-1]["result"]["output"]["stdout"])

    def test_m_claude_credential_file_is_protected_once_claude_is_configured(self):
        Cognition([ClaudeCognition(executable="/nonexistent")])
        rt = Runtime(Memory())
        self.addCleanup(rt.memory.close)
        step = rt.act(Action("process.run", {"argv": ["cat", os.path.expanduser("~/.claude/.credentials.json")]}))
        self.assertEqual(step.result.failure, "invalid_params")


# -- N: neutral instructions ---------------------------------------------------------------


class NeutralInstructionsTest(unittest.TestCase):
    def test_n_every_adapter_is_given_the_same_instructions_and_situation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeClaude(Path(tmp))
            fake.plan({"decision": decision()})
            context = ctx()
            with mock.patch.dict(os.environ, fake.env):
                ClaudeCognition(executable=str(fake.executable)).decide(context)
            [call] = fake.calls()
        argv = call["argv"]

        class Other:  # a second adapter with a different transport
            name = "other"

            def decide(self, context):
                self.request = cognition_request(context)
                return Decision()

        other = Other()
        other.decide(context)
        self.assertEqual(argv[argv.index("--system-prompt") + 1], other.request.instructions)
        self.assertEqual(call["stdin"], other.request.prompt)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), other.request.schema)
        self.assertEqual(other.request.instructions, INSTRUCTIONS)
        self.assertNotIn("Claude", INSTRUCTIONS)


# -- configuration ---------------------------------------------------------------------------


class RegistryTest(unittest.TestCase):
    def test_order_options_and_validation(self):
        self.assertEqual(parse_order("claude, claude@haiku"),
                         [("claude", "claude"), ("claude@haiku", "claude")])
        bad_orders = ["gemini", "claude,claude", "claude,", "Claude", "claude@"]
        for text in bad_orders:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_order(text)
        self.assertEqual(parse_options(["claude@haiku.model=haiku"], {"claude", "claude@haiku"}),
                         {"claude@haiku": {"model": "haiku"}})
        bad_options = ["claude.api_key=sk-1", "claude.token=x", "nope.model=x", "claude-model=x",
                       "claude.model"]
        for item in bad_options:
            with self.subTest(item=item), self.assertRaises(ValueError):
                parse_options([item], {"claude"})

    def test_build(self):
        cognition = build_cognition("claude,claude@haiku", ["claude@haiku.model=haiku",
                                                            "claude.timeout=5"],
                                    defaults={"claude": {"model": "sonnet", "timeout": "300"}})
        first, second = cognition.providers
        self.assertEqual((first.name, first.model, first.timeout), ("claude", "sonnet", 5.0))
        self.assertEqual((second.name, second.model, second.timeout), ("claude@haiku", "haiku", 300.0))
        self.assertIsNone(build_cognition("none"))
        for args in (("claude", ["claude.colour=red"]), ("claude", ["claude.timeout=-1"]),
                     ("none", ["claude.model=x"])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                build_cognition(*args)

    def test_cli_rejects_bad_configuration_clearly(self):
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        with tempfile.TemporaryDirectory() as tmp:
            for args, message in ((["--cognition", "gemini"], "unknown provider"),
                                  (["--cognition", "claude", "--provider-opt", "claude.api_key=sk"],
                                   "never passed on the command line")):
                out = subprocess.run([PY, "-m", "kairo", *args, "--db", f"{tmp}/k.db"],
                                     capture_output=True, text=True, env=env, timeout=TIMEOUT)
                self.assertEqual(out.returncode, 2)
                self.assertIn(message, out.stderr)


# -- the mandatory end-to-end fallback test ------------------------------------------------


class EndToEndFallbackTest(unittest.TestCase):
    def test_real_claude_unavailable_then_fallback_decides_through_the_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = ClaudeCognition(executable=str(Path(tmp) / "missing-claude"))

            class Scripted:
                name = "scripted"
                seen = []

                def decide(self, context):
                    self.seen.append(build_situation(context))
                    if len(self.seen) == 1:
                        return Decision(work=[create("w", "Check the disk")],
                                        actions=[Action("process.run", {"argv": ["echo", "42%"]},
                                                        reason="disk use", work_id="w")],
                                        reason="first look", sleep=True)
                    return Decision(sleep=True)

            fallback = Scripted()
            memory = Memory(Path(tmp) / "k.db")
            self.addCleanup(memory.close)
            rt = Runtime(memory, cognition=Cognition([claude, fallback]))
            rt.start()
            with self.assertLogs("kairo", "ERROR"):
                report = rt.cycle()  # the real cycle path

            [work] = rt.work.all()
            [step] = report.steps
            [attempt] = rt.work.attempts(work.id, 5)
            self.assertEqual((attempt["id"], attempt["strategy_revision"]), (step.action.id, 1))
            self.assertEqual(attempt["result"]["output"]["stdout"], "42%\n")
            cog = memory.all("cycle")[-1]["cognition"]
            self.assertEqual(cog["provider"], "scripted")
            self.assertEqual(cog["selection"], {"position": 1,
                                                "reason": "fallback_after:claude:unavailable"})
            self.assertEqual([(a["provider"], a["outcome"]) for a in cog["attempts"]],
                             [("claude", "unavailable"), ("scripted", "decided")])
            self.assertEqual(fallback.seen[0]["now"]["cognition"]["provider"], "scripted")

            rt.wake("next cycle")
            with self.assertLogs("kairo", "ERROR"):
                rt.cycle()
            cog = memory.all("cycle")[-1]["cognition"]
            self.assertEqual(cog["attempts"][0]["provider"], "claude")  # Claude asked first again
            # The fallback saw the work it created, as normal continuity.
            self.assertEqual(only_open(fallback.seen[1])["objective"], "Check the disk")
            self.assertEqual(fallback.seen[1]["history"]["cycles"]["items"][-1]["provider"],
                             "scripted")


if __name__ == "__main__":
    unittest.main()
