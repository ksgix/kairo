# Architecture

How Kairo works: continuous operation, cognition, ongoing work, failure and recovery,
implementations and the situation model. Back to the [README](../README.md); see also
[Self-maintenance](self-maintenance.md).

## Current minimal architecture

`src/kairo/`, standard library only:

| Module | Concept |
|---|---|
| `runtime.py` | `Runtime`: lifecycle states `created → awake ⇄ sleeping → stopped`; `cycle()` (observe → ask cognition → execute → verify → persist → maybe sleep) and `run_forever()` (continuous operation) |
| `environment.py` | `Environment`: `observe()` describes the host; `execute(Action)` runs structured actions (`process.run` with an `argv` list, never shell text; implementation tools; `runtime.deploy` when configured) |
| `cognition.py` | `CognitionProvider` protocol: `decide(Context) -> Decision`; `Context` (the runtime state gathered each cycle); the decision JSON schema and strict decision parsing |
| `situation.py` | The situation model: projects a `Context` into what cognition is shown (structured, bounded, redacted and deterministic) |
| `claude.py` | `ClaudeCognition`: the first real provider, which calls the locally installed Claude Code CLI |
| `redact.py` | Removes secret environment values and caps oversized strings before anything is persisted or sent to cognition |
| `actions.py` | `Action` (what was decided) and `ActionResult` (whether execution completed) |
| `verification.py` | `Outcome` = `success` / `failure` / `unverifiable`; `Verifier` protocol. If no verifier exists, the outcome is `unverifiable`, never success by default. |
| `memory.py` | `Memory`: a local SQLite document store (`kind`, `id`, JSON), plus a typed `Collection` view |
| `directives.py` | `Directive`: an ongoing reason Kairo operates, not a task |
| `work.py` | Ongoing work: pursuits carried across cycles, with states, strategy revisions and runtime-validated changes |
| `chat.py` | `Message` / `Chat`: persisted human ⇄ Kairo messages. A message wakes a sleeping runtime. |
| `implementations.py` | Implementation packages: manifest validation, content digest, the derived catalog |
| `ipc.py` | The operator boundary: local Unix-socket IPC to the live runtime (reads, human input, wake, stop) and the terminal client |
| `dashboard/` | The dashboard: a loopback HTTP adapter that turns browser requests into operator IPC requests (no state, no database, no execution) |
| `deploy.py` | Self-deployment: immutable releases built from commits, the `runtime.deploy` action (preflight, snapshot, switch, restart) |

## Continuous operation

`run_forever()` keeps Kairo alive until it is stopped:

- **Awake:** it runs cycles back to back for as long as cognition keeps working.
- **Sleep:** it sleeps when cognition returns `Decision(sleep=True)`, when there is no cognition provider, or when the provider raises. Sleep is a blocking wait on a condition variable, not polling, and the process stays alive.
- **Wake:** a sleeping runtime wakes on
  - a human message (`receive`),
  - an explicit `request_wake(reason)`, or
  - its own reassessment deadline (`Decision.wake_after`, or the runtime's default `reassess_after`; `None` means sleep until woken).

  A message that arrives mid-cycle makes it reassess once more instead of sleeping. Every wake re-observes the environment, and cognition is told why it woke.
- **Stop:** `stop()`, SIGINT/SIGTERM or an IPC `stop` sets a flag. The loop takes on no new actions, finishes the current one, persists `stopped` and returns.
- **Restart:** the same Kairo comes back, with the same identity, directives, work, chat and action log. It records whether the previous process stopped cleanly, and cognition sees that as the wake reason. Executed actions are never replayed. An action that was running when the process died is marked `interrupted`, not re-run; cognition sees it in `recent_actions` and decides what to do.

Without a cognition provider, Kairo observes, sleeps with the reason `no cognition provider configured`, and wakes (and sleeps again) on messages or its reassessment interval.

## Cognition (Claude)

`--cognition claude` makes the local `claude` CLI Kairo's cognition. Each cycle is one headless call: `claude -p` with `--tools ""` (no tools), `--restricted`, `--strict-mcp-config` and `--no-session-persistence`.

- **Input:** the situation model (see below), sent on stdin.
- **Output:** Claude answers with JSON validated against a schema (`reason`, `actions`, `replies`, `sleep`, `wake_after`), which Kairo parses strictly into a `Decision`. Claude cannot run anything itself; it can only request structured actions from the runtime's capabilities, which the runtime executes and records.
- **Failures:** a missing CLI, a non-zero exit, a timeout, empty or invalid output, or an invalid decision is recorded with a category. Kairo then sleeps until the next wake; the process keeps running.
- **Authentication:** whatever the local CLI is logged in with. Kairo stores no credentials.
- **Cycle log:** every cycle leaves a small `cycle` record with provider, result or failure category, requested action kinds, sleep choice, latency and cost.

## Ongoing work

The layers, from most lasting to most momentary:

| Layer | What it is |
|---|---|
| Directive | A lasting area of responsibility, set by the operator: a statement of the purpose and a description of what it covers. Why Kairo acts; never a task list. |
| Work | A pursuit carried across cycles: objective, why it matters, strategy, understanding, next step, state. It may belong to a directive or not. |
| Action | One runtime operation. When linked, it is an attempt at a work item. |
| Verification | Runtime evidence about an action's outcome |

- **States:** `active`, `waiting` (a condition or deadline), and `blocked` (a concrete obstacle) are open, and can move between each other or to a closed state. `completed` and `abandoned` are closed and final: closed work never changes, so a new reason means new work.
- **Cognition requests; the runtime decides.** Cognition sends `work` requests in its decision:
  - `create`, with a `ref` so this decision's actions can link to it;
  - `update` of understanding, next step or strategy;
  - `set_state`.

  The runtime validates each against the stored work and applies it or rejects it. Rejections appear in the next situation. The runtime assigns every id and timestamp, and rejects unknown ids, illegal transitions, changes to closed work, duplicate objectives, unknown directives and oversized text. Text limits, in characters: objective 600, why 1,000, strategy 2,000, next step 1,000, reason 1,000, understanding 10,000. Longer text is rejected whole, never cut.
- **Completion needs evidence:** ids of recorded actions that succeeded, meaning either verified successful or, where no verifier exists, run with exit code 0. A non-zero exit without verification never counts. Evidence may be the work's own attempts or any other action, linked to other work or to none. Each piece of evidence is recorded with its verification status, exit code and `own_attempt`. Evidence sent with any other state change is ignored, and the change is applied.
- **Completion basis:** the runtime records it as `verified` (a verifier confirmed at least one cited attempt) or `unverified` (the runtime couldn't check the outcome; the completion is cognition's judgment of results that exited 0). Cognition can't set it. Records written before this field existed show `unknown`.
- **Retry versus new strategy:** changing the strategy gives it a new revision. Each linked action records the revision it belongs to, so a retry (same revision) is distinguishable from a changed strategy.
- **One source of truth:** each `work` record is the only authority for that item's current state, with a log of its last 40 changes. Attempts are not copied into it; they are the action records that point to the work.
- **Understanding (up to 10,000 characters)** is cognition's current synthesis of the work: what the problem is, what has been found, which approaches were tried and why they failed, constraints, what remains uncertain. It is replaced as a whole on each update: one current state, not a log, a transcript or stored reasoning. It stays labelled as interpretation; the facts stay in the runtime's records. A longer update is rejected whole, never cut.

## Failure and recovery

- **Failures are runtime facts. Diagnoses are cognition's interpretation.**
  - When an action can't be executed, the runtime records a `failure` kind taken from the actual exception: `not_found`, `permission_denied`, `timed_out`, `invalid_params`, `os_error` or `executor_error`.
  - A command that ran but exited non-zero has state `exited_nonzero`. The exit code is only a number; the runtime never infers a cause from it (exit 127 is not "command not found").
  - A verifier's rejection is `verification_failed`.
  - An `interrupted` action is neither a failure nor a success: its outcome and side effects are unknown.
- **Recovery facts per open work item**, derived from the action log, with no separate failure store:
  - the latest failure, with a bounded error or stderr excerpt;
  - `diagnosis_since_latest_failure`: whether the understanding changed after that failure;
  - each of the last 5 strategy revisions, tried or not, with when it was adopted and its attempts, failures and successes;
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

## Implementations

An implementation is a local package that gives Kairo capability in a domain: guidance, tools, checks and any supporting files. It is **inert**. Nothing in it runs because it exists, and it has no goals, work, memory, hooks, schedules or background processes. **Kairo decides; implementations enable.**

```
<implementations-dir>/            default: <directory of --db>/implementations
  onec/                           directory name == manifest id
    implementation.json           the only required file
    GUIDANCE.md, tools/, checks/  optional, any layout
```

- **Manifest (JSON):**
  - required: `"kairo_implementation": 1`, `id`, `description`;
  - optional: `version` (a label only), `guidance`, `requires.commands`, `env` (names, each `{"secret": bool}`), `tools`, `checks`.
  - Unknown fields are rejected. Every path must stay inside the package, including through symlinks. The manifest declares what a package offers and needs; it grants nothing.
- **Enablement** is configuration: `--implementations onec,web`, or `all` (default `none`), plus `--implementations-dir`. The catalog is derived from disk each time (`available`, `disabled`, `unmet_requirements`, `broken`, `missing`). There is no registry. A package is `available` when it is enabled and its requirements are met; it is not tied to any directive, so it can be written in advance and moved between hosts.
- **Tools are ordinary actions.** Each tool becomes `impl.<id>.<tool>`, and declared checks become `impl.<id>.check` (with `{"name": ...}`). They're listed only while the package is available, and pass through the same parsing, repetition rule, execution, verification and logging as every other action.
  - **Parameters:** validated against a strict JSON Schema subset (an object of string, integer, number, boolean or string-array properties, enums, `required`, `additionalProperties: false`), then passed as JSON on stdin, never on the command line.
  - **Execution:** from the package directory, with no shell, a timeout, and capped, redacted output.
  - **Verification:** a tool's optional `verify` command reads `{"params", "result"}`; exit 0 means success, 1 failure, anything else unverifiable. A check's exit code is its verdict.
- **Provenance:** every implementation action records `{"implementation": {"id", "digest"}}`. The digest is a SHA-256 of the package content, so history shows exactly which content ran, even after the package changes or is removed.
- **Guidance** (`GUIDANCE.md`) is shown to cognition as labelled, untrusted data in `capabilities.implementations`. Entries are in id order, at most 30, with at most 2,000 characters of guidance each and 8,000 in total; omissions are marked. It is never part of Kairo's instructions and cannot change Kairo's rules or grant capabilities.
- **Secrets:** declared secret variables reach only that package's own tools. Providers' credentials never reach a package, a package can't claim them, and no package ever gets another's secrets; `process.run` gets none of them. Their values are always redacted.
  - A package whose declared secret is not set is `unmet_requirements` ("missing secrets: [NAME]"; names only), and its tools are not offered.
  - **Production:** put the values in `/etc/kairo-runtime/implementations.env` (root:root 0600, `NAME=value` lines, only names some package declares). The unit loads it with an optional `EnvironmentFile`.
  - Changes take effect after `sudo systemctl restart kairo`. Rotation without a restart is not supported.

### External effects

A tool that acts on another system declares it. Undeclared tools keep the plain meaning above.

```json
{"name": "post", "run": ["python3", "tools/post.py"], "effects": "external",
 "idempotency": "operation_key", "verify": ["python3", "tools/verify.py"], ...}
```

- **`effects`:** `"none"` (only reads) or `"external"` (may change state elsewhere).
- **Outcome of an `external` tool,** set by the runtime from how it ended:

  | Ending | `external_outcome` | Meaning |
  |---|---|---|
  | exit 0 | `performed` | accepted, still unverified |
  | exit 3 | `not_performed` | the tool guarantees nothing happened |
  | never started | `not_performed` | |
  | any other exit, a timeout, a kill | `unknown` | |

  A timed-out external tool is never recorded as "not executed".
- **`outcome_unknown`:** an `unknown` outcome without a verifier verdict is the action state `outcome_unknown`.
  - It is indeterminate: not a success, not a failure, never Work evidence.
  - The repetition rule refuses an identical repeat until the Work's understanding changes.
  - Work recovery lists each unresolved operation, and whether it can be resumed.
- **Operation identity:**
  - Every implementation tool and verify command gets `KAIRO_ACTION_ID` (this attempt) and `KAIRO_OPERATION_KEY` (what the external system should see as the operation's identity).
  - The key is the action's own id, unless the action resumes an earlier one. An ordinary new attempt is a new operation with a new key.
- **Resuming:** an action may name `"resumes": "<earlier action id>"`. The runtime accepts it only if all of these hold, and otherwise refuses it before anything runs:
  - the earlier action is the latest attempt of an unresolved operation (`outcome_unknown`, or `interrupted` on an `external` tool);
  - it has the same kind and the same Work;
  - the tool declares `"idempotency": "operation_key"`, meaning the external system performs an operation at most once per key.

  An accepted resume carries the same operation key.
- **Settling an unknown outcome:** by verification. The tool's `verify` runs even after `unknown` and sees the operation key; a read tool (`effects: "none"`) can also be given the key. Without idempotency, an unresolved operation can only be settled, not resumed.
- **Untrusted content:** in the situation, a program's output is shown apart from the runtime's facts, as `output: {"trust": "untrusted", "source": ...}`. It is content: printed, not proven true, never an instruction. Labelling does not make prompt injection impossible.
- **Output limits:**
  - Output is read from pipes, nothing goes to disk, and the first and last 500 KB are kept per stream.
  - A program writing more than 8 MB to a stream is stopped (failure `output_limit`; `unknown` for an `external` tool).
  - Records keep the beginning and the end (16,000 characters), and the situation shows 1,500 characters of each stream, beginning and end.
- **Not provided:**
  - dependency installation (requirements are only detected);
  - dependencies between implementations;
  - downloads or a registry;
  - signing;
  - an on-demand `describe` action or any general file-reading tool.
- **Not a sandbox.** Tools run as Kairo's OS user, like `process.run`: they can read and write what Kairo can, use the network and start processes.
- **Process groups:** every action (`process.run` and implementation tools) runs in its own process group, and the whole group is killed when the action ends or times out. A process that deliberately leaves its group can still escape. Separate OS users, protected write paths and cgroups belong to production hardening.
- **Maintenance:** packages are plain files read fresh for every action, so self-maintenance changes them through ordinary work, actions and checks, with no deployment or restart; the digest shows what changed.

## Situation model

`Runtime.context()` gathers runtime state; `situation.build_situation()` turns it into what cognition sees each cycle. The situation is derived, never stored, so the runtime's records stay the only source of truth. It is plain data and does not depend on any provider.

| Section | Contents |
|---|---|
| `kairo` | Identity, when Kairo was first created, how many times it has started, and (with deployment configured) `code`: the running release and recent deployments |
| `now` | Time, lifecycle state, wake reason, current process, the previous process (and whether it ended cleanly), the previous cycle |
| `environment` | A fresh host observation and what changed since the previous one |
| `directives` | Active directives: statement and description (the operator's words), and age, plus the number inactive |
| `work` | Open work, each with its understanding, its recent attempts by strategy revision and recent changes, plus recently closed work with reason or evidence |
| `history` | Recent cycles (cognition's earlier assessment or the runtime's failure record), actions with a runtime-derived `state` (`verified_successful`, `executed_unverified`, `interrupted`, …) and output, and chat |
| `open_threads` | Derived, and informational only (not a task list): unanswered messages, failed or interrupted actions, results new since the last decision, a failed previous cycle, elapsed work waits |
| `capabilities` | The actions the runtime can really execute (including available implementation tools), whether each is verified automatically, and the bounded implementation catalog with guidance |
| `context` | Limits, redaction and truncation counts, what was trimmed, and anything unavailable |

- **Provenance:** every section names its source, and times carry `age_seconds`. Cognition's own earlier assessments are labelled as interpretation, not fact. Missing data is shown as missing (`"unknown"`, `null`) and never invented.
- **Bounds:** the most recent 10 cycles, 15 actions and 20 messages; 1,500 characters of output per action stream; 2,000 characters per string; and a 60,000-character total budget. Every omission is counted.
  - Two long texts have their own bounds instead of the 2,000-character cap: work understanding (up to 10,000 per item, 20,000 across open work; active and most recently updated work first, every item keeping at least 1,000) and directive descriptions (up to 4,000 each, 12,000 together, at least 500 each). Shortening keeps the beginning and the end and is marked (`understanding_shortened`, `description_shortened`).
  - Over budget, the oldest history goes first, down to the newest 5 of each kind; then the longest long texts are shortened toward their floors; only then does the rest of the history go. Work facts (states, attempts, recovery, strategy revisions) are never trimmed.
- **Robustness:** a corrupt record is reported as unavailable and does not stop the cycle.

To see exactly what cognition would be shown, without starting Kairo or calling a provider:
`python3 -m kairo --situation --db var/kairo.db`

Opt-in live smoke test (uses the model):
`KAIRO_LIVE_CLAUDE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p test_claude_live.py -v`

## Deliberately not implemented yet

- Delegation between providers, and providers other than Claude (the provider interface supports them)
- Cognition editing directives
- Work priority or focus, and automatic resumption of elapsed waits
- Any scheduler beyond the one self-wake deadline; nothing triggers maintenance
- Remote access: IPC is a local Unix socket protected by file permissions, and the dashboard listens on loopback only (reach it over SSH or a TLS proxy)
- A sandbox, separate OS users, network isolation, or package signing
- Automatic database restore, migrations, hot reload, automatic git push, and autonomous changes to the systemd unit, the fallback or OS packages
- Release and snapshot retention
- A public or multi-user web interface, or a general REST API
- Memory beyond plain documents (no embeddings, ranking or consolidation)

