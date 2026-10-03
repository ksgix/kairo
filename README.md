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
| `todo.py` | `TodoItem`: operational notes. They do not drive the runtime; an empty list does not mean idle. |
| `chat.py` | `Message` / `Chat`: persisted human ⇄ Kairo messages. A message wakes a sleeping runtime. |
| `implementations.py` | Implementation packages: manifest validation, content digest, the derived catalog |
| `ipc.py` | The operator boundary: local Unix-socket IPC to the live runtime (reads, human input, wake, stop) and the terminal client |
| `deploy.py` | Self-deployment: immutable releases built from commits, the `runtime.deploy` action (preflight, snapshot, switch, restart) |

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
- **Output:** Claude answers with JSON validated against a schema (`reason`, `actions`, `replies`, `sleep`, `wake_after`), which Kairo parses strictly into a `Decision`. Claude cannot run anything itself; it can only request structured actions from the runtime's capabilities, which the runtime executes and records.
- **Failures:** a missing CLI, a non-zero exit, a timeout, empty or invalid output, or an invalid decision is recorded with a category. Kairo then sleeps until the next wake; the process keeps running.
- **Authentication:** whatever the local CLI is logged in with. Kairo stores no credentials.
- **Cycle log:** every cycle leaves a small `cycle` record with provider, result or failure category, requested action kinds, sleep choice, latency and cost.

## Ongoing work

The layers, from most lasting to most momentary:

| Layer | What it is |
|---|---|
| Directive | A lasting area of responsibility, set by the operator |
| Work | A pursuit carried across cycles: objective, why it matters, strategy, understanding, next step, state. It may belong to a directive or not. |
| Todo | Operational notes. Not required for work. There is no operator or cognition path to change them yet. |
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
- **Enablement** is configuration: `--implementations onec,web`, or `all` (default `none`), plus `--implementations-dir`. The catalog is derived from disk each time (`available`, `disabled`, `unmet_requirements`, `broken`, `missing`). There is no registry.
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

## Self-maintenance

Kairo maintains its own code as **ordinary work**, for a concrete reason (an observed failure, a verified defect, a capability real work needs), never because it is idle. There is no maintenance agent, queue, scheduler or work type. Cognition inspects, edits, tests and commits Kairo's source with ordinary `process.run` actions in a **development repository**, and the existing recovery rules apply (failure facts, strategy revisions, no blind repetition). The one new action, `runtime.deploy`, is for the step those actions cannot do: making a committed revision the code Kairo runs.

**Layout** (`--repository DEV --releases DIR`; deployment is off without them):

| Path | Meaning |
|---|---|
| `DEV` | development repository: for example a git worktree of `/opt/kairo` on its own branch, so the human checkout is never touched |
| `DIR/releases/<sha>/` | immutable release: `git archive` of one commit, read-only, never edited in place |
| `DIR/current` | the release the supervisor starts |
| `DIR/previous` | the release that requested the latest switch, which is the fallback target |
| `DIR/snapshots/` | a database backup taken before each switch (a recovery artifact, never restored automatically) |

The running release is never the development tree. Only committed revisions can be deployed; uncommitted or partial edits never run. The running revision is taken from the code the process actually imported, not from `HEAD`.

**`runtime.deploy {revision}`** goes through the normal action path (`Runtime.act` → `Environment.execute` → action record → verification):

1. Resolve the commit SHA. Names, paths and "latest" are refused.
2. Build the release, or reuse it if it already exists.
3. Preflight:
   - the candidate's own full test suite is a **gate**; "no tests ran" or only skipped tests count as failure;
   - the running release's suite is run against the candidate as **evidence** only;
   - a **dry cycle** of the candidate runs on a copy of the database. It must start, build its context, situation and cognition request, parse decisions and complete a cycle; no provider is called and no action is executed.
4. Snapshot the database.
5. `previous` := the running release; `current` := the candidate. Each is an atomic rename.
6. Request a restart.

The result records each stage, the files changed, and the **trust-critical** files changed (runtime, persistence, deployment, execution, verification, cognition parsing).

A failure at any stage leaves `current` unchanged, and becomes an ordinary failed attempt with its stage.

**Restart:**
- The deploy record, the cycle and the lifecycle reason are persisted first. Kairo then takes on no further actions and exits with status **75**.
- The supervisor starts `current`.
- A human stop still exits 0 and stays stopped.
- IPC is unchanged.

**Confirmation:**
- Until the restarted process confirms it, the deploy action is `awaiting_confirmation`. This is indeterminate, not a success, and cannot be cited as completion evidence.
- The new process checks that it runs the target revision, then confirms after its first usable cycle (a parsed decision, or a completed cycle without cognition). The evidence is the revision, digest, process start and cycle time, and the deploy becomes `verified_successful`.
- If a different release is running (the candidate did not stay up), the deploy becomes `verified_failed` with stage `confirmation`.
- **Probation:** while unconfirmed, if cognition fails because this code cannot use the provider's answer (`invalid_output`, `invalid_decision`, `provider_error`), the process exits with status 3 so the supervisor can recover. External failures (unavailable, timeout, rate limit) only delay confirmation.

**Known-good:** a release that was built from a commit, passed preflight, became current, started, and confirmed itself under supervision. `previous` always qualifies, because it is the release that ran the confirmed cycle in which the deploy was decided.

**Rollback:**
- To roll back, cognition deploys the previous revision. No `git reset`, and the development tree is never changed to restore the runtime.
- Automatic rollback exists only for one case: the new release cannot stay alive.

**Supervisor and fallback** (`deploy/kairo.service`, `deploy/kairo-fallback.service`, `deploy/kairo-fallback`; templates, installed once by the operator):
- `Restart=on-failure`, `RestartForceExitStatus=75`, `SuccessExitStatus=75`, and a start limit.
- `RestartMode=direct` (systemd 254 or later), so `OnFailure=` runs only after a crash loop exhausts the start limit, not on every failed exit.
- A release that keeps failing makes the unit fail, and `OnFailure=` runs the fallback.
- The fallback (about 20 lines of `sh`) switches `current` to `previous` **once**, then starts Kairo. If there is no valid previous release, or `current` already equals `previous`, it does nothing and Kairo stays stopped.
- It never touches the database.

**One database, one runtime:** `--run`, `--once` and `--preflight` hold an exclusive `flock` on `<db>.lock` for the life of the process, so a second runtime on the same database fails at once (exit 2). The OS releases the lock when the process dies. Other databases are unaffected. `--situation` is a read-only view and does not lock.

**Persistence compatibility:**
- Records are read tolerantly: fields this code does not know (written by a newer release) are ignored, so rolling back never hides directives or work.
- Known fields are still type-checked. A known field of the wrong type is corruption, except the fields Phase 6 already shows as unknown (times, logs, completion data).
- An older release that rewrites a record drops the fields it does not know.
- There are no migrations and no new record kinds or tables. Deployments live in their `runtime.deploy` action records.

**Context:** when deployment is configured, the situation has a bounded `kairo.code` section of runtime facts:
- the running revision, release, digest and status (`confirmed`, `awaiting_confirmation` or `operator_selected`);
- the `current` and `previous` links;
- the repository's HEAD, branch, dirty-file count and whether HEAD is running;
- the last 5 deployments with state and stage.

It contains no diffs, logs or source.

**Authority and trust:**
- Kairo keeps its broad authority over the host. The supervisor, fallback and release layout are **recovery infrastructure**, not a security boundary: they let Kairo be restarted and recovered when its own code is broken, and Kairo could still change them, though it does not do so as part of self-maintenance.
- Preflight is judged by the old, running code, and the fallback is outside the candidate.
- Residual risk: a sufficiently broken candidate could falsely confirm itself after the switch.
- Passing tests show that the suite passed. They do not show that a work objective is achieved; that remains cognition's judgment over the evidence.

**Install on a host** (operator, once):

Run as the account Kairo will run as, from `/opt/kairo`. That account's Claude CLI login is Kairo's cognition.

```sh
KAIRO_USER=$(id -un); KAIRO_HOME=$(getent passwd "$KAIRO_USER" | cut -d: -f6)
sudo mkdir -p /var/lib/kairo && sudo chown "$KAIRO_USER": /var/lib/kairo
git -C /opt/kairo worktree add /var/lib/kairo/dev -b kairo/dev       # development worktree
PYTHONPATH=/opt/kairo/src python3 -m kairo --init-release "$(git -C /var/lib/kairo/dev rev-parse HEAD)" \
    --repository /var/lib/kairo/dev --releases /var/lib/kairo/deploy   # first release -> current
sed -e "s|@KAIRO_USER@|$KAIRO_USER|g" -e "s|@KAIRO_HOME@|$KAIRO_HOME|g" deploy/kairo.service \
    | sudo install -m 0644 /dev/stdin /etc/systemd/system/kairo.service   # render the template
sudo install -m 0644 deploy/kairo-fallback.service /etc/systemd/system/
sudo install -m 0755 deploy/kairo-fallback /usr/local/libexec/kairo-fallback
sudo systemctl daemon-reload && sudo systemctl enable --now kairo.service
```

- **Requirements:** systemd 254 or later (`RestartMode=direct`).
- **What it installs:** `/etc/systemd/system/kairo.service`, `/etc/systemd/system/kairo-fallback.service` and `/usr/local/libexec/kairo-fallback`.
- **The unit template:**
  - `deploy/kairo.service` has two placeholders, `@KAIRO_USER@` (the Kairo service user) and `@KAIRO_HOME@` (its home directory, where the Claude CLI lives), and the `sed` step renders them.
  - The service user's authority is unchanged.
  - An unrendered unit never runs: no account has the placeholder's name, so the service fails to start (status `217/USER`).
  - To check an installed unit against the template, render it the same way and `diff` the result with `/etc/systemd/system/kairo.service`.

**Operating it:**

- **Status:** `PYTHONPATH=/opt/kairo/src python3 -m kairo.ipc --socket /var/lib/kairo/kairo.sock status`. The reply includes `revision`: the release this process actually imported. `readlink /var/lib/kairo/deploy/current` and `.../previous` show the links.
- **Logs:** `journalctl -u kairo -u kairo-fallback`.
- **Stop:** `sudo systemctl stop kairo` (or IPC `stop`). SIGTERM reaches only the runtime (`KillMode=mixed`): it takes on nothing new, lets the current action or cognition call finish, and exits 0. There is no restart and no fallback.
- **Exit status:**
  - 75 means a deployment selected a new release; systemd starts `current` again at once.
  - 3 means a just-deployed release could not use cognition before confirming itself.
  - Any other non-zero status is a failure, and systemd restarts after 3 s.
- **Crash-loop threshold:** more than 5 starts within 300 s makes the unit fail, and only then does `OnFailure=` run the fallback. Manual starts and restarts count too: after several in a row, run `sudo systemctl reset-failed kairo` so a manual restart cannot trip the fallback.
- **After a fallback** (the journal shows `kairo-fallback: current switched from … to …`):
  - Kairo runs the previous release again and records the deployment as failed (stage `confirmation`); its cognition sees that.
  - `current` and `previous` are now the same, so a second fallback does nothing.
  - If that release also cannot stay up, Kairo stays stopped. Read the journal before starting it again.
  - The database is never touched. `deploy/snapshots/` holds the pre-switch copies if a deliberate restore is ever needed.

Retention of old releases and snapshots is a later housekeeping concern.

## Situation model

`Runtime.context()` gathers runtime state; `situation.build_situation()` turns it into what cognition sees each cycle. The situation is derived, never stored, so the runtime's records stay the only source of truth. It is plain data and does not depend on any provider.

| Section | Contents |
|---|---|
| `kairo` | Identity, when Kairo was first created, how many times it has started, and (with deployment configured) `code`: the running release and recent deployments |
| `now` | Time, lifecycle state, wake reason, current process, the previous process (and whether it ended cleanly), the previous cycle |
| `environment` | A fresh host observation and what changed since the previous one |
| `directives` | Active directives with age and open to-do counts, plus the number inactive |
| `work` | Open work, each with its recent attempts by strategy revision and recent changes, plus recently closed work with reason or evidence |
| `todo` | Open items and recently completed ones |
| `history` | Recent cycles (cognition's earlier assessment or the runtime's failure record), actions with a runtime-derived `state` (`verified_successful`, `executed_unverified`, `interrupted`, …) and output, and chat |
| `open_threads` | Derived, and informational only (not a task list): unanswered messages, failed or interrupted actions, results new since the last decision, a failed previous cycle, the open to-do count |
| `knowledge` | Empty for now: the place where a future knowledge store plugs in |
| `capabilities` | The actions the runtime can really execute (including available implementation tools), whether each is verified automatically, and the bounded implementation catalog with guidance |
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

# from another terminal, the operator client (see "Operator interface")
python3 -m kairo.ipc status
python3 -m kairo.ipc message "hello Kairo"   # stored in chat; wakes Kairo
python3 -m kairo.ipc chat                    # the conversation, with Kairo's replies
python3 -m kairo.ipc stop                    # graceful shutdown

# single cycle: start, cycle, print status as JSON, stop
python3 -m kairo --db var/kairo.db
```

- The socket defaults to `$KAIRO_SOCKET`, or `var/kairo.sock` relative to the current directory. Run the client from the same directory or pass `--socket`.
- The socket is created with mode `0600` and removed on shutdown. A stale socket left by a crashed process is replaced; a socket another live process is listening on is never touched.
- The IPC protocol is one JSON object per line in each direction, one request per connection; see "Operator interface".
- `--reassess` defaults to 300 seconds; `0` means sleep until woken. Memory defaults to `$KAIRO_DB` or `~/.local/share/kairo/kairo.db`.
- With `pip install -e .`, `kairo` replaces `python3 -m kairo`.

## Operator interface

A human reaches Kairo only through IPC: the Unix socket of the live runtime, reached locally (for example over SSH). The terminal client `python3 -m kairo.ipc` is an adapter: it sends one request per command and prints what the runtime answered. It reads no database, keeps no state and decides nothing. If it disappeared, Kairo would be the same runtime.

**Commands** (production: add `--socket /var/lib/kairo/kairo.sock`; `--json` prints raw responses):

| Command | Operation | What it is |
|---|---|---|
| `status` | `status` | the live runtime: state, reason, identity, starts, revision, counts, last cognition result, protocol version, operations |
| `situation` | `situation` | exactly what cognition would be shown now, computed by the live runtime |
| `chat [--limit N] [--after SEQ]` | `chat` | the conversation, human messages and Kairo's replies, in order, with sequence numbers |
| `message TEXT [--id ID]` | `message` | a human message: persisted, then Kairo wakes. The answer comes later, in `chat` |
| `directives` | `directives` | all directives, active and inactive, with origin and history |
| `directive add STATEMENT` | `directive.add` | a new lasting area of responsibility |
| `directive deactivate ID` / `directive activate ID` | `directive.deactivate` / `directive.activate` | stop, or resume, pursuing a directive |
| `wake [REASON]` | `wake` | reassess now. Not "do X" |
| `stop` | `stop` | stop the runtime gracefully; the stop reason is recorded |

**Semantics:**

- **Messages are input to Kairo, not commands.** "Investigate why X is broken" is persisted as a human message, and cognition decides what it means. Any action that follows is an ordinary action, executed and verified by the runtime.
- **Replies are asynchronous.** `message` returns once the message is persisted (`id`, `duplicate`). Kairo replies in a later cycle, after provider latency. If cognition is unavailable, the message stays unanswered and `status.cognition_last` shows why.
- **Only the operator channel writes human messages.** Cognition's replies are written as `kairo`, and nothing else posts messages, so external content can never appear as human input. A test enforces this structurally.
- **Idempotent delivery.** `--id` (1–100 characters of `A-Z a-z 0-9 . _ : -`) makes delivery idempotent:
  - the record id is derived from the client id;
  - sending the same id and text again returns the stored message with `duplicate: true`, without posting or waking again, even after a restart;
  - the same id with different text is `rejected`.

  There is no separate idempotency store: the message record is the state. Without `--id`, every send is a new message.
- **Directives.** A directive is Kairo's purpose: a lasting area of responsibility, not a task, schedule or command.
  - Adding one executes nothing. Cognition sees it from the next cycle, and Kairo wakes to reassess.
  - Directives are never edited or deleted, only deactivated and activated again, so Work linked to one keeps its meaning.
  - Each records `origin: "operator"` and a history (`created`, `deactivated`, `activated`, with time and origin).
  - A statement is 1–500 characters, and an active duplicate (same words, any case or spacing) is refused.
- **Evidence.**
  - Messages, directives and their history are persisted records.
  - Wakes appear as the lifecycle and cycle wake reason.
  - An operator stop is recorded as `stopped: stop requested over ipc`.
  - Everything survives restart.
- **Reads come from the live runtime.** `--situation` follows the same rule: when a runtime owns the database, it fetches that runtime's situation over IPC (pass `--socket`). Otherwise it builds a preview that claims no running release.
- **Not provided:**
  - no execute, shell or action operation (direct operation of the host is SSH, outside Kairo);
  - no Work editing (ask Kairo in a message);
  - no deployment or configuration operations;
  - no todo operations (todo is unused operational state; deferred);
  - no paged action history (the situation shows recent actions);
  - no web, HTTP or network listener.

**Protocol 2:**

- One JSON object per line each way, one request per connection.
- Every operation names the fields it accepts; any other field is `invalid_params`.
- Success: `{"ok": true, "result": {...}}`.
- Failure: `{"ok": false, "error": "...", "code": ...}`, where `code` is one of:
  - `malformed_request`: not one JSON object, or over 64 KiB;
  - `unknown_op`;
  - `invalid_params`;
  - `rejected`: well-formed but not applicable, such as an unknown directive or a reused message id;
  - `persistence_error`;
  - `response_too_large`: over 4 MiB;
  - `internal_error`: redacted, never a traceback.
- Reads are bounded:
  - `chat`: at most 200 messages, 8,000 characters each, redacted, marked when truncated;
  - `situation`: the situation's own budget;
  - `directives`: at most 200.
- If Kairo is unreachable, the client exits 2. An older runtime (protocol 1) answers new operations with `unknown op`, and the client says so.
- The server handles one request at a time, on one thread.

**Trust boundary:**

- Whoever can open the socket is the operator. That means mode 0600 and the Kairo service user, which normally has sudo, because Kairo is designed to run with broad authority.
- The operator's messages can lead Kairo, which has broad authority on this host, to do anything it can do, so the socket is root-equivalent.
- There is no authentication beyond file permissions, and no network exposure.
- A future browser interface would be a separate, stateless HTTP-to-IPC translator reached through an SSH tunnel or real authentication (see `docs/phase-10-architecture-review.md`). It is not part of this phase.

## Tests

```sh
cd /opt/kairo
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The tests need no network, credentials or third-party packages.

## Deliberately not implemented yet

- Delegation between providers, and providers other than Claude (the provider interface supports them)
- Cognition editing directives or to-do items; operator to-do operations
- Work priority or focus, and automatic resumption of elapsed waits
- Any scheduler beyond the one self-wake deadline; nothing triggers maintenance
- Remote access of any kind and any web interface: IPC is a local Unix socket, protected only by file permissions, with no authentication
- A sandbox, separate OS users, network isolation, or package signing
- Automatic database restore, migrations, hot reload, automatic git push, and autonomous changes to the systemd unit, the fallback or OS packages
- Release and snapshot retention
- Dashboard, web UI, REST API
- Memory beyond plain documents (no embeddings, ranking or consolidation)

## License

MIT. See [LICENSE](LICENSE).
