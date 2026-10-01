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
| `work.py` | Ongoing work: pursuits carried across cycles, with states, strategy revisions and runtime-validated changes |
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

## Ongoing work

The layers, from most lasting to most momentary:

| Layer | What it is |
|---|---|
| Directive | A lasting area of responsibility, set by the operator |
| Work | A pursuit carried across cycles: objective, why it matters, strategy, understanding, next step, state. It may belong to a directive or not. |
| Todo | Operational notes, maintained by the operator. Not required for work. |
| Action | One runtime operation. When linked, it is an attempt at a work item. |
| Verification | Runtime evidence about an action's outcome |

- **States:** `active`, `waiting` (a condition or deadline), and `blocked` (a concrete obstacle) are open, and can move between each other or to a closed state. `completed` and `abandoned` are closed and final: closed work never changes, so a new reason means new work.
- **Cognition requests; the runtime decides.** Cognition sends `work` requests in its decision:
  - `create`, with a `ref` so this decision's actions can link to it;
  - `update` of understanding, next step or strategy;
  - `set_state`.

  The runtime validates each against the stored work and applies it or rejects it. Rejections appear in the next situation. The runtime assigns every id and timestamp, and rejects unknown ids, illegal transitions, changes to closed work, duplicate objectives, unknown directives and oversized text.
- **Completion needs evidence:** ids of the work's own attempts that succeeded. That means either verified successful, or, where no verifier exists, run with exit code 0. A non-zero exit without verification never counts. Each piece of evidence is recorded with its verification status and exit code.
- **Completion basis:** the runtime records it as `verified` (a verifier confirmed at least one cited attempt) or `unverified` (the runtime couldn't check the outcome; the completion is cognition's judgment of results that exited 0). Cognition can't set it. Records written before this field existed show `unknown`.
- **Retry versus new strategy:** changing the strategy gives it a new revision. Each linked action records the revision it belongs to, so a retry (same revision) is distinguishable from a changed strategy.
- **One source of truth:** each `work` record is the only authority for that item's current state, with a short log of its own changes. Attempts are not copied into it; they are the action records that point to the work.

## Failure and recovery

- **Failures are runtime facts. Diagnoses are cognition's interpretation.**
  - When an action can't be executed, the runtime records a `failure` kind taken from the actual exception: `not_found`, `permission_denied`, `timed_out`, `invalid_params`, `os_error` or `executor_error`.
  - A command that ran but exited non-zero has state `exited_nonzero`. The exit code is only a number; the runtime never infers a cause from it (exit 127 is not "command not found").
  - A verifier's rejection is `verification_failed`.
  - An `interrupted` action is neither a failure nor a success: its outcome and side effects are unknown.
- **Recovery facts per open work item**, derived from the action log, with no separate failure store:
  - the latest failure, with a bounded error or stderr excerpt;
  - `diagnosis_since_latest_failure`: whether the understanding changed after that failure;
  - attempts, failures and successes for each of the last 5 strategy revisions;
  - `repeated_identical_failures`.

  The work record keeps `understanding_at` and a `strategy_log` of the last 10 strategies.
- **No blind repetition:** an action linked to a work item is refused, not run, when it exactly repeats an attempt at that work that failed or was interrupted since the understanding last changed. The comparison uses kind and parameters in their stored, redacted form. There is no retry counter: updating the understanding, in the same decision if needed, allows the retry. Refusals appear in the next situation.
- **Waiting deadlines:** while sleeping, Kairo also wakes at the earliest future `waiting_until` of any open work, with the wake reason `wait elapsed for work <id>`. The work stays `waiting` until cognition decides otherwise.
- **Provider failures** are cycle-level. They are never attributed to a work item, and they leave work and its failure history untouched.

## Cognition providers

Kairo's cognition layer (`Cognition`, in `cognition.py`) asks providers for each cycle's decision. Providers are adapters (`claude.py`, test fakes; later e.g. Gemini). They don't know about each other, about fallback, or about Kairo's state, and they have no tools, so a failed call can't have changed anything.

- **Order:** `--cognition claude,claude@haiku`. The first is always asked first, every cycle. Later providers are asked only after a technical failure, and each provider is asked at most once per cycle. The first usable decision is used, even if it is "sleep" or "do nothing". There's no sticky fallback, health scoring, ranking or retrying.
- **Falls back:** `unavailable`, `timeout`, `process_failed`, `auth_failed`, `rate_limited`, `empty_output`, `invalid_output`, `invalid_decision` (an answer that breaks the decision contract is no decision) and `provider_error`.
- **Never falls back:** `model_error`, meaning the model ran but declined or failed to answer. Kairo doesn't route around refusals, and never compares the quality of two valid decisions.
- **All providers failed:** a cycle-level cognition failure, as before. Work is unchanged and no action runs.
- **Provenance:** each cycle records the provider that decided, why it was asked (`first_in_order` or `fallback_after:<provider>:<outcome>`) and every attempt. Cognition sees which provider it is (`now.cognition`) and which provider made each earlier decision (`history.cycles[*].provider`). `status` shows the configured order and the last provider used.
- **Options:** `--provider-opt claude@haiku.model=haiku`. `--model` and `--cognition-timeout` remain shortcuts for every Claude provider.
- **Credentials:** never accepted as options. Each provider declares where its credentials live (`secret_env`, `secret_files`).
  - Their values are always redacted.
  - A provider's subprocess gets only its own credential variables.
  - Actions (`process.run`) get none.
  - An action that names a known credential file is refused.
  - **Limit:** an action can still reach a credential file indirectly. Its token values are then redacted from output, but not if the output is transformed (for example, encoded). Real isolation would need actions to run as a different OS user than the one holding provider credentials.
- **Adding a provider** means adding an adapter (Kairo's instructions, situation and schema come from `instructions.cognition_request`) and one entry in `registry.PROVIDERS`. The runtime doesn't change.

## Situation model

`Runtime.context()` gathers runtime state; `situation.build_situation()` turns it into what cognition sees each cycle. The situation is derived, never stored, so the runtime's records stay the only source of truth. It is plain data and does not depend on any provider.

| Section | Contents |
|---|---|
| `kairo` | Identity, when Kairo was first created, and how many times it has started |
| `now` | Time, lifecycle state, wake reason, current process, the previous process (and whether it ended cleanly), the previous cycle |
| `environment` | A fresh host observation and what changed since the previous one |
| `directives` | Active directives with age and open to-do counts, plus the number inactive |
| `work` | Open work, each with its recent attempts by strategy revision and recent changes, plus recently closed work with reason or evidence |
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
- Cognition cannot yet edit directives or to-do items; its only runtime action is `process.run`, alongside work requests
- Work priority or focus, automatic resumption of elapsed waits, and automatic verifiers
- A daemon/systemd service, cron or any scheduler; the only timing is one self-wake deadline
- Remote access of any kind: IPC is a local Unix socket, protected only by file permissions, with no authentication
- The full autonomous lifecycle (understand, prioritise, intend, strategise, learn, reassess)
- Action kinds beyond `process.run`, and any concrete verifiers
- Implementations/extensions (1C, trading, research, …) and their packaging
- Self-modification
- Dashboard, web UI, REST API
- Memory beyond plain documents (no embeddings, ranking, consolidation or migrations)
