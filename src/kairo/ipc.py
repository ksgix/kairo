"""Local IPC: the operator boundary of a running Kairo, over a Unix socket.

Protocol (version 2): the client connects, sends one JSON object terminated by a
newline, reads one JSON object terminated by a newline, and the connection closes.

    {"op": "status"}                                  live runtime status
    {"op": "situation"}                               what cognition would be shown now
    {"op": "chat", "limit": 50, "after": SEQ}         the conversation (both optional)
    {"op": "message", "text": "...", "id": "..."}     a human message (id optional:
                                                      makes delivery idempotent)
    {"op": "directives"}                              all directives
    {"op": "directive.add", "statement": "..."}       a new directive
    {"op": "directive.deactivate", "id": "..."}       stop pursuing a directive
    {"op": "directive.activate", "id": "..."}         pursue it again
    {"op": "wake", "reason": "..."}                   reassess now (reason optional)
    {"op": "stop"}                                    stop the runtime

    -> {"ok": true, "result": {...}}
    or {"ok": false, "error": "...", "code": CODE}

    CODE: malformed_request (not one JSON object, or too large), unknown_op,
    invalid_params (missing, unknown or mistyped fields), rejected (well-formed
    but not applicable to the current state), persistence_error,
    response_too_large, internal_error. A client that cannot connect, is refused
    by the socket's permissions or times out gets no response at all.

Every operation calls a Runtime method: reads are projections the live runtime
computes, inputs become persisted records that cognition sees, and wake/stop are
lifecycle requests. IPC holds no state of its own and can never execute an
action, change work or deployment, or reach a cognition provider.

Trust boundary: whoever can open the socket (mode 0600, the runtime's user) is
the operator, and the operator's messages can lead Kairo, which has broad
authority on this host, to do anything it can do. The socket is local only.

Client: ``python -m kairo.ipc [--socket PATH] [--json] COMMAND`` (see ``main``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import selectors
import socket
import stat
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from kairo.redact import redact
from kairo.runtime import CHAT_PAGE, OperatorRejected, Runtime

log = logging.getLogger("kairo.ipc")

DEFAULT_SOCKET = Path(os.environ.get("KAIRO_SOCKET", "var/kairo.sock"))
MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
CLIENT_TIMEOUT = 5.0
PROTOCOL = 2
CLIENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
WAKE_REASON = 300

# Each operation and the request fields it accepts besides "op".
OPS: dict[str, frozenset[str]] = {
    "status": frozenset(), "situation": frozenset(), "chat": frozenset({"limit", "after"}),
    "message": frozenset({"text", "id"}), "directives": frozenset(),
    "directive.add": frozenset({"statement"}), "directive.deactivate": frozenset({"id"}),
    "directive.activate": frozenset({"id"}), "wake": frozenset({"reason"}),
    "stop": frozenset(),
}


class IPCError(RuntimeError):
    def __init__(self, message: str, code: str = "invalid_params") -> None:
        super().__init__(message)
        self.code = code


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
            conn.sendall(self.respond(line))
        except OSError as exc:  # timeouts, resets: the client's problem, not Kairo's
            log.warning("ipc client error: %s", exc)

    def respond(self, line: bytes) -> bytes:
        """The encoded response line for one request line, bounded in size."""
        try:
            data = json.dumps(self.dispatch(line), ensure_ascii=False).encode()
        except (TypeError, ValueError):
            log.exception("ipc response could not be serialised")
            data = json.dumps(_error("response could not be serialised", "internal_error")).encode()
        if len(data) > MAX_RESPONSE:
            data = json.dumps(_error(f"response larger than {MAX_RESPONSE} bytes",
                                     "response_too_large")).encode()
        return data + b"\n"

    def dispatch(self, line: bytes) -> dict[str, Any]:
        if len(line) > MAX_REQUEST:
            return _error("request too large", "malformed_request")
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _error(f"invalid JSON: {exc}", "malformed_request")
        if not isinstance(request, dict):
            return _error("request must be a JSON object", "malformed_request")
        op = request.get("op")
        if not isinstance(op, str) or op not in OPS:
            return _error(f"unknown op: {op!r}; known: {sorted(OPS)}", "unknown_op")
        if unknown := set(request) - {"op"} - OPS[op]:
            return _error(f"{op} does not take {sorted(unknown)}", "invalid_params")
        try:
            return {"ok": True, "result": self._perform(op, request)}
        except IPCError as exc:
            return _error(str(exc), exc.code)
        except OperatorRejected as exc:
            return _error(str(exc), "rejected")
        except sqlite3.Error as exc:
            log.exception("ipc request failed in persistence")
            return _error(redact(f"persistence error: {exc}", limit=300), "persistence_error")
        except Exception as exc:  # never a traceback, never a secret
            log.exception("ipc request failed")
            return _error(redact(f"internal error ({type(exc).__name__})", limit=300),
                          "internal_error")

    def _perform(self, op: str, request: dict[str, Any]) -> dict[str, Any]:
        runtime = self.runtime
        match op:
            case "status":
                return {**runtime.status(), "pid": os.getpid(), "protocol": PROTOCOL,
                        "ops": sorted(OPS)}
            case "situation":
                return runtime.situation()
            case "chat":
                limit = _int(request, "limit", 50, 1, CHAT_PAGE)
                after = _int(request, "after", None, 0, None)
                return runtime.conversation(limit=limit, after=after)
            case "message":
                text = request.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise IPCError("'message' requires non-empty string 'text'")
                client_id = request.get("id")
                if client_id is not None and (not isinstance(client_id, str)
                                              or not CLIENT_ID.match(client_id)):
                    raise IPCError(f"'id' must match {CLIENT_ID.pattern}")
                message, duplicate = runtime.accept_message(text, client_id)
                return {"id": message.id, "state": runtime.state, "duplicate": duplicate}
            case "directives":
                return runtime.directive_list()
            case "directive.add":
                statement = request.get("statement")
                if not isinstance(statement, str):
                    raise IPCError("'directive.add' requires string 'statement'")
                return {"directive": _view(runtime.add_directive(statement)),
                        "state": runtime.state}
            case "directive.deactivate" | "directive.activate":
                directive_id = request.get("id")
                if not isinstance(directive_id, str) or not directive_id:
                    raise IPCError(f"'{op}' requires string 'id'")
                directive = runtime.set_directive_active(directive_id, op == "directive.activate")
                return {"directive": _view(directive), "state": runtime.state}
            case "wake":
                reason = request.get("reason", "wake requested over ipc")
                if not isinstance(reason, str) or not reason.strip() or len(reason) > WAKE_REASON:
                    raise IPCError(f"'reason' must be a non-empty string of at most {WAKE_REASON} "
                                   "characters")
                accepted = runtime.request_wake(reason)
                return {"accepted": accepted, "state": runtime.state}
            case "stop":
                runtime.request_stop("stop requested over ipc")
                return {"stopping": True}
        raise IPCError(f"unknown op: {op!r}", "unknown_op")  # unreachable: OPS is checked first


def _int(request: dict[str, Any], name: str, default: int | None, low: int,
         high: int | None) -> int | None:
    value = request.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < low or \
            (high is not None and value > high):
        bound = f"{low}-{high}" if high is not None else f">= {low}"
        raise IPCError(f"'{name}' must be an integer {bound}")
    return value


def _view(directive: Any) -> dict[str, Any]:
    from kairo.runtime import _directive_view
    return _directive_view(directive)


def _error(message: str, code: str) -> dict[str, Any]:
    return {"ok": False, "error": message, "code": code}


# -- client ----------------------------------------------------------------


def request(path: str | Path, payload: dict[str, Any],
            timeout: float = CLIENT_TIMEOUT) -> dict[str, Any]:
    """Send one request to a running Kairo and return its response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(payload).encode() + b"\n")
        with sock.makefile("rb") as f:
            line = f.readline(MAX_RESPONSE + 2)  # the server never sends more than MAX_RESPONSE
    if not line:
        raise IPCError("connection closed without a response")
    if not line.endswith(b"\n"):
        raise IPCError(f"response larger than {MAX_RESPONSE} bytes or incomplete")
    return json.loads(line)


def main(argv: list[str] | None = None) -> int:
    """The terminal operator client: one request per command, printed as the
    runtime answered it. It holds no state, reads no database and decides
    nothing; ``chat`` and ``directives`` are formatted for reading (--json for
    the raw response), everything else is printed as JSON."""
    parser = argparse.ArgumentParser(prog="python -m kairo.ipc",
                                     description="The operator's terminal client for a running Kairo.")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET,
                        help="Kairo's Unix socket (default: $KAIRO_SOCKET or %(default)s)")
    parser.add_argument("--json", action="store_true", help="print raw JSON responses")
    parser.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS",
                        help="how long to wait for Kairo's answer (default: %(default)s)")
    ops = parser.add_subparsers(dest="command", required=True)
    ops.add_parser("status", help="show the live runtime's state")
    ops.add_parser("situation", help="show what cognition would be shown now")
    chat = ops.add_parser("chat", help="show the conversation with Kairo")
    chat.add_argument("--limit", type=int, default=20, help="messages to show (default: 20)")
    chat.add_argument("--after", type=int, metavar="SEQ",
                      help="only messages after this sequence number")
    message = ops.add_parser("message", help="send Kairo a message (it answers in chat)")
    message.add_argument("text")
    message.add_argument("--id", help="client id: sending the same id again is a no-op")
    ops.add_parser("directives", help="list directives")
    directive = ops.add_parser("directive", help="add, deactivate or activate a directive")
    change = directive.add_subparsers(dest="change", required=True)
    change.add_parser("add", help="add a lasting area of responsibility").add_argument("statement")
    change.add_parser("deactivate", help="stop pursuing a directive").add_argument("id")
    change.add_parser("activate", help="pursue a directive again").add_argument("id")
    ops.add_parser("wake", help="ask Kairo to reassess now").add_argument(
        "reason", nargs="?", default="wake requested over ipc")
    ops.add_parser("stop", help="ask Kairo to stop gracefully")
    args = parser.parse_args(argv)

    payload: dict[str, Any] = {"op": args.command}
    match args.command:
        case "chat":
            payload["limit"] = args.limit
            if args.after is not None:
                payload["after"] = args.after
        case "message":
            payload["text"] = args.text
            if args.id:
                payload["id"] = args.id
        case "directive":
            payload["op"] = f"directive.{args.change}"
            payload.update({"statement": args.statement} if args.change == "add"
                           else {"id": args.id})
        case "wake":
            payload["reason"] = args.reason

    try:
        response = request(args.socket, payload, timeout=args.timeout)
    except (OSError, IPCError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": f"cannot reach Kairo at {args.socket}: {exc}"}),
              file=sys.stderr)
        return 2
    readable = not args.json and args.command in ("chat", "directives")
    if not response.get("ok"):
        if response.get("code") is None and str(response.get("error", "")).startswith("unknown op"):
            response["hint"] = "this Kairo predates protocol 2 (deploy a newer release)"
        print(json.dumps(response, indent=2))
        return 1
    if not readable:
        print(json.dumps(response, indent=2))
    elif args.command == "chat":
        print(_format_chat(response["result"]))
    else:
        print(_format_directives(response["result"]))
    return 0


def _when(at: Any) -> str:
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return "unknown time"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))


def _format_chat(result: dict[str, Any]) -> str:
    lines = ["(earlier messages exist: use --limit or --after)"] if result.get("more_before") else []
    for m in result.get("messages", []):
        if m.get("unreadable"):
            lines.append(f"#{m.get('seq')} [unreadable record]")
            continue
        text = str(m.get("text", "")).replace("\n", "\n    ")
        cut = f" [truncated from {m['truncated_from']} characters]" if m.get("truncated_from") else ""
        lines.append(f"#{m.get('seq')} {_when(m.get('at'))} {m.get('from')}: {text}{cut}")
    if not result.get("messages"):
        lines.append("(no messages)")
    if result.get("more_after"):
        lines.append("(later messages exist: use --after with the last sequence number)")
    return "\n".join(lines)


def _format_directives(result: dict[str, Any]) -> str:
    lines = [f"{d.get('id')}  {'active  ' if d.get('active') else 'inactive'}  "
             f"since {_when(d.get('created_at'))}  {d.get('statement')}"
             for d in result.get("directives", [])]
    if result.get("omitted_older"):
        lines.insert(0, f"({result['omitted_older']} older directives not shown)")
    return "\n".join(lines) or "(no directives)"


if __name__ == "__main__":
    raise SystemExit(main())
