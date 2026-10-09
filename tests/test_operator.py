"""Phase 10A, the operator boundary: IPC as the one way a human reaches Kairo.

Reads are projections computed by the live runtime; inputs (messages,
directives) become persisted records that cognition sees; wake and stop are
lifecycle requests. Nothing here may execute, change work or deployment, or
store state outside the runtime. Every wait is bounded.
"""

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import kairo
from kairo import Decision, Environment, Memory, Runtime, State
from kairo import ipc
from kairo.cognition import CognitionError
from kairo.deploy import Deployment
from kairo.ipc import OPS, IPCServer, request
from kairo.memory import lock_database
from kairo.redact import MARKER, protect_env
from kairo.runtime import (CHAT_PAGE, CHAT_TEXT, DIRECTIVE_DESCRIPTION, DIRECTIVE_TEXT,
                           operator_message_id)
from test_cognition import FAKE_CLAUDE, decision
from test_continuous import TIMEOUT, Cognition, always_sleep

SRC = Path(kairo.__file__).resolve().parent.parent
SECRET_NAME = "KAIRO_OPERATOR_TEST_TOKEN"
SECRET = "op-secret-0123456789abcdefXYZ"


class OperatorCase(unittest.TestCase):
    """A runtime (default: no cognition) in a background thread, with its IPC server."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "kairo.db"
        self.sock = self.dir / "kairo.sock"

    def launch(self, cognition=None, environment=None):
        memory = Memory(self.db)
        runtime = Runtime(memory, environment, cognition=cognition)
        server = IPCServer(runtime, self.sock)
        server.start()
        thread = threading.Thread(target=runtime.run_forever, daemon=True)
        thread.start()

        def shutdown():
            runtime.request_stop()
            thread.join(TIMEOUT)
            server.close()
            memory.close()

        self.addCleanup(shutdown)
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        return runtime, thread

    def call(self, op, **fields):
        return request(self.sock, {"op": op, **fields}, timeout=TIMEOUT)

    def ok(self, op, **fields):
        response = self.call(op, **fields)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def error(self, op, code, **fields):
        response = self.call(op, **fields)
        self.assertIs(response["ok"], False, response)
        self.assertEqual(response["code"], code, response)
        return response["error"]

    def settle(self, runtime, cycles):
        """Wait until the runtime has completed ``cycles`` cycles and sleeps."""
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if runtime.memory.count("cycle") >= cycles and runtime.state is State.SLEEPING:
                return
            time.sleep(0.01)
        self.fail(f"expected {cycles} cycles, got {runtime.memory.count('cycle')}")


# -- A: reads ---------------------------------------------------------------------


class ReadTest(OperatorCase):
    def test_status_is_the_live_runtime(self):
        runtime, _ = self.launch()
        status = self.ok("status")
        self.assertEqual((status["state"], status["pid"], status["protocol"]),
                         ("sleeping", os.getpid(), 2))
        self.assertEqual(status["ops"], sorted(OPS))
        self.assertEqual((status["identity"], status["open_work"], status["directives"]),
                         (runtime.identity["id"], 0, 0))
        self.assertNotIn("revision", status)  # no deployment configured

    def test_status_reports_the_last_cognition_failure(self):
        def fail(context, n):
            raise CognitionError("unavailable", "provider down")
        self.launch(cognition=Cognition(fail))
        last = self.ok("status")["cognition_last"]
        self.assertEqual((last["result"], last["failure"]), ("failed", "unavailable"))
        self.assertIsNotNone(last["at"])

    def test_situation_is_what_cognition_sees(self):
        cognition = Cognition(always_sleep)
        runtime, _ = self.launch(cognition=cognition)
        situation = self.ok("situation")
        self.assertEqual(situation["kairo"]["identity"], runtime.identity["id"])
        # Facts only the live process has: its lifecycle state, its process and cycles.
        self.assertEqual(situation["now"]["lifecycle_state"], "sleeping")
        self.assertEqual(situation["now"]["process"]["cycles_completed"], len(cognition.contexts))
        self.assertEqual(set(situation), {"kairo", "now", "environment", "directives", "work",
                                          "history", "open_threads", "knowledge",
                                          "capabilities", "context"})
        self.assertLessEqual(len(json.dumps(situation)), 70_000)  # bounded by the situation budget

    def test_chat_shows_both_sides_in_order_and_pages(self):
        replies = iter(range(100))
        cognition = Cognition(lambda c, n: Decision(replies=[f"reply {next(replies)}"], sleep=True))
        runtime, _ = self.launch(cognition=cognition)
        self.ok("message", text="first")
        self.settle(runtime, 2)
        chat = self.ok("chat")
        self.assertEqual([(m["from"], m["text"]) for m in chat["messages"]],
                         [("kairo", "reply 0"), ("human", "first"), ("kairo", "reply 1")])
        seqs = [m["seq"] for m in chat["messages"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual((chat["more_before"], chat["more_after"]), (False, False))
        latest = self.ok("chat", limit=1)
        self.assertEqual(([m["text"] for m in latest["messages"]], latest["more_before"]),
                         (["reply 1"], True))
        after = self.ok("chat", after=seqs[0], limit=1)
        self.assertEqual(([m["text"] for m in after["messages"]], after["more_after"]),
                         (["first"], True))
        self.assertEqual(self.ok("chat", after=seqs[-1])["messages"], [])

    def test_chat_is_redacted_capped_and_tolerates_corrupt_records(self):
        protect_env([SECRET_NAME])
        with mock.patch.dict(os.environ, {SECRET_NAME: SECRET}):
            runtime, _ = self.launch()
            self.ok("message", text=f"the token is {SECRET}")
            self.settle(runtime, 2)  # the message's own cycle is over
            runtime.memory.put("message", "long", {"sender": "kairo", "text": "x" * (CHAT_TEXT + 5),
                                                   "at": time.time(), "id": "long"})
            runtime.memory.put("message", "bad", {"sender": "martian", "text": 5})
            with self.assertLogs("kairo", "WARNING"):  # the next cycle reports the corrupt record
                runtime.request_wake("check")
                self.settle(runtime, 3)
            messages = self.ok("chat")["messages"]
        self.assertNotIn(SECRET, json.dumps(messages))
        self.assertIn(MARKER, messages[0]["text"])
        self.assertEqual((len(messages[1]["text"]), messages[1]["truncated_from"]),
                         (CHAT_TEXT, CHAT_TEXT + 5))
        self.assertEqual(messages[2], {"seq": messages[2]["seq"], "unreadable": True})
        self.assertEqual(len(runtime.memory.get("message", "long")["text"]), CHAT_TEXT + 5)


# -- B: human input ---------------------------------------------------------------


class MessageTest(OperatorCase):
    def test_message_is_persisted_wakes_and_cognition_sees_it(self):
        cognition = Cognition(always_sleep)
        runtime, _ = self.launch(cognition=cognition)
        result = self.ok("message", text="Investigate why X is broken.")
        self.assertEqual(set(result), {"id", "state", "duplicate"})
        self.assertIs(result["duplicate"], False)
        cognition.wait_calls(2)
        seen = cognition.contexts[-1]
        self.assertEqual([(m.sender, m.text) for m in seen.messages],
                         [("human", "Investigate why X is broken.")])
        self.assertEqual(seen.wake_reason, "message received")
        self.assertEqual(runtime.memory.count("action"), 0)  # the message executed nothing

    def test_client_id_makes_delivery_idempotent_across_restart(self):
        runtime, _ = self.launch()
        first = self.ok("message", text="hello", id="op-1")
        self.settle(runtime, 2)
        again = self.ok("message", text="hello", id="op-1")
        self.assertEqual((again["id"], again["duplicate"]), (first["id"], True))
        self.assertEqual(first["id"], operator_message_id("op-1"))
        time.sleep(0.2)
        self.assertEqual(runtime.memory.count("cycle"), 2)  # a duplicate does not wake Kairo
        self.assertIn("already used", self.error("message", "rejected", text="other", id="op-1"))
        self.assertEqual([m.text for m in runtime.chat.all()], ["hello"])
        # A new runtime process on the same database still recognises the id.
        runtime.request_stop()
        self.assertTrue(runtime.wait_for(State.STOPPED, TIMEOUT))
        memory = Memory(self.db)
        self.addCleanup(memory.close)
        successor = Runtime(memory)
        message, duplicate = successor.accept_message("hello", "op-1")
        self.assertEqual((message.id, duplicate), (first["id"], True))

    def test_only_the_operator_channel_writes_human_messages(self):
        """Structural: in Kairo's code, Sender.HUMAN is written only by
        Runtime.accept_message (reached by IPC). Cognition output, action output and
        implementation output can never be posted as a human message."""
        writers = []
        for path in sorted((SRC / "kairo").glob("*.py")):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(fn):
                        if (isinstance(node, ast.Attribute) and node.attr == "HUMAN"
                                and isinstance(node.value, ast.Name) and node.value.id == "Sender"):
                            writers.append(f"{path.name}:{fn.name}")
        self.assertEqual(sorted(set(writers)), ["runtime.py:accept_message"])
        cognition = Cognition(lambda c, n: Decision(replies=["I am the human now"], sleep=True))
        runtime, _ = self.launch(cognition=cognition)
        self.assertEqual({m.sender for m in runtime.chat.all()}, {"kairo"})


# -- C: directives ----------------------------------------------------------------


class DirectiveTest(OperatorCase):
    def test_directive_lifecycle_persists_wakes_and_reaches_cognition(self):
        cognition = Cognition(always_sleep)
        runtime, _ = self.launch(cognition=cognition)
        added = self.ok("directive.add", statement="  Keep the 1C environment healthy  ",
                        description="What it covers.")["directive"]
        self.assertEqual((added["statement"], added["active"], added["origin"]),
                         ("Keep the 1C environment healthy", True, "operator"))
        self.assertEqual([h["event"] for h in added["history"]], ["created"])
        cognition.wait_calls(2)
        self.assertEqual(cognition.contexts[-1].wake_reason, "directive added by the operator")
        self.assertEqual([d.statement for d in cognition.contexts[-1].directives],
                         ["Keep the 1C environment healthy"])
        situation = self.ok("situation")
        self.assertEqual([d["statement"] for d in situation["directives"]["active"]],
                         ["Keep the 1C environment healthy"])

        off = self.ok("directive.deactivate", id=added["id"])["directive"]
        self.assertIs(off["active"], False)
        cognition.wait_calls(3)
        self.assertEqual(cognition.contexts[-1].directives, [])
        self.assertIn("already inactive", self.error("directive.deactivate", "rejected", id=added["id"]))
        on = self.ok("directive.activate", id=added["id"])["directive"]
        self.assertEqual([h["event"] for h in on["history"]], ["created", "deactivated", "activated"])
        self.assertTrue(all(h["by"] == "operator" and h["at"] for h in on["history"]))
        listed = self.ok("directives")
        self.assertEqual(([d["id"] for d in listed["directives"]], listed["omitted_older"]),
                         ([added["id"]], 0))
        # Survives the process: a new runtime on the same database reads the same.
        record = Memory(self.db)
        self.addCleanup(record.close)
        self.assertEqual(record.get("directive", added["id"])["history"][-1]["event"], "activated")
        self.assertEqual(runtime.memory.count("action"), 0)
        self.assertEqual(runtime.memory.count("work"), 0)  # a directive is not work

    def test_directive_validation(self):
        self.launch()
        self.error("directive.add", "invalid_params")
        # A description is required.
        self.error("directive.add", "invalid_params", statement="Keep host healthy")
        self.error("directive.add", "invalid_params", statement="Keep host healthy", description=5)
        self.error("directive.add", "rejected", statement="Keep host healthy", description="   ")
        self.error("directive.add", "rejected", statement="Keep host healthy",
                   description="x" * (DIRECTIVE_DESCRIPTION + 1))
        self.error("directive.add", "invalid_params", statement=5, description="What it covers.")
        self.error("directive.add", "rejected", statement="   ", description="What it covers.")
        self.error("directive.add", "rejected", statement="x" * (DIRECTIVE_TEXT + 1),
                   description="What it covers.")
        self.ok("directive.add", statement="Keep host healthy", description="What it covers.")
        self.assertIn("already says this",
                      self.error("directive.add", "rejected", statement="keep  HOST healthy",
                                 description="What it covers."))
        self.error("directive.deactivate", "rejected", id="no-such-directive")
        self.error("directive.activate", "invalid_params", id="")
        self.error("directive.activate", "invalid_params", id=["x"])

    def test_directives_are_redacted_when_read_out(self):
        protect_env([SECRET_NAME])
        with mock.patch.dict(os.environ, {SECRET_NAME: SECRET}):
            runtime, _ = self.launch()
            added = self.ok("directive.add", statement=f"Rotate the key {SECRET} monthly",
                            description="What it covers.")
            listed = self.ok("directives")
        self.assertNotIn(SECRET, json.dumps([added, listed]))
        self.assertIn(MARKER, listed["directives"][0]["statement"])


# -- D: protocol ------------------------------------------------------------------


class ProtocolTest(OperatorCase):
    def raw(self, data):
        import socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect(str(self.sock))
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            return json.loads(s.makefile("rb").readline())

    def test_error_codes(self):
        self.launch()
        for data, code in [(b"not json\n", "malformed_request"), (b"[1]\n", "malformed_request"),
                           (b"{" + b" " * (70 * 1024) + b"}\n", "malformed_request"),
                           (b"{}\n", "unknown_op"), (b'{"op": "execute"}\n', "unknown_op"),
                           (b'{"op": 7}\n', "unknown_op")]:
            response = self.raw(data)
            self.assertEqual((response["ok"], response["code"]), (False, code), data[:30])
        self.error("status", "invalid_params", argv=["touch", "x"])
        self.error("chat", "invalid_params", limit=0)
        self.error("chat", "invalid_params", limit=CHAT_PAGE + 1)
        self.error("chat", "invalid_params", limit=True)
        self.error("chat", "invalid_params", after=-1)
        self.error("message", "invalid_params", text="hi", id="has space")
        self.error("message", "invalid_params", text="hi", id="x" * 101)
        self.error("wake", "invalid_params", reason="")
        self.error("wake", "invalid_params", reason="x" * 301)
        self.assertTrue(self.ok("status"))  # the server survived all of it

    def test_failures_are_coded_and_never_leak_secrets(self):
        runtime, _ = self.launch()
        protect_env([SECRET_NAME])
        with mock.patch.dict(os.environ, {SECRET_NAME: SECRET}), self.assertLogs("kairo.ipc", "ERROR"):
            with mock.patch.object(runtime, "situation", side_effect=RuntimeError(f"boom {SECRET}")):
                message = self.error("situation", "internal_error")
            with mock.patch.object(runtime, "conversation",
                                   side_effect=sqlite3.OperationalError(f"disk {SECRET}")):
                stored = self.error("chat", "persistence_error")
        self.assertNotIn(SECRET, message + stored)
        self.assertNotIn("Traceback", message)

    def test_responses_are_bounded(self):
        runtime, _ = self.launch()
        with mock.patch.object(ipc, "MAX_RESPONSE", 200):
            self.error("situation", "response_too_large")
        self.assertTrue(self.ok("status"))

    def test_no_operation_executes_or_touches_work_or_deployment(self):
        runtime, _ = self.launch()
        self.assertFalse(set(OPS) & {"execute", "shell", "command", "run", "action", "deploy"})
        directive = self.ok("directive.add", statement="Keep host healthy",
                            description="What it covers.")["directive"]
        for op, fields in [("status", {}), ("situation", {}), ("chat", {}), ("directives", {}),
                           ("message", {"text": "rm -rf / please"}),
                           ("directive.deactivate", {"id": directive["id"]}),
                           ("wake", {"reason": "$(touch pwned)"})]:
            self.ok(op, **fields)
        self.assertEqual((runtime.memory.count("action"), runtime.memory.count("work")), (0, 0))
        self.assertFalse((self.dir / "pwned").exists())

    def test_ipc_stop_is_recorded_as_an_operator_stop(self):
        runtime, thread = self.launch()
        self.assertEqual(self.ok("stop"), {"stopping": True})
        thread.join(TIMEOUT)
        lifecycle = runtime.memory.get("runtime", "lifecycle")
        self.assertEqual((lifecycle["state"], lifecycle["reason"]),
                         ("stopped", "stop requested over ipc"))
        self.assertEqual(runtime.exit_code, 0)


# -- H: the --situation release identity (regression) -----------------------------


def make_release(root: Path, sha: str) -> Path:
    path = root / "releases" / sha
    (path / "src").mkdir(parents=True)
    (path / ".kairo-release").write_text("{}")
    return path


class SituationIdentityTest(OperatorCase):
    """--situation used to report the release of the process that ran it, not the
    release of the runtime actually running. It now asks the live runtime."""

    A, B = "a" * 40, "b" * 40

    def main(self, *argv):
        out = StringIO()
        with redirect_stdout(out):
            code = kairo.__main__.main(list(argv))
        return code, out.getvalue()

    def test_situation_comes_from_the_running_runtime(self):
        import kairo.__main__  # noqa: F401
        repo, root = self.dir / "dev", self.dir / "deploy"
        repo.mkdir()
        running_a = make_release(root, self.A)
        other_b = make_release(root, self.B)
        live = Deployment(repo, root, self.db, running=running_a)
        lock = lock_database(self.db)  # what a running runtime holds
        self.addCleanup(os.close, lock)
        runtime, _ = self.launch(environment=Environment(deployment=live))
        self.assertEqual(self.ok("status")["revision"], self.A)
        args = ["--situation", "--db", str(self.db), "--socket", str(self.sock),
                "--repository", str(repo), "--releases", str(root)]
        # The invoking process "runs" release B (e.g. an operator using a newer checkout).
        with mock.patch.object(kairo, "__file__", str(other_b / "src" / "kairo" / "__init__.py")):
            code, out = self.main(*args)
        self.assertEqual(code, 0)
        situation = json.loads(out)
        self.assertEqual(situation["kairo"]["code"]["running"]["revision"], self.A)
        self.assertEqual(situation["kairo"]["identity"], runtime.identity["id"])
        self.assertEqual(self.main(*args[:3], "--socket", str(self.dir / "nowhere.sock"))[0], 2)

    def test_a_preview_claims_no_running_release(self):
        import kairo.__main__  # noqa: F401
        repo, root = self.dir / "dev", self.dir / "deploy"
        repo.mkdir()
        other_b = make_release(root, self.B)
        with mock.patch.object(kairo, "__file__", str(other_b / "src" / "kairo" / "__init__.py")):
            code, out = self.main("--situation", "--db", str(self.db), "--repository", str(repo),
                                  "--releases", str(root))
        self.assertEqual(code, 0)
        running = json.loads(out)["kairo"]["code"]["running"]
        self.assertIsNone(running["revision"])
        self.assertIn("runs no release", running["note"])
        os.close(lock_database(self.db))  # the preview released the lock


# -- F, G, I: a real runtime process, the terminal client, restart -----------------


class TerminalSessionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "kairo.db"
        self.sock = self.dir / "kairo.sock"
        fake = self.dir / "claude"
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
        fake.chmod(0o755)
        self.fake = fake
        (self.dir / "plan.json").write_text(json.dumps([
            {"decision": decision(replies=["Noted. I will look into it."], sleep=True)}]))
        self.env = {**{k: v for k, v in os.environ.items() if k not in ("KAIRO_DB", "KAIRO_SOCKET")},
                    "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1",
                    "FAKE_CLAUDE_PLAN": str(self.dir / "plan.json"),
                    "FAKE_CLAUDE_LOG": str(self.dir / "calls")}

    def start(self, cognition=True):
        args = [sys.executable, "-m", "kairo", "--run", "--db", str(self.db), "--socket",
                str(self.sock), "--reassess", "0"]
        if cognition:
            args += ["--cognition", "claude", "--provider-opt", f"claude.executable={self.fake}"]
        proc = subprocess.Popen(args, env=self.env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        self.until(lambda: self.sock.exists() and self.cli("status")[0] == 0, "the runtime to start")
        return proc

    def cli(self, *args):
        out = subprocess.run([sys.executable, "-m", "kairo.ipc", "--socket", str(self.sock), *args],
                             env=self.env, capture_output=True, text=True, timeout=30)
        return out.returncode, out.stdout, out.stderr

    def until(self, predicate, what):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def chat(self):
        code, out, err = self.cli("--json", "chat")
        self.assertEqual(code, 0, err)
        return json.loads(out)["result"]["messages"]

    def stop(self, proc):
        self.assertEqual(self.cli("stop")[0], 0)
        self.assertEqual(proc.wait(20), 0)

    def test_operator_session_survives_restart(self):
        # Kairo without cognition: the message is accepted and persisted, unanswered.
        proc = self.start(cognition=False)
        code, out, _ = self.cli("directive", "add", "Keep the build server healthy",
                                "--description", "Builds stay fast and green.")
        self.assertEqual(code, 0)
        directive = json.loads(out)["result"]["directive"]
        code, out, _ = self.cli("message", "Investigate why the build is slow.", "--id", "m-1")
        self.assertEqual((code, json.loads(out)["result"]["duplicate"]), (0, False))
        self.assertEqual([m["from"] for m in self.chat()], ["human"])
        self.stop(proc)

        # Restart with cognition between the request and any answer: it is answered now.
        proc = self.start(cognition=True)
        self.until(lambda: [m["from"] for m in self.chat()] == ["human", "kairo"], "Kairo's reply")
        prompt = json.loads((self.dir / "calls" / "call-000.json").read_text())["stdin"]
        self.assertIn("Investigate why the build is slow.", prompt)
        self.assertIn("Keep the build server healthy", prompt)
        code, out, _ = self.cli("message", "Investigate why the build is slow.", "--id", "m-1")
        self.assertEqual(json.loads(out)["result"]["duplicate"], True)  # retried across restart
        code, out, _ = self.cli("chat")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"#\d+ \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC human: Investigate")
        self.assertRegex(out, r"kairo: Noted\. I will look into it\.")
        code, out, _ = self.cli("directives")
        self.assertIn(f"{directive['id']}  active", out)
        self.stop(proc)

        # Everything is in the one database, readable by the next process.
        memory = Memory(self.db)
        self.addCleanup(memory.close)
        self.assertEqual(len(memory.all("message")), 2)
        self.assertEqual(memory.get("directive", directive["id"])["origin"], "operator")
        self.assertEqual(memory.get("runtime", "lifecycle")["reason"], "stop requested over ipc")
        self.assertEqual(memory.count("action"), 0)

    def test_client_errors(self):
        code, _, err = self.cli("status")
        self.assertEqual(code, 2)
        self.assertIn("cannot reach Kairo", err)
        proc = self.start(cognition=False)
        code, out, _ = self.cli("directive", "deactivate", "no-such-id")
        self.assertEqual((code, json.loads(out)["code"]), (1, "rejected"))
        self.stop(proc)


if __name__ == "__main__":
    unittest.main()
