# Phase 10 architecture review: human interface and external interaction

Status: architecture review only, no implementation.
Reviewed: repository at `22d0a8b` (Phase 9 plus production installation), and the production installation on this host on 2026-10-02.

## 1. Executive summary

The prompt for this review assumed Phase 10 is "a human interface plus external interaction", and the obvious reading of that is a dashboard plus integrations. The code does not support that framing.

**Human side: the gap is the authority path, not a dashboard.**

- The human interface is almost write-only. Over IPC a human can send a message, wake Kairo or stop it, but cannot read Kairo's replies. Replies exist only as `message` records in SQLite.
- There is no operator path for directives or todo. `Directives.add` and `Todo.add` exist only as Python methods.
- Directives are Kairo's lasting purpose source, so production Kairo has no purpose source except chat. On 2026-10-02 it ran 41 cycles with 0 directives, 0 messages and 0 actions, spending $2.81 of cognition to conclude correctly each time that nothing was worth doing.
- The missing piece is a complete operator boundary, not a richer view.

**External side: the gaps are small and concrete.**

- The implementation system (Phase 8) is already the right shape for outbound interaction: inert capability packages, tools as ordinary actions, per-package secrets, verify commands, provenance.
- Four things are missing:
  1. Tools never receive the action id, so they cannot give an external system an idempotency key.
  2. The production unit has no way to supply implementation secrets.
  3. The instructions tell cognition to "treat runtime records and observations as facts", and that framing covers the stdout of external programs. External content then reaches cognition framed as fact, not as untrusted data.
  4. Message intake is not idempotent.
- Inbound events need no new mechanism yet. They have a clear future shape (section 10) and should not be built until a real event source exists.

**Decision.** Phase 10 should:

- make IPC the one complete operator boundary: read projections computed by the live runtime, plus human input for messages and directives;
- make the terminal client over SSH the first human interface;
- add the four small external-interaction foundations above.

A web UI, inbound events, approval gates and concrete integrations are not part of the minimum. Section 19 lists the minimum scope; section 26 gives the decision.

## 2. Current architecture discovered

All of this was verified in code (`src/kairo`, about 5,000 lines, standard library only) and on the production host.

### A. Runtime authority (`runtime.py`)

| Concern | Owner | Mechanism |
|---|---|---|
| Lifecycle (`created → awake ⇄ sleeping → stopped`) | `Runtime` | `_transition` under one condition variable; every transition is persisted as the `runtime/lifecycle` record |
| Waking | `Runtime` | `receive` (message), `request_wake` (IPC), its own deadline (`wake_after`, the `reassess_after` default, or an elapsed Work wait) |
| Sleeping | `Runtime` | blocking wait on the condition variable; cognition chooses to sleep |
| Action execution | `Runtime.act` → `Environment.execute` | `process.run`, `impl.<id>.<tool>`, `impl.<id>.check`, `runtime.deploy`; one path; a `started` record is written before execution |
| Verification | `Runtime.act` → `verify()` | runtime verifiers, implementation `verify` commands and checks, `AwaitSuccessor` for deploys; no verifier means `unverifiable` |
| Persistence | `Memory` (`memory.py`) | one SQLite document store (`records(kind,id,seq,data)`), exclusive per-database `flock` |
| Deployment | `deploy.Deployment` | immutable releases, `runtime.deploy`, confirmation by the successor process |
| Runtime identity | `runtime/identity` record | id, `born_at`, `starts` |
| Process lifetime | systemd (`kairo.service`) | exit 75 = restart; `RestartMode=direct`; start limit 5 in 300 s; `OnFailure` runs the fallback |
| Release recovery | `kairo-fallback` (sh) | selects `previous` once; no cognition |

### B. Cognition (`cognition.py`, `situation.py`, `instructions.py`, `claude.py`, `registry.py`)

- **Input.** `Runtime.context()` gathers a `Context`:
  - environment observation (host facts only);
  - active directives;
  - open todo;
  - the last 20 messages;
  - the last 15 actions with results and verification;
  - the last 10 cycles;
  - open Work and recently closed Work, with attempt logs;
  - implementation catalog and guidance;
  - `code` facts (Phase 9);
  - runtime facts (identity, starts, previous process, which provider is being asked).
- **Situation.** `build_situation` projects the Context into bounded, redacted, deterministic JSON with sources and ages, and cognition receives that. Runtime facts and cognition's earlier interpretations are labelled differently.
- **Output.** A `Decision`: `reason`, `actions[]` (kind, params, reason, work), `replies[]`, `sleep`, `wake_after`, `work[]` (create, update, set_state requests). It is parsed strictly against the live action catalog.
- **Boundary.** Providers are tool-less (`claude -p --tools ""`) and have no side effects. `Cognition` tries providers in a fixed order and falls back only on technical failure. Everything that affects the world is an action executed by the runtime.

### C. Work (`work.py`)

- A Work item records: objective, why, strategy (with numbered revisions), understanding (plus when it last changed), next_step, and a state (`active`, `waiting` with an optional `waiting_until`, `blocked`, `completed`, `abandoned`).
- Attempts are the action records whose `work_id` points at the Work.
- Completion requires evidence from those attempts, and the runtime records whether the completion basis was `verified` or `unverified`.
- Failures are derived facts: `failure_of`, `action_state`, and per-strategy-revision recovery facts.
- An identical repeat of a failed or interrupted attempt is refused until the Work's understanding changes.
- Cognition only sends requests; `WorkLedger.apply` validates them.

### D. Implementations (`implementations.py`)

- Packages are inert directories with a strict manifest. Tools and checks become ordinary actions, with parameters sent as JSON on stdin and no shell.
- Each package receives only its own declared secrets.
- Guidance is shown as labelled, untrusted data. Actions carry provenance (`{id, digest}`).
- Enablement is configuration (`--implementations`).
- Packages are passive capabilities, not autonomous entities.
- **Production:** none enabled. `/var/lib/kairo/implementations` does not exist.

### E. External IPC (`ipc.py`)

- A Unix socket at mode 0600 (`/var/lib/kairo/kairo.sock`), one JSON request per connection, served by one background thread.
- Operations:
  - `status`: state, reason, identity, starts, running revision, counts, implementations, provider order and last provider;
  - `message`: posts a human message and wakes Kairo;
  - `wake`;
  - `stop`.
- It deliberately cannot execute actions.
- It cannot read chat, Work, directives, history or the situation.
- It cannot write directives or todo.

### F. Self-maintenance (`deploy.py`, `deploy/`)

- Kairo edits, tests and commits in `/var/lib/kairo/dev` (a git worktree on branch `kairo/dev`).
- `runtime.deploy {revision}` builds `deploy/releases/<sha>`, runs preflight, snapshots the database, switches `current`/`previous`, and exits 75.
- systemd restarts the process, and the successor confirms the deployment.
- systemd owns process lifetime; the fallback owns one release switch; Kairo owns everything else.
- **Production:** release `22d0a8b`, operator-selected, no deployments yet.
- **Production:** the reboot on 2026-10-02 09:40 brought Kairo back automatically with no fallback.

### Other production facts relevant to Phase 10

- Only SSH (port 22) listens on the network. Caddy runs, but with global options only and no sites.
- A human reaches Kairo today only through SSH, then the Unix socket, or by reading SQLite directly.
- The unit file has no `EnvironmentFile`.
- `kairo.db` is mode 0644 inside a 0755 directory.

## 3. Current boundaries, and the weaknesses this review found

| # | Weakness | Evidence |
|---|---|---|
| W1 | The human interface is write-mostly: a human can send a message but cannot read the reply | `ipc.py` has only `status`, `message`, `wake`, `stop`; `Decision.replies` are posted to the `message` kind (`runtime.py`, `for reply in decision.replies`) and no client reads them |
| W2 | No operator path for directives or todo | no caller of `Directives.add`, `set_active` or `Todo.add` outside tests; production has 0 directives; README says todo is "maintained by the operator" with no means to do it |
| W3 | With no purpose source, Kairo pays to rediscover that nothing matters | 41 cycles, all `decided`, 0 actions, $2.81 on 2026-10-02 (cognition stretched its own wake interval to about 30 min, which is correct behaviour; the cost comes from having no directives) |
| W4 | External content is framed as fact | `instructions.py`: "Treat runtime records and observations as facts"; `history.actions` shows stdout and stderr with no content label. Today only `process.run` output (e.g. `curl`) is affected; it matters for every integration |
| W5 | Implementation tools cannot be idempotent towards external systems | `_run_implementation` passes `json.dumps(action.params)` on stdin and no action id; the `verify` command also gets no id |
| W6 | Implementation secrets cannot be supplied in production | `deploy/kairo.service` has no `EnvironmentFile`; the only option today is `Environment=` in a world-readable (0644) unit file |
| W7 | Message intake is not idempotent | `Chat.post` always mints a new uuid, so a client retrying after a timeout duplicates the message; harmless for a human, fatal for event intake |
| W8 | `--situation` reports the wrong running revision under deployment | it computes the release from the *invoking* process's code, not the service's. The authoritative projection must come from the live process |
| W9 | Conversation continuity is the last 20 messages | `LIMITS.messages = 20`; there is no long-term memory (`knowledge` is empty by design) |
| W10 | IPC serves one request at a time | a slow read operation would delay `stop` and `wake`; read operations must be bounded |
| W11 | One anonymous human | `Sender` is `human` or `kairo`; there is no notion of which human or which channel |

W1, W2, W4, W5 and W6 are the ones Phase 10 must fix. W7, W8 and W10 shape the design. W9 and W11 are deferred.

## 4. Phase 10 problem definition

Phase 10 has two separate problems, and they have different authority properties.

1. **Operator boundary.** How a human observes, communicates with and steers a persistent autonomous runtime without becoming its source of autonomy and without bypassing it.
   - The human is an **authority**. Their messages and directives legitimately steer Kairo.
   - The danger is a second control path: a UI that executes, mutates Work or stores its own state.
2. **External interaction.** How Kairo acts on and observes systems it does not control.
   - External systems are **untrusted sources and targets**.
   - The dangers are content steering cognition with root authority (prompt injection), side effects that are duplicated or never verified, and credentials leaking.

Treating these as one "interaction layer" would erase the most important distinction in Phase 10: a human message carries operator authority, while an email body or API response carries none. The architecture must never let external content arrive on the human channel.

## 5. Human interface architecture

### Classification of candidate surfaces

| Surface | Underlying state | Kind | Owner | UI may mutate? | Boundary |
|---|---|---|---|---|---|
| Lifecycle, status, revision | `runtime/lifecycle`, `runtime/identity`, in-memory state | authoritative, read | Runtime | no | IPC `status` |
| Situation (what cognition sees) | `build_situation(runtime.context())` | derived, read | Runtime | no | IPC `situation` (new, computed in the live process) |
| Chat | `message` records | human input plus authoritative history | Runtime (`Chat`) | append only | IPC `message`, `chat` |
| Directives | `directive` records | human input (purpose) | operator | create and deactivate | IPC `directive.add`, `directive.deactivate` (new) |
| Todo | `todo` records | human input (operational notes) | operator | add and complete | optional (section 19.B) |
| Work | `work` records | authoritative; cognition requests changes | WorkLedger | **no** | read only, via `situation` |
| Action history | `action` records | authoritative evidence | Runtime | **no** | read via `history` (paged) |
| Cycles | `cycle` records | authoritative log | Runtime | no | read via `situation` |
| Implementations | filesystem plus configuration | configuration and capabilities | operator (filesystem, CLI flags) | no (not via UI) | read via `status` and `situation` |
| Deployment and releases | links, action records | authoritative | Runtime and systemd | **no** | read via `situation` (`kairo.code`) |
| Stop and wake | lifecycle | operational control | operator | yes | IPC `stop`, `wake` (existing) |
| Configuration (providers, flags) | unit file | configuration | operator | no | edit the unit plus `systemctl` (outside Kairo) |

### Rules, answering the per-surface questions

- **Authority.** The UI is a client of IPC. It holds no authoritative state, no database and no queue.
  - Every read is a projection computed by the live runtime, so derived states such as `action_state`, `awaiting_confirmation` and the running revision come from the running release's code, not from a client's possibly different copy (W8).
  - Every write is one of three human inputs (message, directive, todo) or one of two operational controls (stop, wake).
- **What the UI can never do:**
  - execute actions;
  - edit Work;
  - touch deployment state;
  - run shell commands;
  - open SQLite.
- **Mutations are persisted records that cognition sees.**
  - A message wakes Kairo and appears in `history.chat` and `open_threads.unanswered_human_messages`.
  - A directive appears in `directives`.
  - None of them executes anything. What follows is a cognition decision, executed and verified by the runtime like any other.
- **Why no second source of autonomy.** The UI has no timer, no queue and no "do X" operation. The only cause-and-effect path is: input record, wake, cognition, decision, runtime.

### Rejected alternative surfaces

| Rejected | Why |
|---|---|
| Editing Work from the UI | Work is cognition's representation of its own pursuit and is validated by `WorkLedger` against its rules. A human who wants a change says so in a message, and cognition updates the Work |
| Reading SQLite from the UI | it couples the UI to record shapes and to whichever release's derivation code the UI imports, giving a second interpreter of state (W8) |
| A "run command" operation | it is not autonomous cognition and must not be recorded as such. A human who wants to execute something has SSH; Kairo observes the effects |
| A `pause` lifecycle state | not needed. `stop` exists, and with nothing to do Kairo already sleeps; a third lifecycle state would change Phase 2 for no capability gain |

## 6. Chat architecture

- **Entry.** Human messages enter through the existing IPC `message` operation, which calls `Runtime.receive`: `Chat.post(HUMAN)` is persisted first, then `request_wake("message received")`.
  - This is already correct and survives restarts.
  - A message that arrives mid-cycle forces one more reassessment.
  - A message that arrives during a deploy restart is persisted and seen by the successor process.
  - Phase 10 keeps this path.
- **Cognition's view.** Cognition sees human messages as **input from the operator**, not as prompts:
  - they are listed in `history.chat`;
  - unanswered ones are derived in `open_threads`;
  - they sit next to directives, Work and the world state.
  - A message does not automatically create Work: cognition decides, as now, whether it deserves pursuit.
- **Replies.** Replies are `Decision.replies`, persisted as `message` records from `kairo`.
  - They are asynchronous: the reply comes in the next cycle, after provider latency (5–10 s in production).
  - If the provider is unavailable, the message stays unanswered. The human must be able to see that fact: `status` should show the last cycle's result and failure category, which `cognition_last` currently omits.
- **Sleeping, waiting and failure cases:**
  - a sleeping Kairo wakes on a message;
  - a Kairo whose Work is `waiting` still wakes, because waiting is a property of the Work, not of the runtime;
  - if cognition fails, the message persists, and the next successful cycle sees it unanswered.
- **What Phase 10 adds:**
  - an IPC `chat` read operation (messages after a given `seq`, bounded), so clients can show replies (W1);
  - an optional client-supplied message `id`, so intake is idempotent on retry (W7). The record id becomes `h(client_id)`, and storing the same id twice is a no-op.
- **Human requests to act.** "Please do X" is a message. Kairo decides whether and how, records actions with reasons, and the runtime verifies them.
  - This is deliberately different from the human doing X (SSH) and from any "execute" operation, which must not exist.
- **Not added:**
  - message threading or `in_reply_to`: with one operator and ordered history, "unanswered" is already derivable;
  - streaming or "typing" states: a cycle is the unit of response.

## 7. Human intervention model

| Intervention | Necessary? | Category | Where | Persists | Seen by cognition | Runtime executes directly | Audit |
|---|---|---|---|---|---|---|---|
| Send a message | yes | human input | IPC `message` | `message` | yes | no | the record |
| Provide information | yes | human input (a message) | IPC `message` | `message` | yes | no | the record |
| Ask Kairo to investigate or do X | yes | request (a message) | IPC `message` | `message` | yes | no; cognition decides | record, then the action records |
| Create a directive | **yes (W2)** | purpose | IPC `directive.add` (new) | `directive` with `origin: "operator"` | yes | no | the record (`created_at`, `origin`) |
| Deactivate a directive | yes | purpose | IPC `directive.deactivate` (new) | `directive` `active=false`, `deactivated_at` (new field) | yes (as a count) | no | the record |
| Edit a directive in place | no | – | – | – | – | – | replace it: deactivate, then add. A directive's meaning changing in place would invalidate Work linked to it |
| Inspect Work, history, failures, deployment | yes | observation | IPC `situation`, `history` | – | – | – | – |
| Wake | exists | operational control | IPC `wake` | lifecycle reason | yes (wake reason) | yes | lifecycle record |
| Stop | exists | operational control | IPC `stop` or `systemctl stop` | lifecycle | after restart | yes | lifecycle record plus journal |
| Approve a sensitive operation | **not as a mechanism** (section 11) | coordination via chat and Work `waiting` | – | Work state and the human's message | yes | no | Work history plus message |
| Change configuration (providers, implementations) | not in Phase 10 | configuration | unit file plus `systemctl` | unit file | via `status` and `capabilities` | – | journal |
| Execute X directly | **must not exist in Kairo** | operator shell | SSH | – | Kairo may observe effects | – | host audit, not Kairo |

The key distinction: **"human asks Kairo to do X"** enters as a message and passes through cognition, the runtime and verification. **"Human does X"** stays outside Kairo entirely. Nothing in the interface turns the first into the second.

## 8. External interaction architecture

**Outbound interaction uses implementations, unchanged in shape.** An integration (email, GitHub, an HTTP API, a remote machine) is an implementation package:

- tools are ordinary actions `impl.<id>.<tool>`;
- credentials are declared per package;
- `guidance` describes the domain;
- `checks` and per-tool `verify` commands establish outcomes;
- provenance records exactly which package content ran.

The runtime executes and verifies exactly as for any action. Kairo decides when and why to use a package; the package never decides.

**Is the existing implementation system sufficient?** Mostly. Three concrete gaps, all additive:

1. **No idempotency key (W5).** Pass the action's id to tools and verify commands, for example as the environment variable `KAIRO_ACTION_ID`.
   - The stdin contract stays unchanged, so existing packages keep working.
   - A tool can then send it as an idempotency key (GitHub, Stripe and most mail APIs accept one), and the verify command can look the effect up by it.
   - This is the only way to make "the external operation succeeded but Kairo crashed before recording it" both recoverable and non-duplicating.
2. **No credential source (W6).** Add `EnvironmentFile=-/etc/kairo/kairo.env` to the unit. The file is root-owned and 0600, and systemd reads it as root.
   - Values reach only the Kairo process.
   - Only packages that declare a name receive its value (`_package_env`).
   - `process.run` never receives declared names (`scrubbed_env`), and their values are redacted everywhere.
   - Names that no package declares are still redacted by name pattern (`SECRET_NAME`), but `process.run` *does* inherit them. Operator rule: only declared names go in the file.
3. **Effect declaration (optional, section 19.B).** A per-tool `effects: "read" | "external"` field would let the situation say which tools change the outside world.
   - That matters for interpreting `interrupted`: repeating an interrupted read is safe, repeating an interrupted external effect may not be.
   - It is a fact cognition can use, not a permission.
   - It requires manifest format 1 to accept one more optional field; unknown fields are rejected today.

**Rejected alternatives:**

- a connector or integration subsystem with its own executor, scheduler or credential store: a second executor;
- per-integration loops, such as a mail poller that runs by itself: hidden autonomy;
- MCP-style servers that cognition calls during its decision: this breaks the tool-less provider boundary and puts execution outside the runtime.

`process.run` with `curl` remains possible because Kairo has root. It is unverified and has no secrets path. The instructions should steer external side effects towards implementations, but this cannot be enforced, and the review does not pretend otherwise.

## 9. External observation model

An external response is an **observation with provenance**, not truth. The action record already captures everything needed:

| Element | Field in the action record |
|---|---|
| when | `started_at`, `finished_at` |
| which program and package content | `kind`, `implementation.digest` |
| transport and execution outcome | `executed`, `failure`, `returncode` |
| what the system said | `stdout`, `stderr` (capped) |
| whether the outcome was checked | `verification` |
| age shown to cognition | `history.actions` |

Failure mapping, using Phase 6 vocabulary with no new taxonomy:

| Situation | How it appears |
|---|---|
| network failure, timeout | the tool's own exit code and stderr (`exited_nonzero`), or `timed_out` from the runtime |
| authentication failure, rate limit | the tool's exit code and stderr, interpreted by cognition (an exit code is only a number) |
| partial or malformed response | `verify` returns failure or "unverifiable" (exit code other than 0 or 1) |
| transport success but semantic failure | exit 0 but `verify` fails, giving `verified_failed` |
| stale information | the observation's age, which already exists |

**The one missing piece is framing (W4).** The instructions call records "facts". For external content, the fact is only that *this program printed this text at this time*. Whether the text's claims are true is a separate question, and **instructions inside it are never instructions to Kairo**.

Phase 10 must make that explicit:

- in `instructions.py`, extending the guidance rule from Phase 8 to all action output;
- in the `history.actions` note.

No new observation store is needed. Persistent "latest known state of external system X" belongs to the deferred knowledge layer, not to Phase 10.

## 10. Inbound event model

Today the only inbound path is the human message, and polling through implementations covers most event-like needs: cognition sets `wake_after` or a Work `wait_seconds`, then checks.

True push events (webhooks, inbound email delivered to Kairo) should be built only when a concrete source exists. Their architecture is fixed here so it is not improvised later.

```
external sender ──(network)──> ingress adapter (separate process; verifies signature, timestamp window)
        ──(IPC op "event", local socket)──> Runtime: persist `event` record (idempotent), request_wake
        ──> next cycle: situation.events (recent, untrusted, new since the last decision) ──> cognition
```

- **Identity and deduplication.** The record id is `hash(source, external_id)`. A redelivered event overwrites the same record, so replay is harmless.
- **Ordering.** Records are ordered by `seq` (arrival). Both `occurred_at` (sender's claim) and `received_at` (runtime fact) are kept, so a stale event is visible as stale.
- **Authenticity.** The adapter verifies signatures because it holds the webhook secret. The event record stores `authenticated_by` as a fact. An unauthenticated event can still be recorded, but it is labelled as such.
- **Wake semantics.** The same as a message. Events never carry operator authority and are never written as `message`.
- **Kairo down or restarting.** The adapter returns failure (HTTP 503) and **does not buffer**: buffering would be a second store. Senders that retry deliver later; senders that do not retry lose the event, which is a documented limitation.
  - A durable spool directory that the runtime ingests is the alternative if a source does not retry. It is a queue with its own lifecycle, and is justified only by a concrete source.
- **Failure handling.** The event is persisted before any reaction. Cognition decides the reaction. There is no automatic event → action rule: that would be a hidden autonomy rule.
- **Rejected:** an HTTP listener inside the runtime process. It would expose the runtime to the network, disappear during every deploy restart, and mix ingress parsing with runtime authority.

## 11. Security model

Kairo keeps root-level authority (it runs as `kamin` with passwordless sudo). Phase 10 does not redesign that, and it cannot pretend to sandbox external interaction. What it can do is keep these four things distinguishable and auditable:

| What | Where it lives |
|---|---|
| what Kairo intended | `Decision.reason`, `action.reason` |
| what the runtime executed | the action record: kind, params, provenance |
| what the external system reported | output, labelled untrusted |
| what was verified | the verification and the derived state |

**Specific positions:**

- **The human channel is root-equivalent.** A message can lead Kairo to do anything Kairo can do.
  - The IPC socket's protection (0600, user `kamin`) plus SSH is therefore the operator authentication.
  - Any web UI added later is a root-equivalent surface: it must be bound to localhost and reached through an SSH tunnel, or sit behind real authentication. It must never be exposed directly.
  - Caddy is present but serves nothing today. Exposing a UI through it is a deliberate security decision, not a default.
- **Credentials:**
  - never in SQLite, the situation, IPC responses, the UI or git;
  - kept in a root 0600 `EnvironmentFile`;
  - handed only to the declaring package;
  - redacted by value everywhere.
  - Residual: Kairo has root and could read the file; redaction catches exact values but not transformed ones. This is accepted, as in Phases 7–9.
- **Untrusted content** (external output, implementation guidance, future events) is labelled data. **Human messages are the only input with operator authority.** No transport may write `message` records except the human channel.
- **No approval gate as a security mechanism.** A runtime gate on `impl.mail.send` would be bypassed by `process.run curl`, because Kairo has root. That would be a false boundary.
  - The legitimate need, letting the human confirm before an irreversible external act, is coordination. It is already expressible: cognition asks in a reply, sets the Work to `waiting` (reason "awaiting operator confirmation"), and continues when the human's message arrives (messages wake Kairo).
  - It is visible in Work state and history, and needs no new mechanism.
- **Prompt injection is the dominant new risk.** Labelling and instructions reduce it but do not eliminate it. Section 22 lists the residual risk.

## 12. Persistence implications

**Minimum Phase 10 adds no new record kinds and no tables.**

| Concept | Representation | Why it is sufficient, and the details |
|---|---|---|
| Human messages | existing `message` kind | optional client id becomes the record id, for idempotency |
| Directives from the operator | existing `directive` kind, plus fields `origin`, `deactivated_at` | readers already ignore unknown fields. An older release that rewrites a directive drops the new fields (the known Phase 9 limitation); the directive itself survives |
| External operations | existing `action` records | they already hold intent, execution, output, verification, provenance and interruption. A separate "external operation record" would duplicate the same fact |
| External observations | action outputs | no new store; long-term observation memory is deferred (knowledge) |
| UI sessions | none | the UI holds no authoritative state; frontend state is disposable |
| Audit | the records themselves, plus the journal | every mutating IPC operation writes exactly one record that carries its time and origin; a separate audit log would duplicate them |
| Inbound events (later) | new `event` kind when implemented | must be separate from `message` to keep authority distinct; deduplicated by id |

**Restart and crash answers:**

- Messages and directives are written before any reaction, so they survive restart.
- An external operation whose result is lost because of a crash appears as `interrupted` (the `started` record exists).
- The repetition gate refuses an identical retry until cognition reassesses.
- The action id, used as an idempotency key, lets the external system deduplicate if a retry is made.
- The tool's verify command, or a later check action, establishes what actually happened.

## 13. Failure and recovery implications

These are Phase 6 principles, applied:

| Failure | How it surfaces |
|---|---|
| Network failure, timeout, authentication, rate limit | ordinary failed attempts with failure kind, exit code and stderr; cognition diagnoses; no retry loop exists anywhere. Waiting with `wait_seconds` is how "try again later" is expressed |
| Side effect succeeded, response lost | `exited_nonzero` or `timed_out` with a possibly real side effect. The tool should treat ambiguous outcomes as exit codes other than 0 or 1 so `verify` reports "unverifiable"; cognition checks before repeating, and the idempotency key makes a repeat safe |
| Retry would duplicate | only possible without an idempotency key. Packages with external effects should require one; a test for this belongs to each integration's contract tests |
| Duplicated inbound event (later) | the same record id, so it is a no-op |
| Event while asleep | it wakes Kairo |
| UI disconnects or the browser closes | nothing happens: the UI holds no state; messages already sent are persisted |
| Cognition fails | inputs persist; `status` shows the failure; the next successful cycle sees everything |
| Deploy during interaction | IPC is unavailable for seconds. Clients report "not reachable (restarting?)" and retry, idempotently. Actions are sequential and `KillMode=mixed` lets an in-flight action finish, so a deploy never interrupts an external operation |

## 14. Verification model

This uses existing machinery, with semantics made explicit for external effects:

| Term | Runtime representation |
|---|---|
| requested | `started` action record, written before execution |
| executed | `result.executed`, meaning the program ran (not that the world changed) |
| observed | `result.output`, untrusted content |
| verified | verification `success` from a runtime verifier, the tool's `verify` command or a check |
| unverified | `executed_unverified` (exit 0, nobody checked) or `exited_nonzero` |
| failed | `failed_to_execute`, `verified_failed`, `exited_nonzero` |
| indeterminate | `interrupted`, `in_progress`, `awaiting_confirmation` |

Principles carried into Phase 10:

- Exit 0 is transport success, never world success.
- An integration with external side effects should ship a `verify` command that observes the external state, for example "the issue now exists, carrying idempotency key K".
- Without one, the runtime records `executed_unverified`, and any Work completion that rests on it is labelled `unverified`.
- Eventually consistent effects are verified later by an explicit check action, which cognition cites as completion evidence.
- No deferred-verification mechanism is needed. The Phase 9 successor-confirmation pattern is specific to restarts and must not be generalised into a background verifier.

## 15. Provider independence

Nothing in this design is provider-specific:

- chat is `Decision.replies`;
- human input and external content reach cognition only through the situation;
- integrations are actions;
- verification is runtime-owned.

Phase 10 creates no new requirement for provider delegation, provider-specific context or provider memory. Conversation continuity comes from persisted chat (the last 20 messages, W9), never from provider sessions (`--no-session-persistence`).

Long-running external operations are not long-running cognition: they are an action plus waiting plus later observation, so they need no provider changes. Providers stay tool-less.

## 16. Phase 9 compatibility

| Concern | Position |
|---|---|
| Deploy restart | the IPC socket disappears for a few seconds and the path stays stable. Clients must tolerate "connection refused" and use idempotent message ids |
| Old and new releases speaking IPC | a newer client against an older release gets `unknown op`. `status` should report a protocol version and the list of operations, so clients degrade cleanly after a fallback |
| New directive fields | tolerant readers handle them; an older release that rewrites a record drops them (known limitation) |
| Future `event` kind | an older release ignores unknown kinds (counts use a fixed list; nothing iterates all kinds) |
| `EnvironmentFile` | an operator-installed unit change, not Kairo-modifiable infrastructure (Phase 9 boundary); the template change ships in the repository |
| Self-maintenance | unaffected: IPC changes, projections and instructions are ordinary runtime code, deployed through `runtime.deploy` with preflight |
| Migrations | none needed |
| A future web adapter | a separate process that survives Kairo restarts and shows "unavailable" while the socket is down. It is not managed by Kairo's deploy |

## 17. Architectural alternatives

### Human interface

| Option | Architecture | Advantages | Disadvantages and violations |
|---|---|---|---|
| H1: HTTP server inside the runtime process | a thread like IPC, serving HTML and JSON | one process, direct access | network-exposed code inside the runtime; disappears on every deploy; a strong temptation to call Runtime internals directly; needs authentication and TLS in the runtime. Violates "UI is not runtime" in practice |
| H2: separate UI reading SQLite, writing via IPC | UI process plus read-only database access | survives restarts; no IPC changes for reads | a second interpreter of state, with release skew in derived states (W8); couples the UI to record shapes; tempts writes later |
| **H3: IPC as the single operator boundary** (read projections plus human input), with clients: CLI now, optional HTTP↔IPC adapter later | runtime computes every projection; clients are stateless | one authority for derivation; one audited mutation surface; the CLI works over SSH today with no new network exposure; a web adapter later is a thin translator | IPC must grow bounded read operations; the UI shows nothing while Kairo restarts (acceptable) |
| H4: no interface work (SSH plus sqlite3) | status quo | nothing to build | W1 and W2 remain: no supported way to give Kairo purpose; humans read raw records |

**Recommended: H3.** It is the only option where the runtime remains the sole authority over both state and its interpretation, and it adds no network exposure.

### Outbound interaction

| Option | Assessment |
|---|---|
| **E1: implementations plus the small additions in section 8** | satisfies every invariant; the additions are backward-compatible |
| E2: an integration subsystem (connectors, own credential store, scheduler) | a second executor and a second credential path; violates "no second executor" and "integrations are not agents" |
| E3: `process.run` only | no secrets path, no verification, no provenance; acceptable only as Kairo's root fallback, which it already is |

### Inbound events

| Option | Assessment |
|---|---|
| I1: listener inside the runtime | rejected (section 10) |
| I2: external adapter → IPC `event` operation, no buffer | recommended *when a real source exists*; no second store |
| I3: external adapter → spool directory | durable but a queue; only for non-retrying sources |
| **I4: polling through implementations, driven by cognition** | **sufficient now**; no new mechanism |

### Human confirmation of sensitive acts

| Option | Assessment |
|---|---|
| AP1: runtime-enforced approval gate | bypassable with root (`process.run`), so it is a false security boundary; adds state and a new lifecycle for actions |
| **AP2: coordination through a reply, Work `waiting` and a human message** | already supported; honest about authority; visible in Work history |

## 18. Recommended architecture

```
 human ──SSH──> kairo CLI ─┐
 (later: browser ─SSH tunnel─> stateless HTTP↔IPC adapter) ─┤
                                                            ▼
                         IPC (Unix socket 0600): the one operator boundary
       reads:  status · situation · chat(after seq) · history(after seq)
       inputs: message(id?) · directive.add · directive.deactivate
       controls: wake · stop
                                                            │ records, wake
                                                            ▼
   Runtime (lifecycle, persistence, execution, verification, deploy) ──> situation ──> cognition (any provider)
       │  actions: process.run · impl.<id>.<tool>/check · runtime.deploy
       ▼
   implementation packages (inert) ──> external systems
       KAIRO_ACTION_ID (idempotency) · secrets from EnvironmentFile · verify / checks
       output = untrusted observation, labelled
```

## 19. Minimum Phase 10 scope

### A. Required foundation

1. **IPC read projections computed by the live runtime.**
   - `situation`: the same structure cognition sees; read-only; bounded.
   - `chat`: messages after a `seq`, with a bounded count.
   - `history`: actions after a `seq`, bounded, redacted.
   - `status` gains: last cycle result and failure category; protocol version; operation list.
2. **IPC human input.**
   - `message` with an optional client id (idempotent).
   - `directive.add` and `directive.deactivate`, with `origin` and `deactivated_at` fields.
   - Each writes exactly one record and wakes Kairo.
3. **CLI client.** `python -m kairo.ipc` gains `chat` (show the conversation and send), `directives` (list, add, deactivate) and `situation`. This is the Phase 10 human interface, used over SSH.
4. **Untrusted-content framing.**
   - Instructions and the `history.actions` note separate the runtime facts about an action (it ran, its exit code, when) from its output content (untrusted data, never instructions).
   - Human messages are named as the only operator input.
5. **Implementation contract additions.**
   - `KAIRO_ACTION_ID` in the environment of tools and verify commands.
   - Documented guidance: external-effect tools must use it as their idempotency key and should ship a `verify` command.
6. **Credential provisioning.** `EnvironmentFile=-/etc/kairo/kairo.env` (root 0600) in the unit template, plus operator documentation: declared names only.
7. **Conversation data permissions.** The database will now hold human conversation, so make `/var/lib/kairo` 0750 and the database 0600 (`UMask=0077` in the unit, plus a one-time `chmod`).
8. **Tests** (section 25).

### B. Useful but optional

- Todo operations over IPC. README calls todo operator-maintained, but nothing uses it; decide whether todo stays (section 24).
- Per-tool `effects: read|external` manifest field.
- A stateless HTTP↔IPC adapter bound to localhost, reached through an SSH tunnel.

### C. Later

- Inbound events (`event` kind, IPC `event` operation, external adapters).
- Concrete integrations (email, GitHub, …) as implementation packages.
- Pushing notifications to the human outside chat (that is itself an integration).
- Knowledge and long-term memory, for conversation continuity beyond 20 messages.
- Multiple operators and operator identity.

### D. Explicitly not to implement

- An approval or permission subsystem.
- A pause state.
- A "run command" operation.
- Any UI that reads SQLite or holds authoritative state.
- An HTTP listener inside the runtime.
- Integration loops or connectors with their own executor.
- Cognition-created directives (open question).
- Provider-specific chat.
- MCP-style tools in providers.

## 20. Explicitly deferred scope

Everything in 19.C and 19.D, plus the Phase 9 deferrals, which remain open:

- release and snapshot retention;
- an independent verifier and trust architecture;
- migrations;
- a deployment dashboard;
- sandboxing and network isolation;
- package signing.

## 21. Architectural invariants

1. One runtime process per database (`flock`), and one lifecycle.
2. One persistence authority, `Memory`. Clients never open SQLite.
3. Cognition is not the runtime: providers are tool-less and side-effect-free.
4. Providers are not agents, the UI is not a runtime, implementations are not agents.
5. Timers and wakes are infrastructure, never purpose. Purpose comes from directives (operator), the world and messages.
6. Work and todo are representations, not sources of autonomy. The UI cannot edit Work.
7. Every effect on the world is a structured action, executed by `Runtime.act` and recorded before execution.
8. Verification is runtime-owned. Exit 0 is not success. Model claims are not evidence.
9. Failures stay observable. There are no blind retries; the repetition gate applies to integrations too.
10. No hidden loops: no component other than the runtime decides to act or wake on its own.
11. No duplicate state: one record per fact; projections are derived and never stored.
12. No provider-specific architecture.
13. No bypass of the runtime: every human input is a persisted record, every operational control is a lifecycle request.
14. Root authority stays intact; it is never presented as a security boundary.
15. External data is never trusted and never instructions. **Only the human channel carries operator authority; no other transport writes `message` records.**
16. External side effects need evidence (verify or check) before they count as verified; idempotency keys come from the action id.
17. Human input never becomes direct execution.
18. *(new)* Every projection a human sees is computed by the live runtime's code: no client-side derivation, so no release skew.
19. *(new)* Every mutating IPC operation writes exactly one self-describing record (time, origin) and nothing else.
20. *(new)* Credentials live outside the repository, the database, the situation and IPC responses. Packages receive only the names they declare.

## 22. Threat model

| Threat | Exposure | Mitigation | Residual |
|---|---|---|---|
| Malicious human input | the human channel is root-equivalent | socket 0600 plus SSH; the UI only through a tunnel or authentication | whoever controls the operator account controls Kairo (by design) |
| Malicious external content / prompt injection | output reaches cognition; Kairo has root | untrusted labelling; instructions; human channel separated from content | **not eliminated**: a convincing injection can lead to a harmful action. No sandbox |
| Compromised external service | lying responses | verification compares claims against independent observation where possible; untrusted labelling | a verify command that trusts the same compromised API is circular |
| Stolen credentials | secrets file, process environment | root 0600 file; per-package delivery; value redaction | Kairo (root) can read them; transformed values are not redacted |
| Replayed inbound events (later) | ingress | signature plus timestamp window in the adapter; deduplication by id | events from adapters without signatures are labelled unauthenticated |
| Duplicated outbound operations | crash or retry | `started` record → `interrupted`; repetition gate; action-id idempotency key | APIs without idempotency support can still duplicate after a deliberate retry |
| Stale external state | old observations | ages in the situation; re-observe before acting | judgment remains cognition's |
| UI compromise | root-equivalent surface | no UI in the minimum; adapter only on localhost through a tunnel; no execute operation | a compromised adapter can send messages as the operator |
| Provider compromise | decisions | strict decision parsing; structured actions only | a malicious provider can request any action Kairo can perform (root) |
| Implementation compromise | code runs as Kairo | provenance digest; per-package secrets; process groups | not sandboxed (Phase 8 limitation) |
| Runtime compromise | everything | – | total; same as Phase 9 |
| Accidental destructive cognition | root | reasons recorded; evidence; human coordination through Work `waiting` for irreversible acts (AP2) | not prevented |
| Crash between side effect and persistence | external acts | `started` record; interrupted state; idempotency; verify | non-idempotent APIs |
| Deploy during interaction | restart | messages persisted first; idempotent client retry; sequential actions; `KillMode=mixed` | IPC unavailable for seconds |

## 23. Future implementation boundaries

The modules each item would touch. They are identified here, not changed.

| Module | Change |
|---|---|
| `ipc.py` | new operations (`situation`, `chat`, `history`, `directive.add`, `directive.deactivate`), idempotent `message`, protocol version; CLI subcommands |
| `runtime.py` | read projections (bounded); `status` fields; `receive` with an optional id |
| `chat.py`, `directives.py` | optional id on `post`; `origin` and `deactivated_at` fields |
| `instructions.py`, `situation.py` | untrusted-content framing; directive origin shown |
| `environment.py` | `KAIRO_ACTION_ID` for implementation tools and verify commands |
| `deploy/kairo.service` | `EnvironmentFile=-/etc/kairo/kairo.env`, `UMask=0077` |
| `README.md` | operator interface, secrets file, external-effect package guidance |

Not touched: `work.py` semantics, `memory.py` schema, `deploy.py`, the providers.

## 24. Open questions

1. Is the human interface for Phase 10 the terminal over SSH (recommended), or is a browser required now (then 19.B's adapter becomes required, with a decision on access: SSH tunnel versus Caddy plus authentication)?
2. Should todo remain? It is operator-defined but unused, and directives plus messages cover influence. Either give it IPC operations or retire it explicitly.
3. Should cognition ever propose directives? This review keeps directives operator-owned. Kairo can suggest one in a reply.
4. Which external integration is first? It determines whether 19.B's `effects` field and an inbound path are needed in Phase 11.
5. Idle cognition cost: with directives, purpose exists. Should an operator be able to set a different default reassessment interval (an existing flag, `--reassess`) as configuration?
6. Chat retention and history: messages accumulate forever. Retention is the same housekeeping concern as releases and snapshots.

## 25. Testing strategy

Mandatory before Phase 10 is complete:

- **IPC boundary:**
  - every new operation;
  - malformed and oversized requests;
  - unknown operation;
  - mutating operations write exactly one record and wake Kairo;
  - no operation executes an action or touches Work or deployment, verified by checking that record counts do not change;
  - read operations are bounded in size and time.
- **Projection authority.** The IPC `situation` equals `build_situation(context())` inside the live process, and its `kairo.code.running` equals the process's release (the W8 regression).
- **Idempotency.** The same message id twice gives one record. Redelivering after a timeout gives no duplicate.
- **Directive lifecycle.** Add, then deactivate. Cognition sees changes on the next cycle. Fields survive restart. Records are readable by the previous release (tolerant reader) and the directive survives an older release rewriting it.
- **Restart and deploy.** Messages sent during a restart are persisted and answered by the successor. The CLI reports "unreachable" cleanly.
- **Untrusted content.** External output containing injection-style text is shown labelled as content; human messages and content are distinguishable in the situation. These are structural tests, not tests of model behaviour.
- **Implementation contract.** Tools and verify commands receive `KAIRO_ACTION_ID` equal to the action id. Existing packages still work (stdin unchanged).
- **Crash between side effect and persistence.** A fake external service records effects by idempotency key. The runtime is killed after the effect but before the result. Then check: `interrupted`; the identical retry is refused until reassessment; after reassessment the retry with the same key produces no duplicate; verify reports the real state.
- **Credentials.** A value from the `EnvironmentFile` reaches only the declaring package. It never appears in the database, situation, IPC responses or journal. `process.run` does not receive it.
- **Provider independence.** All of the above with a fake provider; no provider-specific code paths.
- **Production (systemd).** After installing the unit with `EnvironmentFile` and `UMask`, the permissions are as specified, restart, stop and fallback behave as before, and the CLI works over SSH.
- **Regression.** The full Phase 1–9 suite, 3 consecutive runs.
- **Mutation checks**, each caught by a test: a mutating operation writing twice; a read operation executing; a projection computed client-side; a missing action id; content labelled as operator input; a secret leaking into a response.

Later phases need contract tests per integration package (idempotency, verify, failure mapping), and authenticity, deduplication and ordering tests for event ingress.

## 26. Final architecture decision

Phase 10 is **"complete the operator boundary, and lay the foundation for external effects"**, not "dashboard plus integrations".

- **Human interface.** IPC becomes the single, complete operator boundary. It offers:
  - read projections computed by the live runtime (`status`, `situation`, `chat`, `history`);
  - human input that becomes persisted records cognition sees (messages, directives);
  - the existing operational controls (`wake`, `stop`).

  The first client is the terminal over SSH. A browser, if wanted, is a stateless HTTP-to-IPC translator added later behind an SSH tunnel or authentication. Nothing in the human interface executes, edits Work, touches deployment or stores state.
- **External interaction.** It remains ordinary actions backed by implementation packages, verified by the runtime. Phase 10 adds only:
  - the action id as an idempotency key;
  - a root-owned secrets file wired into the unit;
  - an explicit rule that external output is untrusted content, never instructions, and never operator authority.

  Inbound events have a defined shape (an `event` kind, deduplicated, written by an external adapter via IPC, with no buffer), and are not built until a real source exists. Human confirmation of irreversible acts uses chat plus Work `waiting`, not an approval gate.
- **Unchanged:** the runtime, lifecycle, persistence schema, Work semantics, provider contract, deployment and supervisor. The minimum scope adds no new record kinds and no tables.

**Verdict: ready for an implementation prompt with the scope in 19.A**, once the operator answers open question 1 (terminal or browser). That answer decides whether 19.B's adapter moves into the required scope.

## 27. Phase 10A implementation notes (corrections to this review)

Phase 10A implemented the operator boundary: items 1–3 of section 19.A, plus their tests. Where the implementation differs from the text above, this section takes precedence.

- **19.A split.** Items 4–7 (untrusted-content framing, `KAIRO_ACTION_ID`, `EnvironmentFile`, conversation-data permissions) belong to external interaction. They are deferred to the next Phase 10 subphase, which must do them before the first integration ships. Nothing in 10A depends on them.
- **Directive fields.** Directives carry `origin` and a bounded `history` (`created`, `deactivated`, `activated`, each with time and origin) instead of a single `deactivated_at`. History keeps every change auditable, including reactivation.
- **`directive.activate` added.** It undoes a mistaken deactivation without minting a new id, so Work linked to the directive keeps its meaning.
- **`directives` read operation added.** The situation shows only active directives, but the operator must see inactive ones to activate them.
- **No `history` (paged actions) operation.** The situation already shows recent actions with derived states, and Work attempts show what Kairo did for each pursuit. Paging deeper is deferred until an interface needs it.
- **`status` additions:**
  - `open_work`;
  - `cognition_last.result`, `.failure` and `.at`, so an unanswered message can be explained;
  - `protocol: 2`;
  - `ops`.
- **Stricter request validation.** IPC now rejects unknown request fields (`invalid_params`); before, they were ignored. Every error carries a `code`.
- **Idempotency** uses the message record as its own state (the record id is derived from the client id), so no separate store or cleanup is needed. Its size bound is the conversation itself.
- **`--situation` fix (W8).** The command probes the database lock.
  - If a runtime owns the database, it fetches that runtime's situation over IPC.
  - Otherwise it builds a preview whose `kairo.code.running` names no release, instead of the invoking process's code.
- **Operator stops are recorded** as the lifecycle reason `stop requested over ipc`, unless a deployment restart already set the reason.
- **Unchanged:** the cognition contract, Work semantics, the persistence schema (no new kinds or tables), deployment, the supervisor, and the provider layer.
