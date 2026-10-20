"""The Kairo dashboard: a browser interface over the operator boundary.

    browser ──HTTP (loopback)──> dashboard ──IPC (Unix socket)──> Kairo runtime

The dashboard is a translator, nothing more. Every API route is exactly one
operator IPC operation (see ROUTES); the runtime computes every answer and
applies every change. The dashboard holds no Kairo state, opens no database,
runs no cognition, executes nothing and has no timers: it does something only
when a browser asks. If it disappeared, Kairo would be the same runtime.

Security: it listens on a loopback address only (reach it over an SSH tunnel,
or behind a TLS reverse proxy). Whoever can use it is the operator, and the
operator's messages can lead Kairo, which has broad authority on its host, to
do anything it can do, so access is guarded:
- a login with the token in ``--token-file`` (a fresh random token each start,
  mode 0600) creates a session cookie (HttpOnly, SameSite=Strict);
- every state-changing request needs the session's CSRF token in a header;
- the Host header (and Origin, when sent) must name this dashboard, against
  DNS rebinding; request bodies are bounded and must be JSON objects.

Run: ``python -m kairo.dashboard --socket /var/lib/kairo/kairo.sock``.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import secrets
import re
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from kairo.ipc import DEFAULT_SOCKET, MAX_REQUEST, PROTOCOL, IPCError, request

log = logging.getLogger("kairo.dashboard")

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765
IPC_TIMEOUT = 30.0          # seconds to wait for the runtime's answer
MAX_BODY = MAX_REQUEST      # bytes of request body (the IPC request limit)
MAX_CONCURRENT = 16         # requests handled at once; more get 503
CONNECTION_TIMEOUT = 15     # seconds a client may take to send its request
SESSION_TTL = 12 * 3600     # seconds a login lasts
SESSION_COOKIE = "kairo_session"
CSRF_HEADER = "X-Kairo-CSRF"

# The whole API. Each route is one operator IPC operation; the body (POST) or
# query (GET) becomes that operation's fields, and the runtime validates them.
ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/status"): "status",
    ("GET", "/api/situation"): "situation",
    ("GET", "/api/metrics"): "metrics",
    ("GET", "/api/activity"): "activity",
    ("GET", "/api/chat"): "chat",
    ("GET", "/api/directives"): "directives",
    ("POST", "/api/message"): "message",
    ("POST", "/api/directives"): "directive.add",
    ("POST", "/api/directives/activate"): "directive.activate",
    ("POST", "/api/directives/deactivate"): "directive.deactivate",
    ("POST", "/api/wake"): "wake",
    ("POST", "/api/stop"): "stop",
}
# Not an IPC operation: the host is asked, because a stopped Kairo cannot be.
START_PATH = "/api/start"
START_TIMEOUT = 30.0  # seconds to wait for the service manager
# A systemd unit name the dashboard may be configured with: a plain name, no
# path, no shell metacharacter, so the fixed command cannot become another one.
# Begins with a letter or digit, so it can never be read as an option.
UNIT_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9:_.-]{0,95}\Z")
# GET query fields, passed as integers.
QUERY_INTS = {"chat": ("limit", "after"), "activity": ("limit", "before")}

# How IPC outcomes become HTTP statuses. Kairo's own error code is passed through.
HTTP_STATUS = {
    "malformed_request": 400, "invalid_params": 400, "rejected": 409, "unknown_op": 501,
    "persistence_error": 503, "response_too_large": 502, "internal_error": 502,
    "unreachable": 503, "permission_denied": 503, "timeout": 504, "bad_response": 502,
    # Starting Kairo (the host, not Kairo): not allowed here, or the attempt failed.
    "not_configured": 501, "start_failed": 502,
}

ASSETS = {
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                "connect-src 'self'; img-src 'self'; form-action 'self'; "
                                "base-uri 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Not no-referrer: under it a browser sends "Origin: null" even on the page's own
    # POSTs (login, logout, every action), which the Origin check must refuse.
    # same-origin keeps the real Origin on our own requests, none for other sites.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def _service_unit(name: str) -> str:
    """Validate the unit the dashboard may start. Rejected at startup, not at
    request time: an operator's typo should stop the dashboard, not surprise
    them later, and nothing a browser sends ever reaches this."""
    if not UNIT_NAME.match(name):
        raise ValueError(f"--start-unit {name!r} is not a plain systemd unit name")
    return name


def write_token(path: Path) -> str:
    """A fresh random login token, written to ``path`` readable by this user
    only. Refuses to follow a symlink. The token is never logged or served."""
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (token + "\n").encode())
    finally:
        os.close(fd)
    return token


class Sessions:
    """Logged-in browsers: session id -> (CSRF token, expiry). In memory only:
    restarting the dashboard logs everyone out; it is not Kairo state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, float]] = {}

    def create(self) -> tuple[str, str]:
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if v[1] > now}
            self._items[sid] = (csrf, now + SESSION_TTL)
        return sid, csrf

    def csrf(self, sid: str | None) -> str | None:
        if not sid:
            return None
        with self._lock:
            item = self._items.get(sid)
            if item is None or item[1] <= time.monotonic():
                self._items.pop(sid, None)
                return None
            return item[0]

    def drop(self, sid: str | None) -> None:
        with self._lock:
            self._items.pop(sid or "", None)


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], socket_path: str | Path, token: str,
                 allowed_hosts: list[str] | None = None, secure_cookie: bool = False,
                 start_unit: str | None = None) -> None:
        if not _loopback(address[0]):
            raise ValueError(f"refusing to listen on {address[0]!r}: the dashboard binds a "
                             "loopback address only (use an SSH tunnel or a TLS reverse proxy)")
        super().__init__(address, Handler)
        self.socket_path = str(socket_path)
        self.token = token
        # The one service the dashboard may start, when the operator allowed it.
        # Without it there is no way to start Kairo from the browser at all.
        self.start_unit = _service_unit(start_unit) if start_unit else None
        self.sessions = Sessions()
        self.started_at = time.time()
        self.secure_cookie = secure_cookie
        port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}",
                              *(allowed_hosts or [])}
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)

    def ipc(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """One operator IPC request, mapped to (HTTP status, JSON body). A runtime
        that cannot be reached is reported as unreachable, never as a state."""
        try:
            response = request(self.socket_path, payload, timeout=IPC_TIMEOUT)
        except (TimeoutError, socket.timeout):
            return _failure("timeout", "Kairo did not answer in time")
        except PermissionError:
            return _failure("permission_denied", "no permission to use Kairo's socket")
        except (FileNotFoundError, ConnectionRefusedError):
            return _failure("unreachable", "Kairo is not reachable (stopped, restarting or "
                                           "not running on this socket)")
        except OSError as exc:
            return _failure("unreachable", f"Kairo is not reachable ({type(exc).__name__})")
        except (IPCError, ValueError):
            return _failure("bad_response", "Kairo's answer could not be read")
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            return _failure("bad_response", "Kairo's answer was not a valid response")
        if response["ok"]:
            return 200, response
        code = response.get("code")
        if not isinstance(code, str):  # a protocol 1 runtime has no error codes
            code = ("unknown_op" if str(response.get("error", "")).startswith("unknown op")
                    else "internal_error")
        return HTTP_STATUS.get(code, 502), {"ok": False, "error": str(response.get("error")),
                                            "code": code}

    def start_kairo(self) -> tuple[int, dict[str, Any]]:
        """Ask the service manager to start Kairo. Not an IPC operation: a
        stopped Kairo has no socket, so this is the one thing the dashboard
        asks of the host instead of of Kairo.

        It runs exactly ``sudo -n systemctl start <unit>`` and nothing the
        request can influence: the unit is fixed at startup, the browser sends
        no arguments, and without the operator's sudo rule the command simply
        fails. Starting an already-running service is what systemd calls a
        no-op, so a double click cannot restart or disturb a working Kairo."""
        if self.start_unit is None:
            return _failure("not_configured",
                            "this dashboard cannot start Kairo (no --start-unit); "
                            "start it on the server with: systemctl start kairo")
        argv = ["sudo", "-n", "systemctl", "start", self.start_unit]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=START_TIMEOUT,
                                  check=False, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return _failure("start_failed", "sudo or systemctl is not installed")
        except (subprocess.TimeoutExpired, OSError) as exc:
            return _failure("start_failed", f"could not start {self.start_unit} "
                                            f"({type(exc).__name__})")
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            log.warning("starting %s failed (exit %s)", self.start_unit, done.returncode)
            return _failure("start_failed",
                            f"starting {self.start_unit} failed: "
                            f"{detail[-1][:200] if detail else f'exit {done.returncode}'}")
        log.info("started %s on the operator's request", self.start_unit)
        return 200, {"ok": True, "result": {"started": self.start_unit}}


class Handler(BaseHTTPRequestHandler):
    server: Dashboard
    timeout = CONNECTION_TIMEOUT
    server_version = "kairo-dashboard"
    sys_version = ""

    # -- dispatch ------------------------------------------------------------

    def do_GET(self) -> None:
        self._guarded(self._get)

    def do_POST(self) -> None:
        self._guarded(self._post)

    def _guarded(self, handle: Any) -> None:
        if not self.server.slots.acquire(timeout=5):
            return self._json(503, {"ok": False, "error": "dashboard busy", "code": "busy"})
        try:
            if self.headers.get("Host") not in self.server.allowed_hosts:
                return self._json(421, {"ok": False, "error": "unexpected Host",
                                        "code": "bad_host"})
            handle(urlsplit(self.path))
        except Exception:  # never a traceback to the browser
            log.exception("dashboard request failed")
            self._json(500, {"ok": False, "error": "dashboard error", "code": "dashboard_error"})
        finally:
            self.server.slots.release()

    def _get(self, url: Any) -> None:
        path = url.path
        if path in ASSETS:
            name, ctype = ASSETS[path]
            return self._send(200, (STATIC / name).read_bytes(), ctype)
        if path == "/login":
            return self._page("login.html", failed=url.query == "failed")
        session_csrf = self._session_csrf()
        if path == "/":
            if session_csrf is None:
                return self._redirect("/login")
            return self._page("index.html", csrf=session_csrf)
        if session_csrf is None:
            return self._json(401, {"ok": False, "error": "log in first", "code": "unauthorized"})
        if path == "/api/dashboard":  # the adapter's own facts (not Kairo's)
            return self._json(200, {"ok": True, "result": {
                "socket": self.server.socket_path, "protocol_expected": PROTOCOL,
                "routes": sorted(f"{m} {p}" for m, p in ROUTES),
                # Which release's dashboard this is: it keeps serving the release it
                # started from, so after a deployment it can be older than Kairo.
                "revision": own_revision(), "started_at": self.server.started_at,
                # Whether this dashboard was allowed to start Kairo (see start_kairo).
                "can_start": self.server.start_unit is not None}})
        op = ROUTES.get(("GET", path))
        if op is None:
            return self._json(404, {"ok": False, "error": "not found", "code": "not_found"})
        payload: dict[str, Any] = {"op": op}
        query = parse_qs(url.query)
        for name in QUERY_INTS.get(op, ()):
            if name in query:
                value = query[name][-1]
                if not value.isdigit() or len(value) > 12:
                    return self._json(400, {"ok": False, "error": f"'{name}' must be a "
                                            "non-negative integer", "code": "invalid_params"})
                payload[name] = int(value)
        self._json(*self.server.ipc(payload))

    def _post(self, url: Any) -> None:
        path = url.path
        origin = self.headers.get("Origin")
        if origin is not None and urlsplit(origin).netloc not in self.server.allowed_hosts:
            return self._json(403, {"ok": False, "error": "cross-origin request refused",
                                    "code": "forbidden"})
        body = self._body()
        if body is None:
            return
        if path == "/login":
            return self._login(body)
        if path == "/logout":
            self.server.sessions.drop(self._cookie())
            return self._redirect("/login", clear_cookie=True)
        session_csrf = self._session_csrf()
        if session_csrf is None:
            return self._json(401, {"ok": False, "error": "log in first", "code": "unauthorized"})
        if not hmac.compare_digest(self.headers.get(CSRF_HEADER, ""), session_csrf):
            return self._json(403, {"ok": False, "error": "missing or wrong CSRF token",
                                    "code": "forbidden"})
        if path == START_PATH:  # the host, not Kairo: see Dashboard.start_kairo
            return self._json(*self.server.start_kairo())
        op = ROUTES.get(("POST", path))
        if op is None:
            return self._json(404, {"ok": False, "error": "not found", "code": "not_found"})
        if "json" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"ok": False, "error": "send JSON", "code": "malformed_request"})
        try:
            fields = json.loads(body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"ok": False, "error": "invalid JSON",
                                    "code": "malformed_request"})
        if not isinstance(fields, dict) or "op" in fields:
            return self._json(400, {"ok": False, "error": "send one JSON object (without 'op')",
                                    "code": "malformed_request"})
        self._json(*self.server.ipc({"op": op, **fields}))

    # -- login -------------------------------------------------------------

    def _login(self, body: bytes) -> None:
        token = parse_qs(body.decode("utf-8", errors="replace")).get("token", [""])[-1].strip()
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            time.sleep(1)  # slows guessing; the token is 256 random bits anyway
            log.warning("failed dashboard login")
            return self._redirect("/login?failed")
        sid, _ = self.server.sessions.create()
        cookie = f"{SESSION_COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}"
        if self.server.secure_cookie:
            cookie += "; Secure"
        self._redirect("/", cookie=cookie)

    # -- helpers -----------------------------------------------------------

    def _body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            self._json(411, {"ok": False, "error": "send a Content-Length",
                             "code": "malformed_request"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._json(413, {"ok": False, "error": f"request body over {MAX_BODY} bytes",
                             "code": "malformed_request"})
            return None
        return self.rfile.read(length)

    def _cookie(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return None

    def _session_csrf(self) -> str | None:
        return self.server.sessions.csrf(self._cookie())

    def _page(self, name: str, csrf: str = "", failed: bool = False) -> None:
        html = (STATIC / name).read_text()
        html = html.replace("{{CSRF}}", csrf).replace(
            "{{FAILED}}", "That token was not accepted." if failed else "")
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def _redirect(self, location: str, cookie: str | None = None,
                  clear_cookie: bool = False) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if clear_cookie:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; "
                                           "Path=/; Max-Age=0")
        self._headers(0)

    def _json(self, status: int, body: dict[str, Any]) -> None:
        self._send(status, json.dumps(body, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _send(self, status: int, data: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self._headers(len(data))
        self.wfile.write(data)

    def _headers(self, length: int) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # path only, never a query or body
        log.info("%s %s", self.command, urlsplit(self.path).path)


def own_revision() -> str | None:
    """The release this dashboard's code was loaded from: the name of its release
    directory when that is a commit id (the deployed layout), otherwise None."""
    name = STATIC.parents[3].name
    return name if len(name) == 40 and all(c in "0123456789abcdef" for c in name) else None


def _failure(code: str, message: str) -> tuple[int, dict[str, Any]]:
    return HTTP_STATUS[code], {"ok": False, "error": message, "code": code}


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kairo.dashboard",
                                     description="A browser interface to a running Kairo.")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET,
                        help="Kairo's Unix socket (default: $KAIRO_SOCKET or %(default)s)")
    parser.add_argument("--host", default="127.0.0.1", help="loopback address to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token-file", type=Path,
                        help="where to write the login token (default: dashboard.token next "
                             "to the socket)")
    parser.add_argument("--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
                        help="another Host name the dashboard is reached as (e.g. behind a "
                             "reverse proxy); repeatable")
    parser.add_argument("--secure-cookie", action="store_true",
                        help="mark the session cookie Secure (when served over HTTPS)")
    parser.add_argument("--start-unit", metavar="UNIT",
                        help="the systemd unit this dashboard may start when Kairo is not "
                             "reachable, e.g. kairo.service; needs a sudo rule allowing "
                             "exactly 'systemctl start UNIT'. Without it the dashboard "
                             "can never start Kairo")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    token_file = args.token_file or args.socket.parent / "dashboard.token"
    try:
        token = write_token(token_file)
        server = Dashboard((args.host, args.port), args.socket, token, args.allowed_host,
                           args.secure_cookie, args.start_unit)
    except (ValueError, OSError) as exc:
        print(f"kairo dashboard: {exc}", file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    log.info("dashboard on http://%s:%s/ for Kairo at %s; login token in %s",
             "localhost" if host in ("127.0.0.1", "::1") else host, port, args.socket, token_file)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
