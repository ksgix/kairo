"""Deployment: selecting a committed revision of Kairo's own code as the running runtime.

Self-maintenance is ordinary work: cognition inspects, edits, tests and commits
Kairo's source with ordinary actions (``process.run``) in a *development
repository*. This module covers only the step those actions cannot: making a
committed revision the code Kairo runs, through one action, ``runtime.deploy``.

Layout (``--releases``, e.g. /var/lib/kairo/deploy)::

    releases/<sha>/     immutable release: ``git archive`` of one commit, read-only
    current  -> releases/<sha>    what the supervisor starts
    previous -> releases/<sha>    the release that requested the latest switch
    snapshots/          database backups taken before each switch (never auto-restored)

The running release is never the development tree, and is never edited in place:
an edit to the repository changes nothing until it is committed and deployed.

``runtime.deploy {revision}`` (executed through Runtime.act -> Environment.execute):

    resolve commit -> build release -> preflight (candidate's own suite: gate;
    running release's suite against the candidate: evidence; dry cycle on a copy
    of the database) -> snapshot database -> previous := running release ->
    current := candidate -> restart requested

The old process then persists everything and exits with RESTART_EXIT; the
external supervisor (systemd, see deploy/) starts ``current``. Only that new
process can confirm the deployment (Runtime): until then the action is
``awaiting_confirmation``, never a success.

Kairo keeps its broad authority over the host. The supervisor and its fallback
are recovery infrastructure that works when Kairo's own code is broken, not a
security boundary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from kairo.actions import Action, ActionResult
from kairo.implementations import content_digest
from kairo.memory import backup_database
from kairo.redact import scrubbed_env
from kairo.verification import Outcome, Verification

KIND = "runtime.deploy"
RESTART_EXIT = 75      # "restart me: a new release was selected" (EX_TEMPFAIL)
PROBATION_EXIT = 3     # a just-deployed release proved unusable before confirming itself
MARKER = ".kairo-release"
REVISION = re.compile(r"^[0-9a-f]{7,40}$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_PREFLIGHT_TIMEOUT = 900.0
GIT_TIMEOUT = 60.0
SUMMARY_CHARS = 600
LIST_LIMIT = 20

# Files whose change alters how Kairo keeps state, executes, verifies, recovers or
# deploys. Changing them is allowed; the fact is reported so it gets scrutiny.
TRUST_CRITICAL = frozenset({
    "src/kairo/runtime.py", "src/kairo/memory.py", "src/kairo/deploy.py",
    "src/kairo/actions.py", "src/kairo/verification.py", "src/kairo/environment.py",
    "src/kairo/work.py", "src/kairo/cognition.py", "src/kairo/redact.py",
    "src/kairo/__main__.py", "deploy/kairo.service", "deploy/kairo-fallback",
})

ACTION = {
    "description": (
        "Make a committed revision of Kairo's own code the running runtime. The runtime "
        "builds an immutable release from the commit, runs preflight (the candidate's full "
        "test suite must pass; the running release's tests are run against it as evidence; a "
        "dry cycle runs on a copy of the database), snapshots the database, switches to the "
        "release and restarts. Only committed revisions can be deployed; uncommitted changes "
        "never run. The deployment stays 'awaiting_confirmation' until the restarted runtime "
        "confirms it is running the revision. To roll back, deploy the previous revision."),
    "params": {
        "type": "object",
        "properties": {
            "revision": {"type": "string", "pattern": REVISION.pattern,
                         "description": "commit SHA (7-40 lowercase hex characters)"},
        },
        "required": ["revision"],
        "additionalProperties": False,
    },
}


class DeployError(Exception):
    """A deployment refused or failed at ``stage``. ``executed``: whether any of
    it ran (False: refused before anything ran)."""

    def __init__(self, stage: str, message: str, failure: str = "invalid_params",
                 executed: bool = False) -> None:
        super().__init__(message)
        self.stage, self.failure, self.executed = stage, failure, executed


class Deployment:
    """Kairo's deployment configuration and the mechanics of one deploy."""

    def __init__(self, repository: str | Path, releases: str | Path, db: str | Path,
                 preflight_args: list[str] | None = None,
                 running: str | Path | None = None,
                 preflight_timeout: float = DEFAULT_PREFLIGHT_TIMEOUT) -> None:
        self.repository = Path(repository).resolve()
        self.root = Path(releases).resolve()
        self.db = Path(db)
        if _inside(self.repository, self.root) or _inside(self.root, self.repository):
            raise ValueError("the development repository and the releases directory must be "
                             "separate: the running release is never the development tree")
        self.preflight_args = list(preflight_args or [])
        self.preflight_timeout = preflight_timeout
        # Which release this process runs: decided once, at start, from the code it
        # actually imported (not from HEAD, and not from 'current', which may move).
        if running is None:
            import kairo
            running = Path(kairo.__file__).resolve().parent.parent.parent
        self.running = self._as_release(Path(running))
        self.process_started_at = time.time()

    # -- facts -------------------------------------------------------------

    @property
    def running_revision(self) -> str | None:
        return self.running.name if self.running is not None else None

    def _as_release(self, path: Path) -> Path | None:
        path = path.resolve()
        releases = self.root / "releases"
        if path.parent == releases and FULL_SHA.match(path.name) and (path / MARKER).is_file():
            return path
        return None

    def _link(self, name: str) -> str | None:
        try:
            target = os.readlink(self.root / name)
        except OSError:
            return None
        release = self._as_release(self.root / target)
        return release.name if release else None

    def facts(self) -> dict[str, Any]:
        """Bounded runtime facts about the code: the running release (authoritative),
        what the links select, and the development repository's state."""
        running = None
        if self.running is not None:
            running = {"revision": self.running.name, "release": str(self.running),
                       "digest": content_digest(self.running)}
        return {
            "running": running or {"revision": None,
                                   "note": "not running from a release under " + str(self.root)},
            "current": {"revision": self._link("current")},
            "previous": {"revision": self._link("previous")},
            "repository": self._repository_facts(),
        }

    def _repository_facts(self) -> dict[str, Any]:
        try:
            head = self._git("rev-parse", "--verify", "--quiet", "HEAD").strip() or None
            branch = self._git("symbolic-ref", "--short", "-q", "HEAD", check=False).strip() or None
            dirty = self._git("status", "--porcelain").splitlines()
        except DeployError as exc:
            return {"path": str(self.repository), "unavailable": str(exc)[:200]}
        return {"path": str(self.repository), "head": head, "branch": branch,
                "dirty_files": len(dirty),
                "head_is_running": head is not None and head == self.running_revision}

    # -- the action --------------------------------------------------------

    def deploy(self, action: Action) -> ActionResult:
        stages: list[dict[str, Any]] = []
        output: dict[str, Any] = {"stages": stages, "switched": False}
        try:
            sha = self._resolve(action.params)
            output.update(revision=sha, **{"from": self.running_revision, "to": sha})
            if self.running is None:
                raise DeployError("build", "this runtime is not running from a release under "
                                  f"{self.root}; deployment needs the supervised release layout")
            release = self.build(sha)
            stages.append({"stage": "build", "passed": True, "release": str(release)})
            output.update(self._changes(sha))
            self.preflight(release, stages)
            snapshot = self.root / "snapshots" / f"{time.strftime('%Y%m%dT%H%M%S')}-{action.id[:12]}.db"
            try:
                backup_database(self.db, snapshot)
            except Exception as exc:
                raise DeployError("snapshot", f"database snapshot failed: {exc}", "os_error",
                                  executed=True) from None
            output["snapshot"] = str(snapshot)
            self.switch(sha)
            stages.append({"stage": "switch", "passed": True})
        except DeployError as exc:
            output["stage"] = exc.stage
            output["error"] = str(exc)[:SUMMARY_CHARS]
            if exc.executed:  # it ran and was refused by a gate: a verified failure, see verifier
                return ActionResult(action.id, executed=True, output=output)
            return ActionResult(action.id, executed=False, output=output, error=str(exc),
                                failure=exc.failure)
        output.update(stage="switch", switched=True)
        return ActionResult(action.id, executed=True, output=output, restart=True)

    def _resolve(self, params: dict[str, Any]) -> str:
        if not isinstance(params, dict) or set(params) != {"revision"}:
            raise DeployError("build", "runtime.deploy takes exactly {'revision': <commit sha>}")
        revision = params["revision"]
        if not isinstance(revision, str) or not REVISION.match(revision):
            raise DeployError("build", "revision must be a commit SHA (7-40 lowercase hex "
                              "characters); names, paths and 'latest' are not accepted")
        sha = self._git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}",
                        check=False).strip()
        if not FULL_SHA.match(sha):
            raise DeployError("build", f"unknown or ambiguous revision {revision!r} in "
                              f"{self.repository}")
        return sha

    # -- releases ----------------------------------------------------------

    def build(self, sha: str) -> Path:
        """The immutable release for a commit, built once from ``git archive`` (never
        from the working tree, so uncommitted changes cannot get in)."""
        if not FULL_SHA.match(sha):
            raise DeployError("build", f"not a full commit sha: {sha!r}")
        release = self.root / "releases" / sha
        if release.exists():
            if self._as_release(release) is None:
                raise DeployError("build", f"{release} exists but is not a complete release",
                                  "os_error")
            return release
        release.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{sha[:12]}-", dir=release.parent))
        try:
            archive = staging / "release.tar"
            self._git("archive", "--format=tar", f"--output={archive}", sha)
            tree = staging / "tree"
            with tarfile.open(archive) as tar:
                tar.extractall(tree, filter="data")
            archive.unlink()
            (tree / MARKER).write_text(json.dumps({"revision": sha, "built_at": time.time()}))
            _read_only(tree)
            os.rename(tree, release)  # appears complete, or not at all
            release.chmod(release.stat().st_mode & ~0o222)  # (a directory moves only while writable)
        except DeployError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise DeployError("build", f"could not build release {sha[:12]}: {exc}",
                              "os_error") from None
        finally:
            _remove(staging)
        return release

    def switch(self, sha: str) -> None:
        """previous := the running release (unless redeploying it), then
        current := the candidate. Each link is replaced atomically (rename)."""
        try:
            if self.running is not None and self.running.name != sha:
                _replace_link(self.root, "previous", f"releases/{self.running.name}")
            _replace_link(self.root, "current", f"releases/{sha}")
        except OSError as exc:
            raise DeployError("switch", f"could not switch releases: {exc}", "os_error",
                              executed=True) from None

    def _changes(self, sha: str) -> dict[str, Any]:
        """What the candidate changes relative to the running release, as facts."""
        if self.running is None or self.running.name == sha:
            return {"trust_critical_changed": [], "tests_changed": []}
        try:
            lines = self._git("diff", "--name-status", "--no-renames", self.running.name, sha)
        except DeployError:
            return {"trust_critical_changed": None, "tests_changed": None,
                    "changes_unavailable": "running revision not in the repository"}
        entries = [line.split("\t", 1) for line in lines.splitlines() if "\t" in line]
        return {
            "files_changed": len(entries),
            "trust_critical_changed": [f"{s} {p}" for s, p in entries if p in TRUST_CRITICAL][:LIST_LIMIT],
            "tests_changed": [f"{s} {p}" for s, p in entries if p.startswith("tests/")][:LIST_LIMIT],
        }

    # -- preflight -------------------------------------------------------------

    def preflight(self, release: Path, stages: list[dict[str, Any]]) -> None:
        """Mandatory checks of a candidate, run by the current (known-good) code.
        Raises DeployError at the first failing gate; nothing is switched then."""
        tests = self._suite(release, release / "tests", "tests", gate=True)
        stages.append(tests)
        if not tests["passed"]:
            raise DeployError("tests", "the candidate's own test suite did not pass: "
                              + tests["summary"], executed=True)
        if self.running is not None and self.running != release:
            stages.append(self._suite(release, self.running / "tests", "baseline", gate=False))
        dry = self._dry_cycle(release)
        stages.append(dry)
        if not dry["passed"]:
            raise DeployError("dry_cycle", "the candidate could not complete a dry cycle: "
                              + dry["summary"], executed=True)

    def _suite(self, release: Path, tests: Path, stage: str, gate: bool) -> dict[str, Any]:
        """Run one unittest suite (``tests/``, discovered as Kairo's own suite is:
        the tests directory is the top level) with the candidate's code. Passing
        means exit 0 and at least one test actually ran, not skipped: "no tests
        ran" is not a pass."""
        result: dict[str, Any] = {"stage": stage, "role": "gate" if gate else "evidence",
                                  "suite": str(tests)}
        if not tests.is_dir():
            return {**result, "ran": False, "passed": False, "summary": "no test suite"}
        code, out, err = self._python(release, ["-m", "unittest", "discover", "-s", str(tests),
                                                "-t", str(tests)])
        ran, skipped = _unittest_counts(err)
        result.update(ran=True, returncode=code, tests_run=ran, skipped=skipped,
                      passed=code == 0 and ran is not None and ran > (skipped or 0),
                      summary=_tail(err or out))
        if code == 0 and not result["passed"]:
            result["summary"] = f"no tests actually ran ({ran} run, {skipped or 0} skipped)"
        return result

    def _dry_cycle(self, release: Path) -> dict[str, Any]:
        """Start the candidate against a copy of the database: it must import,
        open persistence, start, build its context, situation and cognition
        request, parse decisions and complete one cycle. No provider is called and
        no action is executed; the live database is never opened."""
        result: dict[str, Any] = {"stage": "dry_cycle", "role": "gate"}
        with tempfile.TemporaryDirectory(prefix="kairo-preflight-") as tmp:
            copy = Path(tmp) / "kairo.db"
            try:
                if self.db.exists():
                    backup_database(self.db, copy)
            except Exception as exc:
                return {**result, "ran": False, "passed": False,
                        "summary": f"could not copy the database: {exc}"}
            code, out, err = self._python(release, ["-m", "kairo", "--preflight", "--db",
                                                    str(copy), *self.preflight_args], cwd=tmp)
        try:
            report = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except (json.JSONDecodeError, IndexError):
            report = {}
        passed = code == 0 and report.get("ok") is True
        return {**result, "ran": True, "returncode": code, "passed": passed,
                "summary": _tail(err) if not passed else
                f"cycle {report.get('cycle')}, situation {report.get('situation_chars')} chars"}

    def _python(self, release: Path, args: list[str], cwd: str | Path | None = None
                ) -> tuple[int, str, str]:
        """Run Python with the candidate's code, in the same contained way as every
        action: own process group, scrubbed environment (no provider credentials),
        bounded time. KAIRO_DB / KAIRO_SOCKET are removed so nothing defaults to
        the live database or socket."""
        from kairo.environment import run_contained
        env = {k: v for k, v in scrubbed_env().items() if k not in ("KAIRO_DB", "KAIRO_SOCKET")}
        env.update(PYTHONPATH=str(release / "src"), PYTHONDONTWRITEBYTECODE="1")
        try:
            return run_contained([sys.executable, *args], cwd=str(cwd or release), env=env,
                                 timeout=self.preflight_timeout)
        except subprocess.TimeoutExpired:
            return -1, "", f"timed out after {self.preflight_timeout:g}s"
        except OSError as exc:
            return -1, "", f"could not run: {exc}"

    def _git(self, *args: str, check: bool = True) -> str:
        from kairo.environment import run_contained
        try:
            code, out, err = run_contained(["git", "-C", str(self.repository), *args], cwd=None,
                                           env=scrubbed_env(), timeout=GIT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeployError("build", f"git failed: {exc}", "os_error") from None
        if check and code != 0:
            raise DeployError("build", f"git {args[0]} failed: {_tail(err)}", "os_error")
        return out


class AwaitSuccessor:
    """Verifier for runtime.deploy. The deploying process can only establish that
    the switch happened; whether the new release runs is for the restarted runtime
    to verify (Runtime confirms or fails this record). A refused deploy is a
    verified failure: the runtime checked that nothing was switched."""

    def verify(self, action: Action, result: ActionResult) -> Verification:
        output = result.output or {}
        if output.get("switched"):
            return Verification(Outcome.UNVERIFIABLE,
                                "awaiting confirmation by the restarted runtime",
                                {"awaiting": "successor", "target": output.get("to")})
        return Verification(Outcome.FAILURE, f"not deployed: refused at stage "
                            f"{output.get('stage')}: {output.get('error')}",
                            {"stage": output.get("stage"), "switched": False})


class PreflightCognition:
    """Stands in for cognition in a candidate's dry cycle (``--preflight``): it
    builds the real, provider-neutral cognition request from the context (the
    instructions, the situation, the decision schema) and parses decisions
    against the candidate's own action catalogue, exactly as a provider's answer
    would be parsed, without calling any provider. It decides to sleep."""

    name = "preflight"

    def __init__(self) -> None:
        self.situation_chars = 0

    def decide(self, context: Any) -> Any:
        from kairo.cognition import parse_decision
        from kairo.instructions import cognition_request
        request = cognition_request(context)
        json.dumps(request.schema)
        self.situation_chars = len(request.prompt)
        base = {"reason": "preflight", "replies": [], "wake_after": None, "work": []}
        parse_decision({**base, "sleep": False, "actions": [
            {"kind": "process.run", "params": {"argv": ["true"]}, "reason": "parse check",
             "work": None}]}, context.available_actions)
        return parse_decision({**base, "sleep": True, "actions": []}, context.available_actions)


def awaiting_confirmation(record: dict[str, Any]) -> bool:
    verification = record.get("verification")
    return (record.get("kind") == KIND and record.get("status") == "finished"
            and isinstance(verification, dict) and verification.get("outcome") == "unverifiable"
            and (verification.get("evidence") or {}).get("awaiting") == "successor")


# -- helpers -----------------------------------------------------------------


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _replace_link(root: Path, name: str, target: str) -> None:
    tmp = root / f".{name}.{os.getpid()}.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, root / name)


def _read_only(tree: Path) -> None:
    """Running code is never edited in place: everything in a release loses its
    write bits (deepest first, so the walk can finish; the top directory itself
    after it has been moved into place)."""
    for path in sorted(tree.rglob("*"), key=lambda p: -len(p.parts)):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)


def _remove(path: Path) -> None:
    """Remove a (possibly read-only) tree that this module created."""
    if not path.exists():
        return
    for p in [path, *path.rglob("*")]:
        if not p.is_symlink():
            try:
                p.chmod(p.stat().st_mode | 0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
_SKIPPED = re.compile(r"skipped=(\d+)")


def _unittest_counts(text: str) -> tuple[int | None, int | None]:
    ran = _RAN.findall(text or "")
    skipped = _SKIPPED.findall((text or "").strip().splitlines()[-1] if text and text.strip() else "")
    return (int(ran[-1]) if ran else None, int(skipped[-1]) if skipped else 0)


def _tail(text: str) -> str:
    return (text or "").strip()[-SUMMARY_CHARS:]


def init_release(repository: str | Path, releases: str | Path, revision: str) -> int:
    """Operator bootstrap (``python3 -m kairo --init-release SHA --repository R
    --releases D``): build the release for a commit, require its own test suite to
    pass, and select it as ``current`` if nothing is selected yet. Afterwards the
    running Kairo deploys with runtime.deploy."""
    deployment = Deployment(repository, releases, db=os.devnull, running=os.devnull)
    try:
        sha = deployment._resolve({"revision": revision})
        release = deployment.build(sha)
        suite = deployment._suite(release, release / "tests", "tests", gate=True)
    except DeployError as exc:
        print(f"kairo: {exc}", file=sys.stderr)
        return 1
    if not suite["passed"]:
        print(f"kairo: the release's test suite did not pass: {suite['summary']}", file=sys.stderr)
        return 1
    if os.path.lexists(deployment.root / "current"):
        print(f"kairo: built {release}; 'current' already exists and was not changed",
              file=sys.stderr)
        return 0
    _replace_link(deployment.root, "current", f"releases/{sha}")
    print(json.dumps({"current": sha, "release": str(release)}))
    return 0
