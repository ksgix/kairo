# Operator interface

A human reaches Kairo only through IPC: the Unix socket of the live runtime, reached locally (for example over SSH). The terminal client `python3 -m kairo.ipc` is an adapter: it sends one request per command and prints what the runtime answered. It reads no database, keeps no state and decides nothing. If it disappeared, Kairo would be the same runtime.

**Commands** (production: add `--socket /var/lib/kairo/kairo.sock`; `--json` prints raw responses):

| Command | Operation | What it is |
|---|---|---|
| `status` | `status` | the live runtime: state, reason, identity, starts, revision, counts, last cognition result, protocol version, operations |
| `situation` | `situation` | exactly what cognition would be shown now, computed by the live runtime |
| `chat [--limit N] [--after SEQ]` | `chat` | the conversation, human messages and Kairo's replies, in order, with sequence numbers |
| `message TEXT [--id ID]` | `message` | a human message: persisted, then Kairo wakes. The answer comes later, in `chat` |
| `directives` | `directives` | all directives, active and inactive, with origin and history |
| `directive add STATEMENT --description TEXT` | `directive.add` | a new lasting area of responsibility: the purpose and what it covers |
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
  - A description (required, 1–4,000 characters) says what the purpose covers: intent, scope, expectations, boundaries. Kairo decides the concrete work itself. Directives created before descriptions existed show none.
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
  - no paged action history (the situation shows recent actions);
  - no network listener: the socket is local. The [dashboard](dashboard.md) is a separate HTTP adapter over these same operations.

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
- The [dashboard](dashboard.md) is a client of this socket: it runs as the same account and adds its own login.

