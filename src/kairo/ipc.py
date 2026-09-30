"""Local IPC: reach a running Kairo from another process over a Unix socket.

Protocol: the client connects, sends one JSON object terminated by a newline,
reads one JSON object terminated by a newline, and the connection closes.

    {"op": "status"}
    {"op": "message", "text": "..."}
    {"op": "wake", "reason": "..."}        (reason optional)
    {"op": "stop"}

    -> {"ok": true, "result": {...}}  or  {"ok": false, "error": "..."}

Each operation maps onto an existing Runtime method; IPC adds no behaviour of
its own and can never execute actions.

Client: ``python -m kairo.ipc [--socket PATH] status|message TEXT|wake [REASON]|stop``
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import selectors
import socket
import stat
import sys
import threading
from pathlib import Path
from typing import Any

from kairo.runtime import Runtime

log = logging.getLogger("kairo.ipc")

DEFAULT_SOCKET = Path(os.environ.get("KAIRO_SOCKET", "var/kairo.sock"))
MAX_REQUEST = 64 * 1024
CLIENT_TIMEOUT = 5.0


class IPCError(RuntimeError):
    pass


class IPCServer:
    """Listens on a Unix socket inside the Kairo process and serves requests
    one at a time on a single background thread."""

    def __init__(self, runtime: Runtime, path: str | Path) -> None:
        self.runtime = runtime
        self.path = Path(path)
        self._sock: socket.socket | None = None
        self._identity: tuple[int, int] | None = None  # (st_dev, st_ino) of our socket file
        self._thread: threading.Thread | None = None
        self._stop_r, self._stop_w = os.pipe()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._clear_stale_socket()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.path))
            os.chmod(self.path, 0o600)  # before listen(): no one can connect yet
            sock.listen()
        except OSError:
            sock.close()
            raise
        st = os.stat(self.path)
        self._identity = (st.st_dev, st.st_ino)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, name="kairo-ipc", daemon=True)
        self._thread.start()
        log.info("listening on %s", self.path)

    def _clear_stale_socket(self) -> None:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(st.st_mode):
            raise IPCError(f"{self.path} exists and is not a socket; refusing to replace it")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(self.path))
        except ConnectionRefusedError:
            # Nobody is listening: left behind by a process that did not shut down.
            log.warning("removing stale socket %s", self.path)
            self.path.unlink()
            return
        except FileNotFoundError:
            return
        finally:
            probe.close()
        raise IPCError(f"another process is already listening on {self.path}")

    def close(self) -> None:
        """Stop accepting requests, finish the one in progress, remove the socket."""
        if self._thread is not None:
            os.write(self._stop_w, b"x")
            self._thread.join(CLIENT_TIMEOUT * 2)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
            # Only remove the file if it is still ours, never a newer server's.
            try:
                st = os.lstat(self.path)
                if (st.st_dev, st.st_ino) == self._identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
        for fd in (self._stop_r, self._stop_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._stop_r = self._stop_w = -1

    def __enter__(self) -> IPCServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- serving -----------------------------------------------------------

    def _serve(self) -> None:
        assert self._sock is not None
        with selectors.DefaultSelector() as sel:
            sel.register(self._sock, selectors.EVENT_READ)
            sel.register(self._stop_r, selectors.EVENT_READ)
            while True:
                for key, _ in sel.select():
                    if key.fileobj == self._stop_r:
                        return
                    try:
                        conn, _ = self._sock.accept()
                    except OSError:
                        continue
                    with conn:
                        self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(CLIENT_TIMEOUT)
            with conn.makefile("rb") as f:
                line = f.readline(MAX_REQUEST + 1)
            if not line:
                return  # client connected and went away
            response = self.dispatch(line)
            conn.sendall(json.dumps(response).encode() + b"\n")
        except OSError as exc:  # timeouts, resets: the client's problem, not Kairo's
            log.warning("ipc client error: %s", exc)

    def dispatch(self, line: bytes) -> dict[str, Any]:
        if len(line) > MAX_REQUEST:
            return _error("request too large")
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _error(f"invalid JSON: {exc}")
        if not isinstance(request, dict):
            return _error("request must be a JSON object")
        try:
            return {"ok": True, "result": self._perform(request)}
        except IPCError as exc:
            return _error(str(exc))
        except Exception as exc:
            log.exception("ipc request failed")
            return _error(f"internal error: {exc!r}")

    def _perform(self, request: dict[str, Any]) -> dict[str, Any]:
        match request.get("op"):
            case "status":
                return {**self.runtime.status(), "pid": os.getpid()}
            case "message":
                text = request.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise IPCError("'message' requires non-empty string 'text'")
                message = self.runtime.receive(text)
                return {"id": message.id, "state": self.runtime.state}
            case "wake":
                reason = request.get("reason", "wake requested over ipc")
                if not isinstance(reason, str):
                    raise IPCError("'reason' must be a string")
                accepted = self.runtime.request_wake(reason)
                return {"accepted": accepted, "state": self.runtime.state}
            case "stop":
                self.runtime.request_stop()
                return {"stopping": True}
            case op:
                raise IPCError(f"unknown op: {op!r}")


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


# -- client ----------------------------------------------------------------


def request(path: str | Path, payload: dict[str, Any],
            timeout: float = CLIENT_TIMEOUT) -> dict[str, Any]:
    """Send one request to a running Kairo and return its response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(payload).encode() + b"\n")
        with sock.makefile("rb") as f:
            line = f.readline()
    if not line:
        raise IPCError("connection closed without a response")
    return json.loads(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kairo.ipc",
                                     description="Talk to a running Kairo.")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET,
                        help="Kairo's Unix socket (default: $KAIRO_SOCKET or %(default)s)")
    ops = parser.add_subparsers(dest="op", required=True)
    ops.add_parser("status", help="show runtime state")
    ops.add_parser("message", help="send a human message").add_argument("text")
    ops.add_parser("wake", help="ask Kairo to wake and reassess").add_argument(
        "reason", nargs="?", default="wake requested over ipc")
    ops.add_parser("stop", help="ask Kairo to shut down gracefully")
    args = parser.parse_args(argv)

    payload: dict[str, Any] = {"op": args.op}
    if args.op == "message":
        payload["text"] = args.text
    elif args.op == "wake":
        payload["reason"] = args.reason

    try:
        response = request(args.socket, payload)
    except (OSError, IPCError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": f"cannot reach Kairo at {args.socket}: {exc}"}),
              file=sys.stderr)
        return 2
    print(json.dumps(response, indent=2))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
