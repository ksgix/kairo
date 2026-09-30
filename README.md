# Kairo

Kairo is a **persistent autonomous runtime**: a long-lived environment on a Linux
host in which cognition operates. The cognition itself (Claude, OpenAI, Gemini, …)
is external. Kairo is the runtime, not the model.

Kairo is **not** a chatbot, an agent manager, a multi-agent framework, an
orchestrator, a task manager or a project manager.

## Current minimal architecture

`src/kairo/`, standard library only:

| Module | Concept |
|---|---|
| `runtime.py` | `Runtime`: lifecycle states `created → awake ⇄ sleeping → stopped`; `cycle()` (observe → ask cognition → execute → verify → persist → maybe sleep) and `run_forever()` (continuous operation) |
| `environment.py` | `Environment`: `observe()` describes the host; `execute(Action)` runs structured actions (currently only `process.run` with an `argv` list, never shell text) |
| `cognition.py` | `CognitionProvider` protocol: `decide(Context) -> Decision`; `Context` (the runtime state gathered each cycle); the decision JSON schema and strict decision parsing |
| `situation.py` | The situation model: projects a `Context` into what cognition is shown (structured, bounded, redacted and deterministic) |
| `claude.py` | `ClaudeCognition`: the first real provider, which calls the locally installed Claude Code CLI |
| `redact.py` | Removes secret environment values and caps oversized strings before anything is persisted or sent to cognition |
| `actions.py` | `Action` (what was decided) and `ActionResult` (whether execution completed) |
| `verification.py` | `Outcome` = `success` / `failure` / `unverifiable`; `Verifier` protocol. If no verifier exists, the outcome is `unverifiable`, never success by default. |
| `memory.py` | `Memory`: a local SQLite document store (`kind`, `id`, JSON), plus a typed `Collection` view |
| `directives.py` | `Directive`: an ongoing reason Kairo operates, not a task |
| `todo.py` | `TodoItem`: operational notes. They do not drive the runtime; an empty list does not mean idle. |
| `chat.py` | `Message` / `Chat`: persisted human ⇄ Kairo messages. A message wakes a sleeping runtime. |
| `ipc.py` | Local Unix-socket IPC: lets other processes reach a running Kairo (`status`, `message`, `wake`, `stop`) |

## Continuous operation

`run_forever()` keeps Kairo alive until it is stopped:

- **Awake:** it runs cycles back to back for as long as cognition keeps working.
- **Sleep:** it sleeps when cognition returns `Decision(sleep=True)`, when there is no cognition provider, or when the provider raises. Sleep is a blocking wait on a condition variable, not polling, and the process stays alive. The to-do list is never a reason to sleep.
- **Wake:** a sleeping runtime wakes on
  - a human message (`receive`),
  - an explicit `request_wake(reason)`, or
  - its own reassessment deadline (`Decision.wake_after`, or the runtime's default `reassess_after`; `None` means sleep until woken).

  A message that arrives mid-cycle makes it reassess once more instead of sleeping. Every wake re-observes the environment, and cognition is told why it woke.
- **Stop:** `stop()`, SIGINT/SIGTERM or an IPC `stop` sets a flag. The loop takes on no new actions, finishes the current one, persists `stopped` and returns.
- **Restart:** the same Kairo comes back, with the same identity, directives, to-do, chat and action log. It records whether the previous process stopped cleanly, and cognition sees that as the wake reason. Executed actions are never replayed. An action that was running when the process died is marked `interrupted`, not re-run; cognition sees it in `recent_actions` and decides what to do.

Without a cognition provider, Kairo observes, sleeps with the reason `no cognition provider configured`, and wakes (and sleeps again) on messages or its reassessment interval.

## Cognition (Claude)

`--cognition claude` makes the local `claude` CLI Kairo's cognition. Each cycle is one headless call: `claude -p` with `--tools ""` (no tools), `--restricted`, `--strict-mcp-config` and `--no-session-persistence`.

- **Input:** the situation model (see below), sent on stdin.
- **Output:** Claude answers with JSON validated against a schema (`reason`, `actions`, `replies`, `sleep`, `wake_after`), which Kairo parses strictly into a `Decision`. Claude cannot run anything itself; it can only request `process.run` actions, which the runtime executes and records.
- **Failures:** a missing CLI, a non-zero exit, a timeout, empty or invalid output, or an invalid decision is recorded with a category. Kairo then sleeps until the next wake; the process keeps running.
- **Authentication:** whatever the local CLI is logged in with. Kairo stores no credentials.
- **Cycle log:** every cycle leaves a small `cycle` record with provider, result or failure category, requested action kinds, sleep choice, latency and cost.

## Situation model

`Runtime.context()` gathers runtime state; `situation.build_situation()` turns it into what cognition sees each cycle. The situation is derived, never stored, so the runtime's records stay the only source of truth. It is plain data and does not depend on any provider.

| Section | Contents |
|---|---|
| `kairo` | Identity, when Kairo was first created, and how many times it has started |
| `now` | Time, lifecycle state, wake reason, current process, the previous process (and whether it ended cleanly), the previous cycle |
| `environment` | A fresh host observation and what changed since the previous one |
| `directives` | Active directives with age and open to-do counts, plus the number inactive |
| `todo` | Open items and recently completed ones |
| `history` | Recent cycles (cognition's earlier assessment or the runtime's failure record), actions with a runtime-derived `state` (`verified_successful`, `executed_unverified`, `interrupted`, …) and output, and chat |
| `open_threads` | Derived, and informational only (not a task list): unanswered messages, failed or interrupted actions, results new since the last decision, a failed previous cycle, the open to-do count |
| `knowledge` | Empty for now: the place where a future knowledge store plugs in |
| `capabilities` | The actions the runtime can really execute, and whether each is verified automatically |
| `context` | Limits, redaction and truncation counts, what was trimmed, and anything unavailable |

- **Provenance:** every section names its source, and times carry `age_seconds`. Cognition's own earlier assessments are labelled as interpretation, not fact. Missing data is shown as missing (`"unknown"`, `null`) and never invented.
- **Bounds:** the most recent 10 cycles, 15 actions and 20 messages; 1,500 characters of output per action stream; 2,000 characters per string; and a 60,000-character total budget, met by dropping the oldest history first. Every omission is counted.
- **Robustness:** a corrupt record is reported as unavailable and does not stop the cycle.

To see exactly what cognition would be shown, without starting Kairo or calling a provider:
`python3 -m kairo --situation --db var/kairo.db`

Opt-in live smoke test (uses the model):
`KAIRO_LIVE_CLAUDE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p test_claude_live.py -v`

## Running (development)

```sh
cd /opt/kairo
export PYTHONPATH=src

# run continuously in the foreground (Ctrl-C also stops it cleanly)
python3 -m kairo --run --db var/kairo.db [--socket var/kairo.sock] [--reassess SECONDS] \
    [--cognition claude [--model MODEL] [--cognition-timeout SECONDS]]

# from another terminal, talk to the running process
python3 -m kairo.ipc status
python3 -m kairo.ipc message "hello Kairo"   # stored in chat; wakes Kairo
python3 -m kairo.ipc wake "manual wake"
python3 -m kairo.ipc stop                    # graceful shutdown

# single cycle: start, cycle, print status as JSON, stop
python3 -m kairo --db var/kairo.db
```

- The socket defaults to `$KAIRO_SOCKET`, or `var/kairo.sock` relative to the current directory. Run the client from the same directory or pass `--socket`.
- The socket is created with mode `0600` and removed on shutdown. A stale socket left by a crashed process is replaced; a socket another live process is listening on is never touched.
- The IPC protocol is one JSON object per line in each direction, one request per connection. For example, `{"op": "message", "text": "..."}` returns `{"ok": true, "result": {...}}` or `{"ok": false, "error": "..."}`.
- IPC only calls the existing `status`, `receive`, `request_wake` and `request_stop`; it cannot execute actions.
- `--reassess` defaults to 300 seconds; `0` means sleep until woken. Memory defaults to `$KAIRO_DB` or `~/.local/share/kairo/kairo.db`.
- With `pip install -e .`, `kairo` replaces `python3 -m kairo`.

## Tests

```sh
cd /opt/kairo
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests need no network, credentials or third-party packages.

## Deliberately not implemented yet

- Providers other than Claude, multiple providers, and delegation between providers
- Cognition cannot yet edit directives or to-do items; its only action is `process.run`
- A daemon/systemd service, cron or any scheduler; the only timing is one self-wake deadline
- Remote access of any kind: IPC is a local Unix socket, protected only by file permissions, with no authentication
- The full autonomous lifecycle (understand, prioritise, intend, strategise, learn, reassess)
- Action kinds beyond `process.run`, and any concrete verifiers
- Implementations/extensions (1C, trading, research, …) and their packaging
- Self-modification
- Dashboard, web UI, REST API
- Memory beyond plain documents (no embeddings, ranking, consolidation or migrations)
