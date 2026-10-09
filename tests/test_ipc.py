"""Local IPC: a running Kairo reached from outside over its Unix socket.

Synchronisation is on runtime state, cognition calls, socket readiness and
log lines, never fixed sleeps; every wait is bounded.
"""

import json
import os
import queue
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from kairo import Memory, Runtime, State
from kairo.ipc import IPCError, IPCServer, request
from test_continuous import SRC, TIMEOUT, Cognition, always_sleep


def raw(path, data: bytes) -> dict | None:
    """Send raw bytes, return the parsed response (None if none came back)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(TIMEOUT)
        sock.connect(str(path))
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        with sock.makefile("rb") as f:
            line = f.readline()
    return json.loads(line) if line else None


class IPCCase(unittest.TestCase):
    """A Runtime looping in a thread, with an IPCServer attached."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "kairo.db"
        self.sock = self.dir / "kairo.sock"

    def launch(self, cognition=None):
        memory = Memory(self.db)
        self.addCleanup(memory.close)
        runtime = Runtime(memory, cognition=cognition)
        server = IPCServer(runtime, self.sock)
        server.start()
        self.addCleanup(server.close)
        thread = threading.Thread(target=runtime.run_forever, daemon=True)
        thread.start()

        def shutdown():
            runtime.request_stop()
            thread.join(TIMEOUT)

        self.addCleanup(shutdown)
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        return runtime, server, thread


class ServerTest(IPCCase):
    def test_server_starts_with_private_socket(self):
        self.launch()
        mode = os.stat(self.sock).st_mode
        self.assertTrue(stat.S_ISSOCK(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_status(self):
        runtime, _, _ = self.launch(Cognition(always_sleep))
        runtime.directives.add("Keep the host healthy.")
        response = request(self.sock, {"op": "status"})
        self.assertTrue(response["ok"])
        status = response["result"]
        self.assertEqual(status["state"], "sleeping")
        self.assertEqual(status["identity"], runtime.identity["id"])
        self.assertEqual(status["starts"], 1)
        self.assertTrue(status["running"])
        self.assertEqual(status["directives"], 1)
        self.assertNotIn("open_todo", status)  # removed with todo; protocol stays 2
        self.assertEqual(status["cognition"], "test")
        self.assertEqual(status["pid"], os.getpid())
        self.assertIn("wake_at", status)

    def test_message_is_received_and_wakes_sleeping_runtime(self):
        cognition = Cognition(always_sleep)
        runtime, _, _ = self.launch(cognition)
        response = request(self.sock, {"op": "message", "text": "hello Kairo"})
        self.assertTrue(response["ok"])
        cognition.wait_calls(2)
        ctx = cognition.contexts[1]
        self.assertEqual(ctx.wake_reason, "message received")
        self.assertEqual([m.text for m in ctx.messages], ["hello Kairo"])
        self.assertEqual(ctx.messages[0].id, response["result"]["id"])
        # One chat, not a second one: it is in the runtime's persistent chat.
        self.assertEqual([m.text for m in runtime.chat.all()], ["hello Kairo"])

    def test_wake(self):
        cognition = Cognition(always_sleep)
        self.launch(cognition)
        response = request(self.sock, {"op": "wake", "reason": "manual wake"})
        self.assertEqual(response["result"]["accepted"], True)
        cognition.wait_calls(2)
        self.assertEqual(cognition.contexts[1].wake_reason, "manual wake")

    def test_stop_is_graceful_and_leaves_valid_database(self):
        runtime, server, thread = self.launch()
        self.assertEqual(request(self.sock, {"op": "stop"}),
                         {"ok": True, "result": {"stopping": True}})
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())
        self.assertIs(runtime.state, State.STOPPED)
        server.close()
        self.assertFalse(self.sock.exists(), "socket left behind after shutdown")
        runtime.memory.close()
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class MalformedRequestTest(IPCCase):
    def test_bad_requests_get_errors_and_server_survives(self):
        runtime, _, thread = self.launch()
        cases = [
            b"not json\n",
            b"\xff\xfe\n",
            b"[1, 2]\n",
            b"{}\n",
            b'{"op": "reboot"}\n',
            b'{"op": "message"}\n',
            b'{"op": "message", "text": 42}\n',
            b'{"op": "message", "text": "   "}\n',
            b'{"op": "wake", "reason": ["x"]}\n',
            b"{" + b" " * (70 * 1024) + b"}\n",
        ]
        for data in cases:
            with self.subTest(data=data[:40]):
                response = raw(self.sock, data)
                self.assertIs(response["ok"], False)
                self.assertTrue(response["error"])
        # Connect-and-leave and a request without newline must not wedge it either.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(self.sock))
        self.assertTrue(raw(self.sock, b'{"op": "status"}')["ok"])
        self.assertTrue(request(self.sock, {"op": "status"})["ok"])
        self.assertTrue(thread.is_alive())
        self.assertEqual(runtime.chat.all(), [])

    def test_ipc_cannot_execute_commands(self):
        runtime, _, _ = self.launch()
        marker = self.dir / "pwned"
        attempts = [
            {"op": "execute", "argv": ["touch", str(marker)]},
            {"op": "action", "kind": "process.run", "params": {"argv": ["touch", str(marker)]}},
            {"op": "status", "argv": ["touch", str(marker)]},
            {"op": "wake", "reason": f"$(touch {marker})"},
        ]
        for attempt in attempts:
            request(self.sock, attempt)
        text = f"; touch {marker} && `touch {marker}`"
        self.assertTrue(request(self.sock, {"op": "message", "text": text})["ok"])
        self.assertTrue(runtime.wait_for(State.SLEEPING, TIMEOUT))
        self.assertFalse(marker.exists())
        self.assertEqual(runtime.memory.all("action"), [])
        self.assertEqual([m.text for m in runtime.chat.all()], [text])  # stored verbatim


class SocketOwnershipTest(IPCCase):
    def test_stale_socket_is_replaced(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.sock))
        stale.close()  # file remains, nobody listening: a crashed process
        with self.assertLogs("kairo.ipc", "WARNING") as logs:
            self.launch()
        self.assertIn("stale socket", logs.output[0])
        self.assertTrue(request(self.sock, {"op": "status"})["ok"])

    def test_active_socket_is_not_disrupted(self):
        runtime, _, _ = self.launch()
        other = IPCServer(Runtime(Memory()), self.sock)
        with self.assertRaises(IPCError):
            other.start()
        other.close()
        # The first server still owns and serves the socket.
        status = request(self.sock, {"op": "status"})["result"]
        self.assertEqual(status["identity"], runtime.identity["id"])

    def test_non_socket_file_is_never_replaced(self):
        self.sock.write_text("important")
        server = IPCServer(Runtime(Memory()), self.sock)
        with self.assertRaises(IPCError):
            server.start()
        server.close()
        self.assertEqual(self.sock.read_text(), "important")

    def test_close_does_not_remove_a_socket_it_no_longer_owns(self):
        _, server, _ = self.launch()
        self.sock.unlink()
        newer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(newer.close)
        newer.bind(str(self.sock))
        server.close()
        self.assertTrue(self.sock.exists())


class EndToEndTest(unittest.TestCase):
    """A real ``python -m kairo --run`` process driven by ``python -m kairo.ipc``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "kairo.db"
        self.sock = self.dir / "kairo.sock"
        self.env = {**os.environ, "PYTHONPATH": str(SRC)}

    def spawn(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "kairo", "--run", "--db", str(self.db),
             "--socket", str(self.sock), "--reassess", "0"],
            env=self.env, stderr=subprocess.PIPE, text=True)
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in proc.stderr], daemon=True).start()

        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(TIMEOUT)
            proc.stderr.close()

        self.addCleanup(cleanup)
        return proc, lines

    def await_log(self, lines, text):
        while True:
            try:
                line = lines.get(timeout=TIMEOUT)
            except queue.Empty:
                self.fail(f"timed out waiting for log line containing {text!r}")
            if text in line:
                return line

    def client(self, *args):
        out = subprocess.run(
            [sys.executable, "-m", "kairo.ipc", "--socket", str(self.sock), *args],
            env=self.env, capture_output=True, text=True, timeout=TIMEOUT)
        self.assertEqual(out.returncode, 0, out.stderr)
        response = json.loads(out.stdout)
        self.assertTrue(response["ok"])
        return response["result"]

    def test_full_session_over_ipc(self):
        proc, lines = self.spawn()
        self.await_log(lines, "sleeping: no cognition provider configured")

        status = self.client("status")
        self.assertEqual((status["state"], status["pid"], status["running"]),
                         ("sleeping", proc.pid, True))

        self.client("message", "hello Kairo")
        self.await_log(lines, "awake: message received")
        self.await_log(lines, "sleeping:")
        self.client("wake", "manual wake")
        self.await_log(lines, "awake: manual wake")
        self.await_log(lines, "sleeping:")
        self.assertEqual(self.client("status")["pid"], proc.pid)

        self.assertEqual(self.client("stop"), {"stopping": True})
        self.assertEqual(proc.wait(TIMEOUT), 0)
        self.assertFalse(self.sock.exists())

        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        memory = Memory(self.db)
        self.addCleanup(memory.close)
        self.assertEqual(memory.get("runtime", "lifecycle")["state"], "stopped")
        self.assertEqual([m["text"] for m in memory.all("message")], ["hello Kairo"])

    def test_second_instance_refuses_the_socket(self):
        first, lines = self.spawn()
        self.await_log(lines, "sleeping:")
        second = subprocess.run(
            [sys.executable, "-m", "kairo", "--run", "--db", str(self.dir / "other.db"),
             "--socket", str(self.sock)],
            env=self.env, capture_output=True, text=True, timeout=TIMEOUT)
        self.assertEqual(second.returncode, 1)
        self.assertIn("already listening", second.stderr)
        self.assertEqual(self.client("status")["pid"], first.pid)
        self.client("stop")
        self.assertEqual(first.wait(TIMEOUT), 0)

    def test_client_reports_unreachable_kairo(self):
        out = subprocess.run(
            [sys.executable, "-m", "kairo.ipc", "--socket", str(self.sock), "status"],
            env=self.env, capture_output=True, text=True, timeout=TIMEOUT)
        self.assertEqual(out.returncode, 2)
        self.assertIn("cannot reach Kairo", out.stderr)


if __name__ == "__main__":
    unittest.main()
