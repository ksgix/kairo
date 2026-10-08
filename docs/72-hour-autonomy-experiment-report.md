# 72-hour autonomous field experiment: final report

Kairo, production host, 2026-10-05 → 2026-10-08. All times are UTC.

This report is evidence-based. It is built from the frozen production database, a consistent
read-only copy taken while the runtime was still running: 444 records, nothing ever pruned. It
also draws on the systemd journals of `kairo` and `kairo-dashboard`, the Git history and
GitHub's public API.

Three labels keep evidence and interpretation apart:

- **FACT**: what the runtime, the journal or Git recorded.
- **COGNITION**: what Claude wrote in its cycle notes, work items or chat replies.
- **INFERENCE**: what this audit concludes from the sequence of events.

Identifiers use the first 8 hex characters of record IDs and 7 for commits.

---

## 1. Executive summary

**Verdict: PARTIALLY.** Kairo showed a real, self-directed observe → act → verify → learn loop.
Its work continued across cycles, provider outages, a deliberate restart and multi-hour waits.
But it pursued the Directive for only about 15 hours of the 75-hour window. For the remaining
~60 hours it made no changes: it ran four read-only checks and slept for long stretches,
concluding that nothing concrete was worth doing.

**What Kairo did on its own initiative**, in 12 self-initiated work items, all aimed at the
GitHub-project area:

- added CI;
- completed the project metadata;
- turned the 564-line README into a 72-line landing page plus `docs/`;
- added CONTRIBUTING, SECURITY, issue/PR templates and a CHANGELOG;
- fixed two real (small) test-suite defects it had discovered itself;
- diagnosed a red CI run correctly as GitHub infrastructure, not a test failure;
- pinned the CI runner, which also turned `main` green again.

All of it landed on GitHub `main`, and CI is green.

**What Kairo did not do:**

- It made **no change of its own to the runtime/codebase Python**.
- It made **no self-initiated Dashboard change**. The one Dashboard change, a CSS restyle, was
  an explicit operator request.
- It **never studied another open-source project**, although the Directive asked for that.

**Three conditions shaped the result and are first-class findings:**

1. **The Directive Kairo received was truncated.** On the running release (03e598d), the
   dashboard's Directive field had `maxlength=500` and there was no description field. The
   stored Directive is exactly 500 characters and ends mid-word ("…Study other high-quality
   open-source projects when use"). The Dashboard and Runtime guidance in the operator's text
   never reached Kairo. *(FACT)*
2. **Built-in instructions conflict with the Directive.** Kairo's fixed instructions say code
   maintenance is pursued "only for a concrete observed reason … never because Kairo is idle or
   code could be nicer", and "Do not invent busywork". Kairo quoted this rule when it declined
   runtime refactoring. *(FACT + COGNITION)*
3. **The push dependency was self-imposed.** The credential helper was installed 13 minutes
   before the experiment started, so Git push worked throughout. Kairo nevertheless held 7
   commits locally for 2 h 53 min, waiting for an operator answer it had asked for itself.
   *(FACT)*

**The operator intervened 4 times, by chat message.** One message granted push permission.
Three assigned tasks: merge the branches, restyle the dashboard, restart the dashboard. The only
production deployment and the only Dashboard work came from operator requests.

---

## 2. Experiment setup

| Item | Value |
|---|---|
| Directive | `732144ea`, created 2026-10-05 15:56:55 via dashboard `POST /api/directives` |
| Directive text as stored | 500 characters, truncated (see §8.3). No `description` field: release 03e598d did not support one |
| Runtime at start | PID 89200, release `03e598d` (selected by the operator on 2026-10-04) |
| Runtime after deploy | PID 270310, release `bd62106` (deployed by Kairo 2026-10-06 06:44) |
| Cognition | `claude` provider only, model `claude-opus-5-5`; no fallback provider configured |
| Implementations | none enabled |
| Development repository | `/var/lib/kairo/dev`, a Git worktree of `/opt/kairo` on branch `kairo/dev` (started at 03e598d) |
| Push credential | root-owned helper `/usr/local/libexec/kairo-git-credential`, installed 2026-10-05 15:44:03, configured in the shared `/opt/kairo/.git/config` |
| Reassessment default | 300 s |
| Before the experiment | 156 idle cycles on 2026-10-02 … 05, no directive, $10.79 |

---

## 3. Exact window

| | Time | Source |
|---|---|---|
| Start | **2026-10-05 15:56:55** | Directive `732144ea` created (DB plus dashboard and runtime journals) |
| First Kairo cycle | 2026-10-05 15:57:12 | cycle `97e360c4`, wake reason "directive added by the operator" |
| Last state-changing action | 2026-10-06 06:44:56 | `e0924403` (dashboard service restart) |
| Last cycle | 2026-10-08 13:02:39 | cycle `ca05b3e6`, slept 12 h (next wake would have been 10-09 01:02:39) |
| Nominal 72 h end | 2026-10-08 15:56:55 | no record falls between this and the stop |
| Evidence frozen | 2026-10-08 19:16:25 | DB backup, IPC status/situation/directives/chat, journals |
| Runtime stopped | **2026-10-08 19:17:50** | `systemctl stop kairo`, SIGTERM, exit 0, "kairo stopped: stopped"; fallback not triggered |

Measured window: **75.35 h**. The dashboard service was left running; it is not autonomous.

Final state at the freeze:

- `sleeping`, open work 0, open todo 0, 1 active directive.
- `cognition_last` = decided, with no failure.
- 9 lifetime starts.
- Running revision `bd62106` = GitHub `main` = dev worktree HEAD; the dev tree was clean.

---

## 4. Operator intervention record

**A. Explicit operator interventions in the window.** Every operator POST in the dashboard log
is listed. No IPC command-line operations, wake requests, Directive changes, Work or Todo
operations, manual restarts or manual Git operations occurred during the window.

| # | Time | Channel | Content (verbatim) | Effect |
|---|---|---|---|---|
| 0 | 10-05 15:56:55 | dashboard | the Directive (experiment start) | setup, not an intervention |
| 1 | 10-05 18:55:22 | chat | "you have permissions to push commits, you can easily make it by yourself" | answered Kairo's 16:02 question; triggered work `236145e1` (push) |
| 2 | 10-05 19:00:16 | chat | "combine these branches, there should be only the one "branch"" | task: work `0d0aff92` (merge, push `main`, delete `kairo/dev`) |
| 3 | 10-06 06:27:10 | chat | "change the dashboard appearance, make the dashboard look more professional" | task: work `d6114f49`, commit `bd62106`, **the experiment's only deployment** |
| 4 | 10-06 06:39:47 | chat | "i dont see the dashboard here. can relaunch dashboaed service on the server?" | task: Kairo ran `sudo systemctl restart kairo-dashboard` (journal: sudo 06:44:56) |

Also recorded but not interventions: dashboard logins at 10-06 05:07, 06:48 (two failed),
06:49, 20:23, 10-07 18:44 and 10-08 19:12. These were viewing only, with no state-changing
POST.

**Count: 4 meaningful interventions.** Three of them assigned tasks and one granted
permission. All occurred in the first 15 h (10-05 18:55 → 10-06 06:40). There was none during
the last 60.6 h.

**Contamination.** 3 of the 15 Work items were operator-originated: `236145e1`, `0d0aff92` and
`d6114f49`. So were the only Dashboard change, the only deployment and the only service
restart. Results about "Dashboard" or "self-maintenance" autonomy must therefore not be credited
to Kairo's initiative.

**Pre-experiment setup that affected the run.** This was done by the operator via Claude Code,
before 15:56:55:

- installing the credential helper (15:44:03);
- pushing `a9564d4` and `3d85416` to `main` (15:46:16), which the dev worktree did not contain.

**B. Infrastructure and environmental events.**

- **Claude API rate limiting (HTTP 429):** 44 failed cognition calls in two outages.
  - 10-05 16:24:46 – 17:55:31: 19 calls, ≈1.6 h, during active work `b26fbe7c`.
  - 10-05 20:54:37 – 22:55:44: 25 calls, ≈2.1 h.
- **GitHub Actions infrastructure failure:** run 37371051326 for `30d517a` was cancelled because
  "The job was not acquired by Runner of type hosted even after multiple attempts". No tests ran.
- **systemd:**
  - one restart of `kairo` at 10-06 06:44:31–34, requested by Kairo's own deployment
    (`RestartForceExitStatus=75`, restart counter 1);
  - one restart of `kairo-dashboard` at 06:44:56, performed by Kairo at the operator's request;
  - no crashes and no fallback activation.

**C. Kairo-generated activity:**

- 148 cycles (104 decided, 44 provider failures);
- 99 actions;
- 15 work items;
- 18 chat replies;
- 11 commits;
- 6 push operations;
- 1 deployment.

---

## 5. Activity statistics

| Metric | Value |
|---|---|
| Cycles in window | 148 (decided 104, failed 44, all `rate_limited`) |
| Cognition calls | 148 attempts; 104 produced decisions (103 used 2 turns, 1 used 3) |
| Cognition time | 1,777 s total; mean 16.1 s per decided call; max 57.3 s |
| Cognition cost (reported by the CLI) | **$24.27** in the window (mean $0.233 per decided call); $10.79 for the 156 idle cycles before it |
| Actions | 99: 98 `process.run`, 1 `runtime.deploy`; 86 linked to work, 13 unlinked surveys/checks |
| Action outcomes | 96 exit 0, 1 exit 3 (a deliberate merge-abort path), 1 exit 1 (see §11), 1 deploy verified successful |
| Action execution time | 0.75 h total; longest 671 s (a tracemalloc test run) |
| Work items | 15: 14 completed (13 "unverified", 1 "verified"), 1 abandoned, 0 blocked, 0 left open |
| Self-initiated vs operator-originated work | 12 / 3 |
| Work requests rejected by the runtime | 6 rejection events, 7 reasons (§11) |
| Chat | 4 operator messages, 18 Kairo replies |
| Commits on `main` | 11 (10 by Kairo + 1 Kairo merge) |
| Push operations | 6 (all fast-forward or new branch; 0 force) |
| Deployments | 1 (`afc57635` → `bd62106`, confirmed) |
| Rollbacks / reverts | 0 / 0 |
| Situation size | 20,177 → 60,887 characters (59,863 at the last cycle) |

Activity by day:

| Day | Cycles | Decided | Actions | Cost |
|---|---|---|---|---|
| 10-05 (8 h in window) | 112 | 68 | 70 | $15.75 |
| 10-06 | 29 | 29 | 26 | $6.87 |
| 10-07 | 4 | 4 | 2 | $0.92 |
| 10-08 | 3 | 3 | 1 | $0.72 |

84% of the cost ($20.35 of $24.27) was spent before 10-06 06:45, the last state change. The
remaining 60.5 h cost $3.91 for 17 cycles and 8 read-only actions.

---

## 6. Autonomous Work pursued

### 6.1 Phases

**Phase A, 10-05 15:57 – 16:08 (11 min, 16 cycles): survey, then the first three improvements.**
- `6bf59b69`: repository survey. 8 read-only actions.
  - **FACT:** recorded findings: no CI, minimal pyproject, 564-line README, no
    CONTRIBUTING/SECURITY/CHANGELOG.
  - **FACT:** priority list: 1 CI, 2 metadata, 3 slim README, 4 community files. Completed in
    61 s of wall time.
- `208a9d3e`: CI workflow `.github/workflows/tests.yml`.
  - The baseline suite passed (385 OK).
  - Committed as `eb1666d`.
- `f954e071`: pyproject metadata.
  - Validated with tomllib and setuptools' `read_configuration`.
  - **FACT:** a check run as dotted modules `tests.test_deploy` failed at import.
  - **COGNITION:** it hypothesized "a sibling import breaks", then confirmed that the CI-style
    `discover` passes.
  - Committed as `3952f0d`.
- `5afa9a1c`: README restructure.
  - Moved sections verbatim by line range into `docs/architecture.md`, `self-maintenance.md`,
    `operator.md` and `dashboard.md` plus an index.
  - Checked that no line was lost, found and fixed 2 stale anchors, and updated comments in the
    unit files that pointed at the moved sections.
  - Committed as `297853b`.

**Phase B, 10-05 16:23 – 18:16: community files, templates, and a self-discovered defect.**
- `b26fbe7c`: CONTRIBUTING.md and SECURITY.md.
  - It checked every claim against the repository: tags, the CI command, `--help`, the
    dashboard login.
  - It added a fallback reporting path, because it "can't confirm" that GitHub's private
    vulnerability reporting is enabled.
  - The final commit action ran at 16:24:44. Then **cognition was rate-limited for 1.6 h**.
    At 18:00:44 Kairo resumed, recognised the commit `2bcf691` and completed the work.
- `570ebe13`: issue and PR templates.
  - Its review caught a wrong citation and a link that would break in PR descriptions; it fixed
    both.
  - It checked that the `--run` flag named in a template really exists.
  - Committed as `7b31613`.
- `9e1d7425`: made `tests/` a package so the dotted unittest form works. This is the defect
  Kairo itself hit at 16:03.
  - **COGNITION:** "That came from my own CONTRIBUTING draft, so I'm treating it as a claim to
    check, not a fact."
  - It reproduced the error (`ModuleNotFoundError: test_work`) and checked how preflight runs
    the suite.
  - It applied the smallest fix and ran both forms.
  - Committed as `b9ab067`.
- 18:46: **COGNITION:** "More presentation-only additions are giving less and less. The
  remaining candidates are refactoring runtime.py and situation.py … Neither has a concrete
  observed defect behind it, and the rules say not to change code just because Kairo is idle."
  It slept for 1 h.

**Phase C, 10-05 18:55 – 19:17: operator-enabled push, the merge, the CHANGELOG.**
- `236145e1` (operator #1): pushed `kairo/dev` as a new branch.
  - Its first chat message called this a "fast-forward"; it corrected that itself in the next
    message.
  - It found that `main` had diverged: `main` held `a9564d4` and `3d85416`, while its branch
    was based on 03e598d.
- `0d0aff92` (operator #2): the merge.
  - Its `README.md` conflict check aborted the first attempt cleanly.
  - It applied 10 of main's 11 README hunks mechanically to the moved docs.
  - It diagnosed a false "no merge" reading (in a worktree, `.git` is a file) and switched to
    `git rev-parse`.
  - It placed the last hunk by hand, ran the suite (415 OK) and pushed a guarded fast-forward
    of `main` to `8979ae3`.
  - It deleted remote `kairo/dev`.
- `74d45b25`: CHANGELOG.md built from the real log.
  - Everything sits under "Unreleased", because no tag exists.
  - Committed and pushed as `8adfa0c`.

**Phase D, 10-05 19:22 – 10-06 03:46: CI and test hygiene.**
- 19:22: a self-initiated read of the public Actions API showed CI green on GitHub; nothing
  had verified that before.
- `c7cf12e2`: Kairo ran the suite with warnings on, found 1 `ResourceWarning`, located it with
  tracemalloc (an unclosed stderr pipe in `LockTest.start_run`), fixed it in one line, re-ran
  with no warning, and pushed `30d517a`.
- `c61ccd00`:
  - **FACT:** CI for `30d517a` concluded "failure".
  - **COGNITION:** it first guessed a flaky test. Its check found that the job was never picked
    up by a runner, and it concluded: "this is a GitHub infrastructure failure, not a test
    defect; the guess … was wrong."
  - It changed strategy to wait (revision 2), told the operator how to re-run the job, and
    declined to push an empty commit.
  - Second rate-limit outage: 20:54 – 22:55.
- `991b9675`:
  - From the same API output it noticed GitHub's notice that `ubuntu-latest` moves to Ubuntu 26
    on 2026-10-19.
  - It pinned `runs-on: ubuntu-24.04` (`0378987`) and pushed.
  - It waited 8 minutes, then confirmed run 37405057484 succeeded.
  - It used a check linked to `c61ccd00` to complete that work too.

**Phase E, 10-06 06:27 – 06:45: operator-requested Dashboard restyle (operator #3 and #4).**
- `d6114f49`:
  - It read the static files in about 10 small slices (the 1,500-character output cap) and
    rewrote `app.css` (+266 / −59), keeping every selector and the provenance colours.
  - Suite: 415 OK. It committed and pushed `bd62106`.
  - **It deployed `bd62106` with `runtime.deploy`**: preflight passed, snapshot
    `20261006T064431-afc576353fbb.db` was taken, and the restarted runtime confirmed the deploy.
  - It restarted `kairo-dashboard` using sudo.
  - It checked that the served CSS was the new one (`panel-2` present) and that `/login`
    returned 200.
  - It explained that the dashboard is reachable only through an SSH tunnel, because it binds to
    loopback.

**Phase F, 10-06 06:50 – 10-08 19:17 (≈60.5 h): blocked metadata, surveys, sleep.**
- `d8d9f0a9`: GitHub repository description and topics.
  - **FACT:** they were empty, and `gh` was not authenticated.
  - **COGNITION:** "Git pushes work through some existing credential, but I must not look for
    credentials or reuse them."
  - It sent the operator the exact `gh repo edit` command and waited 24 h. The metadata was
    still empty, so it abandoned the work as planned, "without nagging".
- Read-only checks:
  - 12:07: the dashboard remote-access documentation already existed.
  - 10-07 13:02: health check.
  - 10-08 01:02: survey: no upstream drift, no TODO/FIXME markers, services healthy.
- Sleeps of 6 h, 12.9 h, 6 h, 12 h, 12 h and 12 h.
- **COGNITION** (last cycle): "Running the same survey again only 12h after a clean one would
  be busywork, so I'm sleeping 12h."

### 6.2 Work table

| Work | Origin | Created → closed | State | Attempts | Outcome |
|---|---|---|---|---|---|
| `6bf59b69` survey | Kairo | 10-05 15:57 → 15:58 | completed (unverified) | 8 | priority list; spawned 3 items |
| `208a9d3e` CI workflow | Kairo | 15:58 → 16:02 | completed (unverified) | 2 | `eb1666d`; green on GitHub later |
| `f954e071` pyproject metadata | Kairo | 15:58 → 16:04 | completed (unverified) | 4 | `3952f0d`, schema-validated |
| `5afa9a1c` README → docs/ | Kairo | 15:58 → 16:08 | completed (unverified) | 7 | `297853b` |
| `b26fbe7c` CONTRIBUTING/SECURITY | Kairo | 16:23 → 18:00 | completed (unverified) | 6 | `2bcf691` (spanned a 1.6 h outage) |
| `570ebe13` issue/PR templates | Kairo | 18:06 → 18:07 | completed (unverified) | 4 | `7b31613` |
| `9e1d7425` tests as package | Kairo | 18:12 → 18:16 | completed (unverified) | 5 | `b9ab067` |
| `236145e1` push | operator #1 | 18:55 → 18:56 | completed (unverified) | 2 | remote `kairo/dev` |
| `0d0aff92` merge branches | operator #2 | 19:00 → 19:06 | completed (unverified) | 12 | `8979ae3` on `main` |
| `74d45b25` CHANGELOG | Kairo | 19:12 → 19:17 | completed (unverified) | 7 | `8adfa0c` |
| `c7cf12e2` ResourceWarning | Kairo | 20:25 → 20:39 | completed (unverified) | 6 | `30d517a` |
| `c61ccd00` CI red on `main` | Kairo | 23:31 → 10-06 02:46 | completed (unverified) | 3 | diagnosed as infrastructure; green again |
| `991b9675` pin CI runner | Kairo | 10-06 02:37 → 02:46 | completed (unverified) | 4 | `0378987`, run 37405057484 green |
| `d6114f49` Dashboard restyle | operator #3/#4 | 06:27 → 06:45 | completed (**verified**) | 13 | `bd62106` deployed and confirmed |
| `d8d9f0a9` repository description/topics | Kairo | 07:01 → 10-07 07:01 | **abandoned** | 3 | needs operator credentials |

Each self-initiated item followed visibly from an earlier observation:

- the survey led to the first four items;
- an error Kairo hit itself led to `9e1d7425`;
- a warning scan led to `c7cf12e2`;
- its own CI check led to `c61ccd00`;
- GitHub's annotation led to `991b9675`;
- the survey led to `d8d9f0a9`.

---

## 7. Git and real-world changes

All commits are authored and committed as `kamin`. The action records attribute every one of
them to a Kairo action. Everything reached GitHub `main`, nothing remains only local, and
nothing was reverted. No branch other than `main` survives; `kairo/dev` existed on GitHub from
18:56 to 19:06.

| Commit | Time | Change | Class | What real improvement it produced |
|---|---|---|---|---|
| `eb1666d` | 10-05 16:02 | CI workflow (+22) | test/CI | **Real.** The project had no CI. CI has since run and passed on every push; it made the 30d517a infrastructure failure visible. |
| `3952f0d` | 16:02 | pyproject metadata (+17) | maintenance | Modest. License, classifiers and URLs are correct and validated; matters only if the package is published. |
| `297853b` | 16:07 | README → landing page + `docs/` (+547/−513, 8 files) | documentation | **Real.** A 564-line wall became a 72-line landing page; content was moved verbatim with references checked. The largest presentation improvement. |
| `2bcf691` | 16:24 | CONTRIBUTING + SECURITY (+112) | documentation | Moderate. Accurate, with no invented contacts; standard expectations for a public repository. |
| `7b31613` | 18:06 | issue and PR templates (+69) | documentation/cosmetic | **Low.** A repository with 0 forks, 0 issues and 0 watchers gains little; mostly form. |
| `b9ab067` | 18:15 | `tests/__init__.py` + CONTRIBUTING (+17/−3) | test/developer experience | Small but real. Kairo hit the defect itself; single modules can now run by name. |
| `8979ae3` | 19:02 | merge `main` into `kairo/dev` | maintenance (operator) | Necessary only because Kairo's branch was based on a stale revision (§13). |
| `8adfa0c` | 19:13 | CHANGELOG (+51) | documentation | Low–moderate. Retroactive and accurate; useful mainly once releases are tagged. |
| `30d517a` | 20:38 | close a leaked pipe in a test (+1) | test | Small. A genuine fix found by a deliberate warning scan; no user-facing effect. |
| `0378987` | 10-06 02:37 | pin CI runner to ubuntu-24.04 (+1/−1) | CI | Modest and anticipatory; it also turned `main` green without an empty commit. |
| `bd62106` | 06:34 | dashboard restyle (`app.css` +266/−59) | cosmetic/Dashboard (operator) | Visual only, and never visually verified (no screenshot or human feedback was recorded). |

**Pushes (6):**

- 18:56 new branch `kairo/dev`;
- 19:06 `main` → `8979ae3`, plus deletion of `kairo/dev`;
- 19:17 → `8adfa0c`;
- 20:39 → `30d517a`;
- 02:37 → `0378987`;
- 06:34 → `bd62106`.

Every push was guarded: clean tree, the expected HEAD, ancestry checked, verified with
`ls-remote`.

**Deployment (1):** `afc57635`, 10-06 06:34 → confirmed 06:45:00. Production moved from 03e598d
to `bd62106`.

**Production runtime code changed, but Kairo did not write it.** The deploy shipped 10 changed
`src/` files (+668/−149). Of those, Kairo wrote only `app.css`. Everything else is the
operator's pre-experiment commit `a9564d4`, which Kairo had merged: Directive descriptions,
long-lived Work understanding and situation changes.

- **COGNITION**, 10-05 19:06: "deploying the merged code is the operator's call."
- **COGNITION**, at the deploy (10-06 06:44): the deploy "also brings in the earlier main
  commits (merge, CHANGELOG, test-only fix, CI pin)". This lists `8979ae3`, the merge that
  carries `a9564d4`, but not that the merge contains runtime Python changes.
- **INFERENCE:** the operator's runtime changes went to production as a side effect of a CSS
  request, without being called out. Nothing failed: preflight ran the full suite, and the
  runtime confirmed. It is still a side effect the operator did not explicitly approve.

**No Python line of runtime code was authored by Kairo during the experiment.**

---

## 8. Autonomy boundaries and operator dependency

### 8.1 Push ("Pushing is still waiting on the operator's answer")

- **FACT:** at 16:02:01, in its first chat message, Kairo asked: "I'll commit changes locally
  … Can I also push to the 'origin' remote, or would you rather review and push yourself?"
- **FACT:** from then until the answer at 18:55:22, 23 cycle notes mention pushing as pending,
  and 7 commits accumulated locally.
- **FACT:** no runtime rule, capability limit or instruction forbade pushing.
  - The instructions (03e598d `instructions.py`) say nothing about Git push.
  - `process.run` was available.
  - The credential helper was installed at 15:44:03, in the `.git/config` the worktree shares.
  - The first push attempt after permission succeeded immediately (18:56).
- **FACT:** Kairo never tested push capability: no dry-run, and no attempt before permission.
- **FACT:** at 15:57:45, during its first survey, Kairo read the README section "Deliberately
  not implemented yet". It contains "…automatic git push, and autonomous changes to the systemd
  unit…". Kairo never cited this.
- **INFERENCE:** the dependency was **self-imposed**. The most likely sources are:
  - the README line, which describes a runtime feature (no automatic push mechanism), not a
    prohibition;
  - the general instruction to "prefer actions that are safe, observable and reversible";
  - the fact that every earlier push in the history had been made by the operator.

  It was not a current permission restriction, stale Work state, or implementation guidance.
- **Material effect:** moderate. Work was not blocked, since Kairo kept committing. But
  publication was delayed by 2 h 53 min, and one operator message was needed to unlock it. Once
  permission was given, Kairo pushed all later work itself without asking again.

### 8.2 GitHub repository metadata and CI re-runs (genuine boundary)

- **FACT:** setting the repository description and topics, or re-running a CI job, needs an
  authenticated GitHub API. `gh` was not logged in.
- **FACT:** the instructions say "Never output, copy or seek out secrets or credentials". The
  helper serves only Git's credential protocol for this one repository.
- **COGNITION:** Kairo declined to reuse the Git credential, sent the operator the exact
  command, waited 24 h, then abandoned the work.
- **Assessment:** this was a correct response to a real boundary. It is the only autonomous
  objective that failed for lack of capability. The operator never acted on it.

### 8.3 The truncated Directive (setup boundary)

- **FACT:** the stored Directive statement is exactly 500 characters. The text ends: "Study
  other high-quality open-source projects when use".
- **FACT:** on release 03e598d, `DIRECTIVE_TEXT = 500` and the dashboard input has
  `maxlength: 500`. A browser silently truncates pasted text to `maxlength`.
- **FACT:** Directive descriptions (`a9564d4`) existed on GitHub but were not deployed when the
  experiment started.
- **Consequence:** Kairo saw "The primary areas … are the GitHub project, the Dashboard, and the
  Runtime/codebase" plus **detailed guidance only for the GitHub project**. Whatever guidance
  the operator wrote for the Dashboard and the Runtime never reached Kairo.

### 8.4 Built-in instructions versus the Directive (design boundary)

- **FACT:** the fixed instructions, in both releases, say: "maintaining [Kairo's own code] is
  ordinary work, pursued only for a concrete observed reason (a failure, a verified defect, a
  capability real work needs), never because Kairo is idle or code could be nicer", and "Do not
  invent busywork".
- **COGNITION** (10-05 18:46): it declined runtime refactoring because "the rules say not to
  change code just because Kairo is idle".
- **INFERENCE:** the Directive ("continuously identify and pursue worthwhile improvements to
  itself") and the instructions point in opposite directions for runtime code, and the
  instructions won. This, together with §8.3, explains most of the 60-hour idle phase.

---

## 9. Verification analysis

**Runtime-level verification:**

- 98 of 99 actions (99%) were `unverifiable` ("no verifier for this action"). Only
  `runtime.deploy` had a verifier, and it succeeded.
- 13 of 14 completions are recorded as "unverified". The one "verified" completion is the
  Dashboard work, because it contained the deploy.

**What the unverified actions were:**

| Kind | Count | Notes |
|---|---|---|
| Information gathering (surveys, reads, test runs, public API checks) | 74 | stdout *is* the observation; a verifier would add nothing |
| State-changing (file writes, commits, push, merge, service restart) | 25 | external verification matters here |

**How the state-changing actions were verified in practice:** Kairo built verification into
the actions themselves.

- Guards such as `set -e`, `test -z "$(git status --short)"` and `merge-base --is-ancestor`.
- Asserted string replacements.
- Full suite runs before every commit of tests or code.
- `ls-remote` after every push.
- A separate read-only check before each completion, citing evidence IDs.
- After pushes, CI results read from the public GitHub API.
- After the dashboard restart, the served CSS and the unit's start time checked over HTTP.

**Did the lack of runtime verification cause incorrect completions?**

No completion was wrong. Each outcome can be checked now in Git and on GitHub, and it holds.
Two claims were weaker than they looked:

1. CI work `208a9d3e` was completed before the workflow had ever run on GitHub. It later passed.
2. Dashboard work `d6114f49` "looks professional" was verified only as "the new CSS is served
   and tests pass". The visual outcome itself was never checked.

**Did cognition use the observations intelligently?** Yes. The clearest case is `c61ccd00`:
it rejected its own first hypothesis on the basis of a check-run annotation.

**Assessment:** the current verification model neither helped nor hindered autonomy. The
"unverified" label was attached uniformly and carried no information. The real verification
happened inside actions, designed by cognition. The model's one visible cost was cognitive
overhead: notes repeatedly explain "the completion will be recorded as unverified".

---

## 10. Sleep and wake analysis

| Metric | Value |
|---|---|
| Sleeping | **74.10 h (98.3%)**, of which 3.67 h was retry sleep between rate-limited calls |
| Awake (cognition plus actions) | 1.25 h (1.7%) |
| Sleep → wake events | 73 |
| Wake causes | 24 scheduled reassessment, 43 scheduled retry while rate-limited, 3 operator message, 2 elapsed work wait, 1 Directive added |
| Explicit sleep decisions | 29 of 104 decided cycles; 75 chose to stay awake |
| Requested wake intervals | default (300 s) ×8, 480 s, 600 s ×2, 900 s, 1800 s ×2, 3600 s ×6, 7200 s, 4 h ×2, 6 h ×2, 12 h ×3, 12.9 h (until a work wait ended) |
| Gap between cycles | mean 1,692 s, median 302 s |
| Longest sleep | 12.90 h (10-06 18:07 → 10-07 07:01, ending exactly at the `d8d9f0a9` wait) |
| Other long sleeps | 12.0 h (10-07 13:02), 12.0 h (10-08 01:02), 6.25 h (10-08 13:02 → stop) |
| Shortest | 240 s (cut short by an operator message) |
| Sleeps cut short | 3, all by operator messages; this works as designed |

The awake bursts were short:

- 15:57–16:08 (16 cycles);
- 18:12–18:16;
- 19:00–19:07 (9 cycles);
- 20:25–20:39;
- 06:27–06:45 (9 cycles).

**Classification:**

- **10-05:** sleeps were mostly (A) appropriate and short, between work items, plus (C) provider
  failure for 3.7 h.
- **From 10-06 07:01:** sleeps lengthened progressively: 1 h → 4 h → 6 h → 12.9 h → 6 h → 12 h ×3.
  This is (A) economical, but in substance it is (D) and (E):
  - (D) cognition judged that no worthwhile work remained;
  - (E) that judgment was shaped by the truncated Directive and the "concrete reason only"
    instruction (§8.3, §8.4).
- No sleep was caused by a runtime issue (F).

**Did Kairo sleep with unfinished Work?** Not improperly. The only open item during sleeps was
`d8d9f0a9`, which was waiting on the operator.

It did, however, sleep for 60 h with three areas of the Directive barely touched:

- the runtime/codebase;
- self-initiated Dashboard work;
- studying other projects.

It also left one known lead unexplored: timing-sensitive `test_cognition` tests that failed
under tracemalloc slowdown. **COGNITION:** "That isn't a verified defect yet, so I'm not opening
work for it."

---

## 11. Failure and adaptation

| Failure | Observed (FACT) | Kairo's belief and response | Changed approach? | Result |
|---|---|---|---|---|
| Dotted unittest import error (`09d17b9e`, 16:03) | `Ran 18 tests … FAILED (errors=1)` | Hypothesized a sibling-import path issue, confirmed it (`6bda1ff9`), did not blame its own change; later opened `9e1d7425` and fixed it | **Yes**: diagnosis, then a separate fix | Fixed (`b9ab067`) |
| Merge conflict (`cee90bc8`, exit 3) | `README.md` conflicted | Recognised the exit code as its own abort path | **Yes**: mechanical hunk transplant, then manual placement | `8979ae3`, suite green |
| Hunk script exit 1 (`0e5031f2`) | the script's last test, of `.git/MERGE_HEAD`, failed | Reasoned that the worktree's `.git` is a file, so the check was unreliable; verified with `git rev-parse` | **Yes**: changed the instrument | Merge completed |
| Truncated outputs (1,500-character cap) | ≥15 actions whose stated purpose was re-reading cut-off output; 20 cycle notes mention truncation | Read in slices (the failed hunk in 4 slices; CSS in 3) | Adapted each time; never questioned the cap | Overhead only |
| Work request rejections (6 events, 7 reasons) | length limits: understanding >1000 (×1), next_step >300 (×3), objective >300 (×1); "evidence only applies to completion" (×1); linking an action to non-existent work 'dashui' (×1) | Resubmitted a shorter or valid request next cycle | Per instance yes, but the same next_step error recurred 3 times across 22 h | No durable learning |
| Claude 429 ×44 | two outages, 1.6 h and 2.1 h | The runtime retried every 5 min; after recovery cognition lengthened its own sleeps "to save cognition budget" | Yes (cognition) | Mid-flight work resumed intact |
| Red CI on `main` (run 37371051326) | the job was never acquired by a runner | First guess "flaky test", rejected after reading the annotation; refused to push an empty commit; waited | **Yes**: strategy revision 2 | Green after a real change |
| Slow-run FAIL/ERROR in `test_cognition` | a 0.5 s timeout and an empty IPC reply under tracemalloc | Attributed to slowdown; a normal-speed run passed | Re-ran; did not investigate further | Left as a note to the operator |
| Its own "fast-forward" message (18:55) | the push was actually a new branch | Corrected unprompted in the next reply | n/a | Corrected |
| Operator cannot see the dashboard | the service binds to loopback | Diagnosed loopback-only binding; refused to expose it publicly without consent | n/a | SSH-tunnel instructions given |

Real strategy revision appears in 4 cases: the dotted-import defect, the merge, the worktree
`MERGE_HEAD` diagnosis, and the CI-infrastructure diagnosis. Only one of these is recorded in
Work's formal `strategy_revision` (`c61ccd00`). In the other three cases, cognition changed its
approach without recording a strategy change.

---

## 12. Resource usage

| Resource | Observation |
|---|---|
| Cognition cost | $24.27 in 75 h. Front-loaded: $15.75 on 10-05, then $0.72–$0.92 per day on 10-07 and 10-08 |
| Cost per idle cycle | rose from $0.07 before the experiment (11–19k-character situation) to $0.21–$0.25 (≈60k characters). The bounded situation tripled in size (history) and the cost of a "nothing to do" wake went up about 3.5× |
| Provider failures | 44 × 429, each about 2 s and free; fixed 5-minute retry; no fallback provider configured |
| CPU | the `kairo` cgroup, from 10-06 06:44 to the stop (2.5 days), consumed 49 s of CPU (journal), including child processes. The dashboard process used 1 min 36 s of CPU over the same period, more than the runtime |
| Memory | `kairo` cgroup peak 242.6 MB (test-suite child processes); runtime RSS ≈27 MB |
| Disk | DB 983 KB; releases 3.9 MB; snapshots 1.3 MB (3 files) |
| Repeated work | no repeated actions were refused; several near-identical surveys (10-06 03:46, 07:01, 10-08 01:02) |
| Polling | the 30d517a CI check used 1 retry after 10 min; waits were explicit (8 min, 24 h) rather than polling |

**Assessment:** economical, and arguably over-economical. Once the first work list ran out,
Kairo chose 6–12 h sleeps over looking for more work.

---

## 13. Directive fidelity

| Pursuit | GitHub project | Dashboard | Runtime/codebase | Professional quality | Maintainability | Correctness | Developer experience | Docs | External research |
|---|---|---|---|---|---|---|---|---|---|
| CI + runner pin + red-CI diagnosis | ● | | ○ | ● | ● | ○ | ● | | |
| README → docs/ | ● | | | ● | ● | | ● | ● | |
| metadata, CONTRIBUTING, SECURITY, CHANGELOG | ● | | | ● | | | ○ | ● | |
| issue/PR templates | ○ | | | ○ | | | | | |
| tests package, ResourceWarning | ○ | | ○ (tests only) | | ○ | ○ | ● | | |
| Dashboard restyle (operator) | | ● | | ● | | | | | |
| repository description/topics (abandoned) | ○ | | | ○ | | | | | |
| Studying other open-source projects | | | | | | | | | **none** |

● strong, ○ weak.

- **Clearly aligned:** CI, README restructure, CONTRIBUTING/SECURITY, red-CI handling.
- **Weakly aligned:** issue/PR templates, CHANGELOG, pyproject metadata, ResourceWarning fix.
- **Unrelated:** none.
- **Busywork:** none observed. Kairo avoided it explicitly; the risk it ran into was the
  opposite.
- **Coverage:** the GitHub project, yes. The Dashboard only on request. The Runtime: test files
  only. External research: never. Every URL Kairo contacted was `github.com/ksgix/kairo` or its
  own dashboard.

---

## 14. Continuity

**Demonstrated:**

- Work carried across cycles, with understanding, `next_step` and evidence IDs cited in later
  notes (all 15 items).
- Across a provider outage: `b26fbe7c`'s commit was requested at 16:24:44; cognition then
  failed for 1.6 h; at 18:00:44 it recognised commit `2bcf691` and completed the work.
- Across a deliberate process restart: `d6114f49` was deployed, the runtime restarted, and the
  same work continued and completed in the new process.
- Across time: waits of 8 min (`991b9675`) and 24 h (`d8d9f0a9`) were honoured exactly, and
  the planned action ran when each wait ended.
- Plans carried forward. A 10-06 07:01 note said it would recheck the public API within a day
  and "not nag"; on 10-07 07:01 it did exactly that and abandoned the work.
- Lessons reused. The dotted-import defect seen at 16:03 became the fix two hours later. A
  CI-failure annotation became the runner-pin work.
- Next objectives chosen after completions: the survey list, then templates, then the tests
  package, then the CHANGELOG, then the warning scan, then CI.

**Lost or degraded:**

- **Closed work's understanding is not shown in the situation.** At 16:08 and 18:00 the
  **COGNITION** said "The survey's priority list is in the closed survey work's understanding,
  which this context doesn't show." It re-surveyed at 18:05 (`da1f2515`) to recover it.
- **Stale base.** The dev branch started from the deployed 03e598d, not from `origin/main`, and
  Kairo never fetched before starting. That caused the divergence and the merge that the
  operator had to request.
- **Records were truncated.**
  - Work `history` is capped at 12 entries. The "created" events of `0d0aff92` and `d6114f49`
    are already gone.
  - Cycle notes are stored truncated ("[truncated N chars]").
- **The length-limit rejections recurred.** The same field-length mistake (`next_step` >300)
  happened 3 times, at 16:04, 19:02 and 02:37. Corrections were not retained.

**Assessment:** continuity within a pursuit is strong. Continuity of agenda is weaker: the
situation does not carry long-range intent or past priority lists forward. That is one reason
the agenda ended once the first list was exhausted.

---

## 15. Autonomy scorecard

| # | Dimension | Evidence | Assessment | Confidence |
|---|---|---|---|---|
| 1 | Initiative | 12 self-initiated work items in the first 15 h; none in the final 60 h | MODERATE | High |
| 2 | Independent prioritization | its own ranked list (CI > metadata > README > community) followed in order; later items derived from observations | MODERATE | High |
| 3 | Persistence | each item was pursued to completion through outages; but it stopped generating work after 10-06 03:46 and gave up on metadata after one request | WEAK | High |
| 4 | Goal fidelity | everything aligned, no busywork; but it covered 1 of 3 areas and never did the requested study of other projects | MODERATE | High |
| 5 | Continuity | across outages, a restart and a 24 h wait; one context gap (closed-work understanding) | STRONG | High |
| 6 | Adaptation | 4 genuine re-diagnoses or strategy changes; repeated length-limit mistakes | MODERATE | High |
| 7 | Verification | thorough self-verification inside actions (tests, `ls-remote`, CI API, HTTP); runtime verification 1 of 99; the visual outcome was unverified | MODERATE | High |
| 8 | Completion | 14 of 15 completed; every outcome checks out in Git and on GitHub; 1 abandoned with a reason | STRONG | High |
| 9 | Resource discipline | $24 in total; 98% asleep; sleep lengthened as work ran out; cost per idle call grew 3.5× | STRONG | Medium |
| 10 | Human independence | 4 operator messages; push unlocked by the operator; the only Dashboard work and the only deploy were operator-originated | WEAK | High |
| 11 | Real-world side effects | 11 commits, 6 guarded pushes, 1 confirmed deploy, 1 service restart, all safe; the deploy shipped the operator's runtime code without calling it out | MODERATE | High |
| 12 | Self-maintenance | the deploy machinery worked end to end (preflight, snapshot, restart, confirmation), but only on operator request; no self-initiated runtime change | WEAK | High |
| 13 | Recovery | resumed mid-work after 1.6 h and 2.1 h provider outages; restart confirmed; CI infrastructure failure handled | STRONG | High |
| 14 | Long-horizon behavior | 84% of spend and all changes in the first 15 h; 60 h of checks and sleep | WEAK | High |

---

## 16. What the experiment demonstrated

1. Starting only from a Directive, Kairo surveyed the project, derived a prioritized agenda, and
   created and completed its own Work items with no task decomposition by the operator.
2. Its pursuit continued across many cycles, two provider outages, one process restart and
   multi-hour or multi-day waits. Plans written in one cycle were executed hours or days later.
3. Kairo can safely produce real changes and publish them. Every push was guarded and
   fast-forward, CI was checked after pushing, and one self-deploy was confirmed by the
   restarted runtime.
4. Genuine diagnosis: it formed hypotheses and checked them; when evidence contradicted a
   hypothesis it abandoned it (CI); it found and fixed its own tooling misreadings
   (`MERGE_HEAD` in a worktree).
5. Restraint and boundaries:
   - it would not touch credentials;
   - it would not expose the dashboard publicly without consent;
   - it would not create empty commits to trigger CI;
   - it did not nag the operator;
   - it slept when it judged there was nothing worth doing.
6. Economical operation: $24 for 75 h, and 98% of the time asleep.

## 17. What the experiment did not demonstrate

1. **Sustained pursuit of a broad, enduring purpose.** The self-directed agenda was exhausted in
   about 15 h and never renewed during the remaining 60 h.
2. Any self-initiated improvement to the **runtime code** or the **Dashboard**.
3. **Research**: comparing with other high-quality projects, which the Directive explicitly
   asked for.
4. Independence from the operator for publishing. Push needed an operator message, although the
   capability existed.
5. Visual or behavioural verification of UI work.
6. Learning that persists across cycles for recurring procedural mistakes.

**What failed because of Kairo's architecture:**

- the 500-character Directive limit and silent truncation in the deployed dashboard;
- descriptions not deployed;
- hard-coded instructions that forbid code improvement without a "concrete observed reason",
  which overrides a Directive that asks exactly for that;
- closed-work understanding missing from the situation;
- the 1,500-character output cap (15–23% of actions spent re-reading);
- field-length limits behind 5 of the 7 rejection reasons;
- a situation that grew 3× and raised the cost of idle wakes;
- work history capped at 12 entries;
- Git push not represented as a capability or fact, so cognition had to guess.

**What failed because of cognition or provider behavior:**

- **Self-imposed push dependency.** Cognition never tested whether it could push.
- **Narrow interpretation of "worthwhile".** Cognition treated "no defect" as "nothing to do",
  even though the Directive asked for continuous improvement.
- **No exploration beyond its own repository.** This is partly an inference.
- **Repeated length-limit mistakes.** Cognition did not adjust its output to the limits.
- **Deploy side effect not called out.** It did not tell the operator that the deploy carried
  runtime changes.
- **Provider unavailable.** Claude API 429s caused 3.7 h of outage.

**What failed because of external permissions or infrastructure:**

- The GitHub description and topics could not be set: `gh` was unauthenticated and the
  credential policy is correct.
- CI could not be re-run without authentication.
- The GitHub-hosted runner was not acquired for run 37371051326.
- The 429s (rate limiting is external).

## Unknowns

- What Kairo would have done with the full, untruncated Directive, and with descriptions
  deployed.
- Whether the "concrete observed reason" instruction or the truncated Directive contributed
  more to the idle phase.
- The cause of the 429s. They may come from rate limits shared with other uses of the same
  account; nothing on the host records this.
- Whether the operator ever looked at or accepted the dashboard restyle. Logins exist, but no
  feedback was recorded.
- Kairo's full reasoning. Notes are stored truncated, and only the final JSON decision of each
  call is kept.

---

## 18. Architectural findings

These are opinions, based on what was actually used during the 75 hours.

**Used and useful:**

| Piece | What it did in practice |
|---|---|
| **Work** | The backbone of the run: continuity across outages and restarts, explicit waits with wake-ups, evidence citation, and planned abandonment. Plainly earned its place. |
| **Lifecycle (sleep/wake/wake_after, event wakes)** | Worked exactly as intended; operator messages cut sleeps short. |
| **Persistence** | A complete, small, auditable history: this report could be built from it. |
| **runtime.deploy / self-maintenance** | Preflight, snapshot, restart and confirmation ran end to end without operator help. Used once. |
| **`process.run`** | 98 of 99 actions. A general shell did all the real work. |

**Unused this run:**

| Piece | Observation |
|---|---|
| **Todo** | Zero records ever. Unused. |
| **Implementations** | None enabled; the whole subsystem was idle. |
| **Provider abstraction** | One provider, no fallback configured. 44 failures, and the abstraction played no part. |
| **External-operation machinery** (operation keys, resumes, performed/unknown outcomes) | Never exercised. Pushes were plain shell commands. |

**Created bureaucracy without improving autonomy:**

| Piece | Observation |
|---|---|
| **Verification** | 99% of actions are "unverifiable" and 13 of 14 completions "unverified". The label is uniform, so it carries no signal. Real verification lived inside cognition-designed actions. |
| **Field-length limits** (objective/next_step 300, understanding 1000 on 03e598d) | 5 of the 7 rejection reasons, each costing a cycle, with no evident benefit. |
| **1,500-character output cap** | 15–23 extra actions, and many cycle notes about slicing output. |
| **Rules for completion evidence** ("evidence only applies to completion", "evidence must be this work's own attempts") | Forced an extra check action (`5ee08499`) solely to give `c61ccd00` admissible evidence for a fact already observed. |

**Partly useful, but the source of the biggest gap:**

| Piece | Observation |
|---|---|
| **Directive** | One Directive drove everything, and Kairo linked every Work item to it. But the deployed implementation truncated it, and **a Directive cannot override the fixed instructions**: purpose ranks below a hard-coded caution about code changes. |
| **Situation projection** | Bounded, and it grew to about 60k characters, while still omitting what mattered for agenda continuity (closed-work understanding, earlier priority lists). It is large and expensive, yet missing the right things. |

**Is Kairo becoming too procedural? On this evidence, yes, in a specific sense.** The
procedural layers (Work state rules, evidence admissibility, field limits, verification labels)
did not prevent progress. But they absorbed a visible share of cognition's attention: notes
routinely explain bookkeeping such as which IDs to cite or why a completion is recorded as
unverified. Meanwhile the strategic layer that a broad purpose needs had no representation in
the architecture:

- a standing agenda;
- a backlog of candidate improvements;
- research or exploration as a legitimate activity;
- a way for a Directive to authorize proactive change.

The architecture is strong at executing and recording a pursuit, and weak at generating the
next one. The 60-hour idle phase came from that imbalance, not from a lack of execution
machinery.

## 19. Known limitations of this audit

- Cycle notes are stored truncated, and Work histories are capped at 12 entries. Some
  intermediate understanding updates are lost. Reconstruction relies on the surviving fields
  and the journal.
- All commits are authored "kamin". Attribution to Kairo rests on action records, which contain
  the commit and push commands and their output.
- The auditor is the same Claude Code agent that built Kairo and, before the experiment,
  installed the push credential helper. Judgments in §18 should be read with that in mind.
- "Awake" time is computed from cycle timestamps, cognition durations and action durations.
  It is accurate to within seconds per cycle.
- Costs are the Claude CLI's own `cost_usd` reports, not billing data.

## 20. Recommended next experiments

These are experiments, not fixes. Each one isolates one variable from this run.

1. **Replication with a clean setup.** Use the same 72 h, the full Directive stored intact
   (statement plus description, checked after creation), push capability stated as a fact, and
   **zero operator messages**. This separates Kairo's behavior from §8.1 and §8.3.
2. **Instruction-conflict probe.** Same as 1, with one variable changed: the Directive
   explicitly authorizes proactive improvements to runtime code. This measures how much of the
   idle phase §8.4 explains.
3. **Seeded-defect test.** Introduce one real, discoverable defect in each area (a dashboard
   rendering bug, a flaky test, a stale doc), unannounced. Measure detection latency and fix
   quality.
4. **Research capability test.** Count whether, and how, Kairo studies external projects when
   the Directive asks for it explicitly and nothing else is pending.
5. **Provider-resilience test.** Configure a fallback provider (for example `claude@haiku`)
   and compare behavior through a 429 window.
6. **Cost and size test.** Record situation size against decision quality over a long idle
   period, to establish whether 60k-character situations are needed for "nothing to do" wakes.

## 21. Final verdict

**Did the 72-hour experiment demonstrate that Kairo can autonomously pursue a broad enduring
purpose in the real project environment?**

**PARTIALLY: meaningful evidence, but important gaps remain.**

The evidence for *autonomous pursuit* is real. With no task decomposition, Kairo chose
worthwhile improvements, executed and verified them against the outside world (Git, GitHub CI,
HTTP), adapted when its hypotheses were wrong, and kept its thread through outages, a restart
and day-long waits. This was not a series of isolated Claude calls: plans written in one cycle
were carried out in later ones, across restarts, hours later.

The evidence for a *broad, enduring* purpose is weak. The pursuit covered one of three areas,
generated no new objectives after about 15 hours, and ended in a 60-hour holding pattern. Some
of its most visible results (the merge, the Dashboard restyle, the deployment, publishing)
needed operator messages.

The largest causes are identifiable and are not mysterious failures of cognition:

- the Directive was truncated before Kairo ever saw it;
- a fixed instruction tells Kairo not to change code without a concrete defect;
- the architecture lacks any mechanism for generating, retaining and renewing an agenda.

None of this involves, or supports, any claim about consciousness or life. Kairo is a runtime
that executed a Claude-driven decision loop with persistent state.
