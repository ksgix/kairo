# Self-maintenance

Kairo maintains its own code as **ordinary work**. By default it changes code only for a concrete reason (an observed failure, a verified defect, a capability real work needs), never because it is idle. An active directive whose statement or description asks for improving Kairo's own code authorises proactive improvement within what it describes. There is no maintenance agent, queue, scheduler or work type. Cognition inspects, edits, tests and commits Kairo's source with ordinary `process.run` actions in a **development repository**, and the existing recovery rules apply (failure facts, strategy revisions, no blind repetition). The one new action, `runtime.deploy`, is for the step those actions cannot do: making a committed revision the code Kairo runs.

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

