# Dashboard

A browser interface to the running Kairo, for the operator. It is a translator, not a second system:

```
browser ──HTTP, loopback only──> dashboard ──operator IPC (Unix socket)──> Kairo runtime ──> SQLite
         login + CSRF            (kairo.dashboard)   one route = one IPC operation          (Kairo's only)
```

The dashboard holds no Kairo state. It opens no database, runs no cognition, executes nothing and has no timers or background work: it does something only when a browser asks. Every answer is computed by the live runtime, and every change is applied by it, exactly as for the terminal client. If the dashboard stopped, Kairo would be the same runtime. Its only state is the login sessions in memory, and its only file is its login token.

**Run it** (as the account that owns Kairo's socket):

```sh
PYTHONPATH=src python3 -m kairo.dashboard --socket var/kairo.sock      # http://localhost:8765/
```

- At each start it writes a fresh random login token to `--token-file` (default: `dashboard.token` next to the socket, mode 0600) and logs only where the token is. Read it with `cat`, and paste it into the login page.
- `--host` must be a loopback address; anything else is refused. `--port` defaults to 8765.
- From another machine, use an SSH tunnel to the same port: `ssh -L 8765:127.0.0.1:8765 host`, then open `http://localhost:8765/`. If the local port differs, pass `--allowed-host localhost:PORT`, because the dashboard serves only the host names it knows.

**Production** (not installed automatically): `deploy/kairo-dashboard.service` is a template like `kairo.service`.

```sh
KAIRO_USER=$(id -un)   # the account Kairo runs as
sed -e "s|@KAIRO_USER@|$KAIRO_USER|g" deploy/kairo-dashboard.service \
    | sudo install -m 0644 /dev/stdin /etc/systemd/system/kairo-dashboard.service
sudo systemctl daemon-reload && sudo systemctl enable --now kairo-dashboard
```

- It runs as Kairo's account, from the current release, on `127.0.0.1:8765`, with its token in `/var/lib/kairo/dashboard.token`.
- It is independent of `kairo.service`: it never starts, stops or restarts Kairo, and Kairo does not depend on it. While Kairo is stopped or restarting, the dashboard shows it as unreachable.
- After a deployment it still serves the release it started from. Restart it to serve the new one; a restart logs everyone out.
- Exposing it beyond the host needs a TLS reverse proxy in front of it: pass `--allowed-host NAME` and `--secure-cookie`.

**Routes.** Each route is one operator operation; the runtime validates every field.

| Route | Operation |
|---|---|
| `GET /api/status` | `status` |
| `GET /api/situation` | `situation` |
| `GET /api/chat?limit=N&after=SEQ` | `chat` |
| `GET /api/directives` | `directives` |
| `POST /api/message` `{"text", "id"}` | `message` |
| `POST /api/directives` `{"statement", "description"}` | `directive.add` |
| `POST /api/directives/deactivate` / `activate` `{"id"}` | `directive.deactivate` / `directive.activate` |
| `POST /api/wake` `{"reason"}` | `wake` |
| `POST /api/stop` | `stop` |

- Responses are Kairo's own: `{"ok": true, "result": ...}`, or `{"ok": false, "error", "code"}` with the IPC code.
- The HTTP status follows the code: `invalid_params` and `malformed_request` are 400, `rejected` is 409, and `unknown_op` (an older runtime) is 501.
- Codes about reaching Kairo are the dashboard's own:
  - `unreachable` and `permission_denied` are 503;
  - `timeout` is 504;
  - `bad_response`, `response_too_large` and `internal_error` are 502.
- `GET /api/dashboard` describes the adapter itself (socket, expected protocol, routes).

**Views.** All views come from these reads:

- Overview: state, counts, last cognition result, what needs attention, recent activity.
- Chat: send messages; a retry reuses the message id, so Kairo stores the message once.
- Directives: each as one of Kairo's responsibilities (statement, description, state, origin, linked open work, the implementations naming it, history); add with a statement and a description; deactivate, activate.
- Work: facts, cognition's account, attempts, recovery, unresolved external operations.
- Activity: cycles, actions, deployments.
- Context: the situation, section by section, as cognition sees it.
- System: host, release, capabilities, implementations, wake and stop.

Every item is marked as one of four kinds:

- **runtime fact**: recorded or derived by the runtime;
- **cognition**: cognition's own words, such as objectives, strategies, assessments and replies; not verified;
- **untrusted content**: what a program or external system printed; shown only as text, in a marked box;
- **operator**: human input.

Polling reads only, every 5–15 s, pauses while the tab is hidden, and backs off up to 60 s on errors. Loading or refreshing a page never wakes Kairo: only Wake, a message or a directive change does, as over IPC.

**Security:**

- Whoever can log in is the operator, with the operator's full authority.
- Access is guarded in three ways:
  - it listens on loopback only, and accepts only its own `Host` names (against DNS rebinding);
  - it requires the token login. The session cookie is `HttpOnly` and `SameSite=Strict`, lasts 12 h, and is held in memory;
  - every state change needs the session's CSRF token in a header and a same-origin `Origin`.
- Bodies must be JSON objects of at most 64 KiB. A body cannot name the operation, and chunked bodies are refused.
- A strict Content-Security-Policy applies: no inline script and no other origins.
- The browser code inserts Kairo's text as text only, never as HTML.
- Secrets are redacted by Kairo before anything leaves the socket. The token is never served or logged, and request logs carry only the method and path.
- Requests are bounded: 16 at once. Kairo still answers them one at a time.

**Not available, by design:** no shell, process or action execution; no implementation calls; no SQL or database access; no file access; no Work editing; no deployment or configuration; no arbitrary HTTP or debug endpoints. Anything else is asked of Kairo in a message, and Kairo does it as an ordinary, recorded and verified action, or not at all.

