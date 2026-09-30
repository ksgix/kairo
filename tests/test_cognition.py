"""Cognition: decision parsing, context rendering, and the Claude provider.

The provider is exercised against a fake ``claude`` executable (a small script
driven by a plan file), so the real subprocess boundary is tested without a
live model. The live smoke test is in test_claude_live.py and is opt-in.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from kairo import Action, Memory, Runtime, State
from kairo.claude import SYSTEM_PROMPT, ClaudeCognition
from kairo.cognition import (
    CognitionError, Context, Decision, decision_schema, parse_decision,
)
from kairo.environment import ACTIONS
from kairo.redact import MARKER
from kairo.situation import build_situation
from test_continuous import SRC, TIMEOUT

FAKE_CLAUDE = r'''#!{python}
"""Fake Claude CLI. Plan: JSON list of steps, one per call (last one repeats)."""
import json, os, sys, time
plan = json.load(open(os.environ["FAKE_CLAUDE_PLAN"]))
log = os.environ["FAKE_CLAUDE_LOG"]
os.makedirs(log, exist_ok=True)
n = len(os.listdir(log))
prompt = sys.stdin.read()
with open(os.path.join(log, "call-%03d.json" % n), "w") as f:
    json.dump({{"argv": sys.argv[1:], "stdin": prompt}}, f)
step = plan[min(n, len(plan) - 1)]
if "sleep" in step:
    time.sleep(step["sleep"])
if "decision" in step:
    envelope = {{"type": "result", "subtype": "success", "is_error": False,
                "result": json.dumps(step["decision"]), "structured_output": step["decision"],
                "num_turns": 1, "duration_api_ms": 1200, "total_cost_usd": 0.01,
                "modelUsage": {{"fake-model": {{}}}}}}
    print(json.dumps(envelope))
if "envelope" in step:
    print(json.dumps(step["envelope"]))
if "stdout" in step:
    sys.stdout.write(step["stdout"])
if "stderr" in step:
    sys.stderr.write(step["stderr"])
sys.exit(step.get("exit", 0))
'''


def decision(**overrides):
    base = {"reason": "assessed", "actions": [], "replies": [], "sleep": True, "wake_after": None}
    return {**base, **overrides}


class FakeClaude:
    """A fake ``claude`` on disk plus the plan and call log that drive it."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.bin = directory / "bin"
        self.bin.mkdir()
        self.executable = self.bin / "claude"
        self.executable.write_text(FAKE_CLAUDE.format(python=sys.executable))
        self.executable.chmod(0o755)
        self.plan_file = directory / "plan.json"
        self.log = directory / "calls"
        self.env = {"FAKE_CLAUDE_PLAN": str(self.plan_file), "FAKE_CLAUDE_LOG": str(self.log)}

    def plan(self, *steps):
        self.plan_file.write_text(json.dumps(list(steps)))

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.log.iterdir())]


class CognitionCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fake = FakeClaude(self.dir)
        env = mock.patch.dict(os.environ, self.fake.env)
        env.start()
        self.addCleanup(env.stop)

    def provider(self, **kwargs):
        return ClaudeCognition(executable=str(self.fake.executable), **kwargs)

    def context(self, **kwargs):
        return Context(environment={"hostname": "h"}, directives=[], todo=[], messages=[],
                       available_actions=ACTIONS, **kwargs)

    def assertCognitionError(self, category, fn, *args):
        with self.assertRaises(CognitionError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.category, category, str(caught.exception))
        return caught.exception


class ParseDecisionTest(unittest.TestCase):
    def test_valid_decision(self):
        d = parse_decision(decision(
            actions=[{"kind": "process.run", "params": {"argv": ["df", "-h"]}, "reason": "disk"}],
            replies=["hi", "  "], sleep=False, wake_after=60), ACTIONS)
        self.assertIsInstance(d, Decision)
        self.assertEqual((d.actions[0].kind, d.actions[0].params, d.actions[0].reason),
                         ("process.run", {"argv": ["df", "-h"]}, "disk"))
        self.assertEqual(d.replies, ["hi"])  # blank replies dropped
        self.assertEqual((d.sleep, d.wake_after, d.reason), (False, 60.0, "assessed"))

    def test_invalid_decisions(self):
        bad = {
            "not an object": ["sleep"],
            "missing fields": {"reason": "x", "sleep": True},
            "unknown field": decision(shell="rm -rf /"),
            "sleep not bool": decision(sleep="yes"),
            "reason not str": decision(reason=None),
            "replies not strings": decision(replies=[1]),
            "negative wake": decision(wake_after=-5),
            "bool wake": decision(wake_after=True),
            "infinite wake": decision(wake_after=float("inf")),
            "actions not list": decision(actions={"kind": "process.run"}),
            "unsupported kind": decision(actions=[{"kind": "shell", "params": {"cmd": "ls"},
                                                   "reason": ""}]),
            "raw shell text": decision(actions=["ls -la"]),
            "action missing reason": decision(actions=[{"kind": "process.run",
                                                        "params": {"argv": ["ls"]}}]),
            "params not object": decision(actions=[{"kind": "process.run", "params": "ls",
                                                    "reason": ""}]),
        }
        for label, data in bad.items():
            with self.subTest(label):
                with self.assertRaises(CognitionError) as caught:
                    parse_decision(data, ACTIONS)
                self.assertEqual(caught.exception.category, "invalid_decision")

    def test_schema_matches_parser(self):
        schema = decision_schema(ACTIONS)
        self.assertEqual(set(schema["required"]), set(decision(**{}).keys()))
        self.assertFalse(schema["additionalProperties"])
        [variant] = schema["properties"]["actions"]["items"]["anyOf"]
        self.assertEqual(variant["properties"]["kind"], {"const": "process.run"})
        self.assertEqual(variant["properties"]["params"], ACTIONS["process.run"]["params"])


class RenderContextTest(unittest.TestCase):
    def test_context_is_bounded_and_redacted(self):
        secret = "sk-test-abcdefghijklmnop"
        with mock.patch.dict(os.environ, {"SOME_API_KEY": secret}):
            ctx = Context(
                environment={"hostname": "h"}, directives=[], todo=[], messages=[],
                recent_actions=[{"id": "a", "result": {"output": {
                    "stdout": f"SOME_API_KEY={secret}\n" + "x" * 5000}}}],
                available_actions=ACTIONS)
            text = json.dumps(build_situation(ctx))
        self.assertNotIn(secret, text)
        self.assertIn(MARKER, text)
        self.assertIn("truncated", text)
        self.assertLess(len(text), 12000)


class ClaudeProviderTest(CognitionCase):
    def test_valid_response_becomes_decision(self):
        self.fake.plan({"decision": decision(
            actions=[{"kind": "process.run", "params": {"argv": ["uptime"]}, "reason": "load"}],
            replies=["Checking load."], sleep=False)})
        d = self.provider().decide(self.context())
        self.assertEqual([a.params["argv"] for a in d.actions], [["uptime"]])
        self.assertEqual(d.replies, ["Checking load."])
        self.assertFalse(d.sleep)
        self.assertEqual(d.meta["cost_usd"], 0.01)
        self.assertEqual(d.meta["models"], ["fake-model"])

    def test_invocation_is_locked_down(self):
        self.fake.plan({"decision": decision()})
        self.provider(model="sonnet").decide(self.context())
        [call] = self.fake.calls()
        argv = call["argv"]
        # Claude gets no tools, settings, hooks, MCP servers or skills.
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        for flag in ("-p", "--restricted", "--strict-mcp-config", "--disable-slash-commands",
                     "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], SYSTEM_PROMPT)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]),
                         decision_schema(ACTIONS))
        # The context travels on stdin, not in the process list.
        self.assertTrue(call["stdin"].startswith("Kairo situation:"))
        self.assertNotIn("hostname", " ".join(argv))

    def test_result_text_fallback(self):
        self.fake.plan({"envelope": {"type": "result", "subtype": "success", "is_error": False,
                                     "result": json.dumps(decision(reason="from text"))}})
        self.assertEqual(self.provider().decide(self.context()).reason, "from text")

    def test_failures_are_categorised(self):
        cases = [
            ("process_failed", {"exit": 1, "stderr": "boom"}),
            ("empty_output", {"stdout": ""}),
            ("invalid_output", {"stdout": "not json at all"}),
            ("invalid_output", {"envelope": ["not", "an", "object"]}),
            ("model_error", {"envelope": {"type": "result", "subtype": "error_max_turns",
                                          "is_error": True}}),
            ("empty_output", {"envelope": {"type": "result", "subtype": "success",
                                           "is_error": False, "result": ""}}),
            ("invalid_decision", {"envelope": {"type": "result", "subtype": "success",
                                               "is_error": False, "result": "I think we sleep"}}),
            ("invalid_decision", {"decision": {"reason": "missing the rest"}}),
            ("invalid_decision", {"decision": decision(actions=[
                {"kind": "sudo.anything", "params": {}, "reason": ""}])}),
        ]
        for category, step in cases:
            with self.subTest(category=category, step=step):
                self.fake.plan(step)
                self.assertCognitionError(category, self.provider().decide, self.context())

    def test_process_failure_message_is_bounded(self):
        self.fake.plan({"exit": 3, "stderr": "e" * 10_000})
        err = self.assertCognitionError("process_failed", self.provider().decide, self.context())
        self.assertLess(len(str(err)), 400)

    def test_timeout(self):
        self.fake.plan({"sleep": 30, "decision": decision()})
        self.assertCognitionError("timeout", self.provider(timeout=0.5).decide, self.context())

    def test_unavailable(self):
        missing = ClaudeCognition(executable=str(self.dir / "no-such-claude"))
        self.assertCognitionError("unavailable", missing.decide, self.context())
        not_executable = self.dir / "claude-noexec"
        not_executable.write_text("")
        self.assertCognitionError("unavailable",
                                  ClaudeCognition(executable=str(not_executable)).decide,
                                  self.context())


class ClaudeInRuntimeTest(CognitionCase):
    """The provider driving the real runtime."""

    def runtime(self, **kwargs):
        memory = Memory(self.dir / "kairo.db")
        self.addCleanup(memory.close)
        return Runtime(memory, cognition=self.provider(**kwargs))

    def run_loop(self, runtime):
        thread = threading.Thread(target=runtime.run_forever, daemon=True)
        thread.start()

        def shutdown():
            runtime.request_stop()
            thread.join(TIMEOUT)

        self.addCleanup(shutdown)
        return thread

    def test_context_carries_runtime_information(self):
        self.fake.plan({"decision": decision()})
        runtime = self.runtime()
        d = runtime.directives.add("Keep the host healthy.")
        runtime.todo.add("check backups", directive_id=d.id)
        runtime.chat.post("human", "anything wrong?")
        runtime.start()
        runtime.act(Action("process.run", {"argv": ["echo", "earlier"]}, reason="probe"))
        runtime.cycle()

        situation = json.loads(self.fake.calls()[0]["stdin"].split("\n", 1)[1])
        self.assertEqual(situation["kairo"]["identity"], runtime.identity["id"])
        self.assertEqual(situation["now"]["lifecycle_state"], "awake")
        self.assertEqual(situation["now"]["wake_reason"], "first start")
        self.assertIn("hostname", situation["environment"]["facts"])
        [directive] = situation["directives"]["active"]
        self.assertEqual((directive["id"], directive["statement"]), (d.id, "Keep the host healthy."))
        self.assertEqual(situation["todo"]["open"][0]["description"], "check backups")
        self.assertEqual(situation["history"]["chat"]["items"][0]["text"], "anything wrong?")
        [earlier] = situation["history"]["actions"]["items"]
        self.assertEqual(earlier["stdout"], "earlier\n")
        self.assertEqual(earlier["verification"]["outcome"], "unverifiable")
        self.assertEqual(earlier["state"], "executed_unverified")
        self.assertIn("process.run", situation["capabilities"]["actions"])

    def test_valid_action_is_executed_by_runtime(self):
        marker = self.dir / "marker"
        self.fake.plan({"decision": decision(actions=[{
            "kind": "process.run", "reason": "leave a mark",
            "params": {"argv": [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ok')"]},
        }], replies=["Done, pending verification."], sleep=True, wake_after=3600)})
        runtime = self.runtime()
        runtime.start()
        report = runtime.cycle()

        self.assertEqual(marker.read_text(), "ok")
        [step] = report.steps
        self.assertTrue(step.result.executed)
        self.assertEqual(runtime.memory.get("action", step.action.id)["status"], "finished")
        self.assertEqual(runtime.chat.all()[-1].text, "Done, pending verification.")
        # The sleep decision reached the existing sleep behaviour, deadline included.
        self.assertIs(runtime.state, State.SLEEPING)
        self.assertIsNotNone(runtime.status()["wake_at"])
        [cycle] = runtime.memory.all("cycle")
        self.assertEqual(cycle["cognition"]["result"], "decided")
        self.assertEqual(cycle["cognition"]["requested"], ["process.run"])
        self.assertEqual(cycle["actions"][0]["kind"], "process.run")

    def test_secrets_stay_out_of_context_and_memory(self):
        secret = "ghp_" + "S3cr3tT0k3nValue" * 2
        self.fake.plan(
            {"decision": decision(actions=[{"kind": "process.run", "reason": "inspect env",
                                            "params": {"argv": ["sh", "-c", "echo GITHUB_TOKEN=$GITHUB_TOKEN"]}}],
                                  sleep=False)},
            {"decision": decision()},
        )
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": secret}):
            runtime = self.runtime()
            runtime.start()
            runtime.cycle()
            runtime.cycle()
        second_prompt = self.fake.calls()[1]["stdin"]
        self.assertIn("GITHUB_TOKEN=" + MARKER, second_prompt)
        self.assertNotIn(secret, second_prompt)
        runtime.memory.close()
        dump = "\n".join(sqlite3.connect(self.dir / "kairo.db").iterdump())
        self.assertNotIn(secret, dump)

    def test_failures_do_not_kill_run_forever(self):
        self.fake.plan({"exit": 1, "stderr": "auth expired"},
                       {"stdout": "garbage"},
                       {"sleep": 30},
                       {"decision": decision(reason="recovered")})
        runtime = self.runtime(timeout=0.5)
        with self.assertLogs("kairo", "ERROR"):
            thread = self.run_loop(runtime)
            for expected in ("process_failed", "invalid_output", "timeout"):
                self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
                self.assertIn(f"cognition error ({expected})", runtime.reason)
                self.assertTrue(thread.is_alive())
                runtime.request_wake("retry")
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        self.assertEqual(runtime.reason, "recovered")
        failures = [c["cognition"].get("failure") for c in runtime.memory.all("cycle")]
        self.assertEqual(failures, ["process_failed", "invalid_output", "timeout", None])
        runtime.stop()
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())


class CliWithClaudeTest(unittest.TestCase):
    """``python -m kairo --run --cognition claude`` with the fake CLI on PATH, over IPC."""

    def test_foreground_run_with_claude_cognition(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = FakeClaude(tmp)
            fake.plan({"decision": decision(reason="nothing needs attention")},
                      {"decision": decision(replies=["Hello, human."], reason="answered")})
            env = {**os.environ, **fake.env, "PYTHONPATH": str(SRC),
                   "PATH": f"{fake.bin}{os.pathsep}{os.environ['PATH']}"}
            sock, db = tmp / "k.sock", tmp / "k.db"
            proc = subprocess.Popen(
                [sys.executable, "-m", "kairo", "--run", "--cognition", "claude",
                 "--db", str(db), "--socket", str(sock), "--reassess", "0"],
                env=env, stderr=subprocess.PIPE, text=True)
            try:
                lines = iter(proc.stderr)
                wait = lambda text: next(l for l in lines if text in l)  # noqa: E731
                timer = threading.Timer(TIMEOUT * 2, proc.kill)
                timer.start()
                wait("sleeping: nothing needs attention")
                ipc = lambda *a: json.loads(subprocess.run(  # noqa: E731
                    [sys.executable, "-m", "kairo.ipc", "--socket", str(sock), *a],
                    env=env, capture_output=True, text=True, timeout=TIMEOUT).stdout)["result"]
                self.assertEqual(ipc("status")["cognition"], "claude")
                ipc("message", "hello")
                wait("sleeping: answered")
                ipc("stop")
                self.assertEqual(proc.wait(TIMEOUT), 0)
                timer.cancel()
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stderr.close()
            memory = Memory(db)
            try:
                self.assertEqual([(m["sender"], m["text"]) for m in memory.all("message")],
                                 [("human", "hello"), ("kairo", "Hello, human.")])
            finally:
                memory.close()
            self.assertIn('"text": "hello"', fake.calls()[1]["stdin"])


if __name__ == "__main__":
    unittest.main()
