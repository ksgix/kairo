"""Phase 9, self-maintenance: deployment of Kairo's own code.

Self-maintenance is ordinary work with ordinary actions; what is tested here is
the part that is new: immutable releases built from commits, the runtime.deploy
action (preflight, snapshot, switch, restart), confirmation by the restarted
process, the external fallback, the database lock and tolerant record readers.

Every test uses temporary repositories, databases and release directories. The
end-to-end test runs real Kairo processes under a test supervisor that applies
the same rules as deploy/kairo.service and calls the real deploy/kairo-fallback.
"""

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

import kairo
from kairo import Action, ActionResult, Decision, Environment, Memory, Runtime, State
from kairo import deploy
from kairo.actions import INDETERMINATE, action_state, attempt_identity
from kairo.cognition import CognitionError, Context
from kairo.deploy import Deployment, _remove, _replace_link
from kairo.directives import Directive
from kairo.environment import ACTIONS
from kairo.implementations import Implementations
from kairo.memory import DatabaseLocked, from_record, lock_database
from kairo.redact import protect_env, redact
from kairo.situation import build_situation, render_situation
from kairo.work import Work, WorkLedger, work_from_record
from test_work import Script, create, set_state, update

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path(kairo.__file__).resolve().parent
FALLBACK = ROOT / "deploy" / "kairo-fallback"
GIT = ["git", "-c", "user.name=Kairo Test", "-c", "user.email=kairo@test.invalid",
       "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]
SECRET = "KAIRO_TEST_PROVIDER_TOKEN"
SECRET_VALUE = "sk-test-0123456789abcdefXYZ"

SMOKE = '''import os
import unittest


class Smoke(unittest.TestCase):
    def test_runtime_starts(self):
        from kairo import Memory, Runtime
        runtime = Runtime(Memory())
        runtime.start()
        runtime.stop()

    def test_no_provider_secret_in_environment(self):
        self.assertNotIn("KAIRO_TEST_PROVIDER_TOKEN", os.environ)
'''


def git(repo, *args) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class Repo:
    """A development repository holding a copy of the Kairo source under test
    and a small test suite of its own."""

    def __init__(self, base: Path):
        self.path = base / "dev"
        shutil.copytree(PACKAGE, self.path / "src" / "kairo",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in [self.path, *self.path.rglob("*")]:  # the source may be a read-only release
            path.chmod(path.stat().st_mode | 0o700)
        for name in ("_deploy_test_note.py", "e2e_marker.py"):  # files the tests create themselves
            (self.path / "src" / "kairo" / name).unlink(missing_ok=True)
        self.write("tests/test_smoke.py", SMOKE)
        git(self.path, "init", "-q")
        self.commit("release A")

    def write(self, rel: str, text: str) -> None:
        path = self.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def edit(self, rel: str, old: str, new: str) -> None:
        text = (self.path / rel).read_text()
        assert old in text, old
        self.write(rel, text.replace(old, new, 1))

    def commit(self, message: str) -> str:
        git(self.path, "add", "-A")
        git(self.path, "commit", "-qm", message)
        return self.head

    @property
    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD")


def bootstrap(repo: Repo, root: Path) -> Path:
    """The operator's first release: built, its suite passed, selected as current."""
    proc = subprocess.run([sys.executable, "-m", "kairo", "--init-release", repo.head,
                           "--repository", str(repo.path), "--releases", str(root)],
                          capture_output=True, text=True, env=_env(PACKAGE.parent))
    assert proc.returncode == 0, proc.stderr
    return (root / "current").resolve()


def _env(src: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("KAIRO_DB", "KAIRO_SOCKET")}
    return {**env, "PYTHONPATH": str(src), "PYTHONDONTWRITEBYTECODE": "1", **extra}


class DeployCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "deploy"
        self.addCleanup(_remove, self.root)  # releases are read-only
        self.repo = Repo(self.base)
        self.db = self.base / "kairo.db"
        self.release_a = bootstrap(self.repo, self.root)
        self.sha_a = self.release_a.name
        self.memory = Memory(self.db)
        self.addCleanup(self.memory.close)
        self.deployment = Deployment(self.repo.path, self.root, self.db, running=self.release_a)
        self.runtime = Runtime(self.memory, Environment(deployment=self.deployment))

    def deploy(self, revision, work_id=None, params=None):
        return self.runtime.act(Action("runtime.deploy", params or {"revision": revision},
                                       reason="deploy", work_id=work_id))

    def record(self, step):
        return self.memory.get("action", step.action.id)

    def link(self, name):
        return os.readlink(self.root / name).split("/")[-1]

    def candidate(self, message="candidate", rel="src/kairo/_deploy_test_note.py", text="NOTE = 1\n"):
        self.repo.write(rel, text)
        return self.repo.commit(message)


# -- 1-8: the action, releases and switching -----------------------------------------


class DeployActionTest(DeployCase):
    def test_01_03_malformed_and_unknown_revisions_are_refused_before_anything_runs(self):
        for params in ({}, {"revision": "HEAD"}, {"revision": "latest"}, {"revision": "main"},
                       {"revision": "../../etc"}, {"revision": "/tmp/x"}, {"revision": "ABCDEF1"},
                       {"revision": 1234567}, {"revision": "abc"},
                       {"revision": self.sha_a, "path": "/tmp"}, {"revision": "deadbeef" * 5}):
            step = self.deploy(None, params=params)
            self.assertFalse(step.result.executed, params)
            self.assertEqual(step.result.failure, "invalid_params", params)
            self.assertEqual(action_state(self.record(step)), "failed_to_execute")
            self.assertFalse(step.result.restart)
        self.assertEqual(self.link("current"), self.sha_a)
        self.assertEqual(sorted(p.name for p in (self.root / "releases").iterdir()), [self.sha_a])
        self.assertNotIn("runtime.deploy", ACTIONS)  # never a core action without configuration
        self.assertIn("runtime.deploy", self.runtime.environment.actions())
        self.assertNotIn("runtime.deploy", Environment().actions())
        refused = Environment().execute(Action("runtime.deploy", {"revision": self.sha_a}))
        self.assertEqual(refused.failure, "invalid_params")

    def test_02_07_14_valid_deploy_builds_preflights_snapshots_and_switches(self):
        self.memory.put("directive", "d1", {"statement": "keep Kairo healthy", "active": True,
                                            "id": "d1"})
        sha_b = self.candidate()
        step = self.deploy(sha_b[:12])  # an abbreviated SHA resolves to the full commit
        out = step.result.output
        self.assertTrue(step.result.executed)
        self.assertTrue(step.result.restart)
        self.assertEqual((out["from"], out["to"], out["switched"]), (self.sha_a, sha_b, True))
        self.assertEqual([s["stage"] for s in out["stages"]],
                         ["build", "tests", "baseline", "dry_cycle", "switch"])
        tests, baseline, dry = out["stages"][1:4]
        self.assertEqual((tests["role"], tests["passed"], tests["tests_run"]), ("gate", True, 2))
        self.assertEqual((baseline["role"], baseline["passed"]), ("evidence", True))
        self.assertEqual((dry["role"], dry["passed"]), ("gate", True))
        self.assertEqual((self.link("current"), self.link("previous")), (sha_b, self.sha_a))
        snapshot = Path(out["snapshot"])
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(snapshot) as copy:
            kinds = {k for (k,) in copy.execute("SELECT DISTINCT kind FROM records")}
        self.assertTrue({"directive", "action"} <= kinds)  # taken before the switch
        record = self.record(step)
        self.assertEqual(action_state(record), "awaiting_confirmation")
        self.assertIs(record["result"]["restart"], True)
        self.assertEqual(record["verification"]["evidence"], {"awaiting": "successor",
                                                              "target": sha_b})
        self.assertEqual(self.runtime.exit_code, deploy.RESTART_EXIT)

    def test_04_36_uncommitted_changes_never_reach_a_release(self):
        self.repo.write("src/kairo/_deploy_test_note.py", "DIRTY = True\n")
        self.repo.edit("src/kairo/situation.py", '"""The situation model', '"""DIRTY The situation model')
        release = self.deployment.build(self.repo.head)
        self.assertEqual(release, self.release_a)  # HEAD is unchanged: the same release
        self.assertFalse((release / "src/kairo/_deploy_test_note.py").exists())
        self.assertNotIn("DIRTY", (release / "src/kairo/situation.py").read_text())
        sha_b = self.candidate(text="COMMITTED = True\n")
        self.repo.write("src/kairo/_deploy_test_note.py", "DIRTY_AGAIN = True\n")
        release_b = self.deployment.build(sha_b)
        self.assertEqual((release_b / "src/kairo/_deploy_test_note.py").read_text(), "COMMITTED = True\n")

    def test_05_06_43_releases_match_their_commit_and_are_immutable(self):
        sha_b = self.candidate()
        release = self.deployment.build(sha_b)
        for rel in git(self.repo.path, "ls-tree", "-r", "--name-only", sha_b).splitlines():
            self.assertEqual((release / rel).read_bytes(),
                             subprocess.run([*GIT, "-C", str(self.repo.path), "show",
                                             f"{sha_b}:{rel}"], capture_output=True).stdout, rel)
        if os.geteuid() != 0:
            with self.assertRaises(PermissionError):
                (release / "src/kairo/runtime.py").write_text("edited in place")
            with self.assertRaises(PermissionError):
                (release / "src/kairo/new.py").write_text("x")
        self.assertFalse((release / ".git").exists())  # not a development tree
        proc = subprocess.run(["git", "-C", str(release), "status"], capture_output=True)
        self.assertNotEqual(proc.returncode, 0)
        with self.assertRaises(ValueError):  # a release (or the layout) can never be the repository
            Deployment(self.root / "releases" / sha_b, self.root, self.db)
        with self.assertRaises(ValueError):
            Deployment(self.repo.path, self.repo.path / "deploy", self.db)
        self.assertEqual(self.deployment.build(sha_b), release)  # built once, reused

    def test_08_links_are_replaced_atomically_and_redeploying_keeps_previous(self):
        sha_b = self.candidate()
        self.deploy(sha_b)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".")], [])
        running_b = Deployment(self.repo.path, self.root, self.db,
                               running=self.root / "releases" / sha_b)
        running_b.switch(sha_b)  # redeploying the running release: previous stays known-good
        self.assertEqual((self.link("current"), self.link("previous")), (sha_b, self.sha_a))


# -- 9-13, 42, 44: preflight ---------------------------------------------------------


class PreflightTest(DeployCase):
    def refused_at(self, step, stage):
        out = step.result.output
        self.assertEqual(out["stage"], stage)
        self.assertFalse(out["switched"])
        self.assertFalse(step.result.restart)
        self.assertEqual(self.link("current"), self.sha_a)
        self.assertFalse((self.root / "previous").exists())
        self.assertFalse((self.root / "snapshots").exists())  # no snapshot without a switch
        record = self.record(step)
        self.assertEqual(action_state(record), "verified_failed")
        self.assertEqual(self.runtime.exit_code, 0)

    def test_09_10_a_failing_suite_refuses_the_deploy(self):
        self.repo.write("tests/test_broken.py", "import unittest\nclass T(unittest.TestCase):\n"
                        "    def test_x(self):\n        self.assertEqual(1, 2)\n")
        step = self.deploy(self.repo.commit("broken"))
        self.refused_at(step, "tests")
        tests = step.result.output["stages"][1]
        self.assertEqual((tests["role"], tests["passed"], tests["returncode"]), ("gate", False, 1))

    def test_12_no_tests_is_not_a_pass(self):
        git(self.repo.path, "rm", "-q", "-r", "tests")
        step = self.deploy(self.repo.commit("no tests"))
        self.refused_at(step, "tests")
        self.assertEqual(step.result.output["stages"][1]["summary"], "no test suite")

    def test_12_only_skipped_tests_is_not_a_pass(self):
        self.repo.write("tests/test_smoke.py", "import unittest\nclass T(unittest.TestCase):\n"
                        "    @unittest.skip('x')\n    def test_x(self):\n        pass\n")
        step = self.deploy(self.repo.commit("skipped only"))
        self.refused_at(step, "tests")
        tests = step.result.output["stages"][1]
        self.assertEqual(tests["returncode"], 0)  # unittest itself says OK...
        self.assertIn("no tests actually ran", tests["summary"])  # ...but nothing ran

    def test_13_a_candidate_that_cannot_complete_a_dry_cycle_is_refused(self):
        self.repo.edit("src/kairo/situation.py", "def build_situation(context: Context, "
                       "limits: Limits = LIMITS) -> dict[str, Any]:\n",
                       "def build_situation(context: Context, limits: Limits = LIMITS) -> "
                       "dict[str, Any]:\n    raise RuntimeError('broken situation')\n")
        step = self.deploy(self.repo.commit("breaks the situation"))  # its own suite passes
        self.refused_at(step, "dry_cycle")
        self.assertTrue(step.result.output["stages"][1]["passed"])

    def test_11_42_baseline_is_evidence_and_trust_critical_changes_are_facts(self):
        # The candidate changes a behaviour the running release's tests assert, and
        # rewrites that test: its own suite passes, the old suite does not.
        self.repo.write("tests/test_note.py", "import unittest\nfrom kairo import _deploy_test_note as e2e_note\n"
                        "class T(unittest.TestCase):\n    def test_note(self):\n"
                        "        self.assertEqual(e2e_note.NOTE, 1)\n")
        self.repo.write("src/kairo/_deploy_test_note.py", "NOTE = 1\n")
        self.repo.commit("asserted behaviour")
        old = self.deployment.build(self.repo.head)
        running = Deployment(self.repo.path, self.root, self.db, running=old)
        self.runtime.environment.deployment = running
        self.repo.write("src/kairo/_deploy_test_note.py", "NOTE = 2\n")
        self.repo.write("tests/test_note.py", (self.repo.path / "tests/test_note.py").read_text()
                        .replace("NOTE, 1", "NOTE, 2"))
        self.repo.edit("src/kairo/verification.py", '"""Verification:', '"""Verification (v2):')
        step = self.deploy(self.repo.commit("changes behaviour and its test"))
        out = step.result.output
        self.assertTrue(out["switched"])
        baseline = next(s for s in out["stages"] if s["stage"] == "baseline")
        self.assertEqual((baseline["role"], baseline["passed"]), ("evidence", False))
        self.assertIn("M src/kairo/verification.py", out["trust_critical_changed"])
        self.assertIn("M tests/test_note.py", out["tests_changed"])
        self.assertNotIn("M src/kairo/_deploy_test_note.py", out["trust_critical_changed"])

    def test_44_38_preflight_never_sees_provider_credentials_and_calls_no_provider(self):
        protect_env([SECRET])
        os.environ[SECRET] = SECRET_VALUE
        self.addCleanup(os.environ.pop, SECRET, None)
        # A provider whose executable does not exist: built and validated, never called.
        self.deployment.preflight_args = ["--cognition", "claude", "--provider-opt",
                                          f"claude.executable={self.base / 'no-such-claude'}"]
        step = self.deploy(self.candidate())  # the candidate's smoke test fails if it sees the secret
        self.assertTrue(step.result.output["switched"], step.result.output)
        dump = "\n".join(sqlite3.connect(self.db).iterdump())
        self.assertNotIn(SECRET_VALUE, dump)
        self.assertNotIn(SECRET_VALUE, json.dumps(step.result.output))
        context = render_situation(build_situation(self.runtime.context()))
        self.assertNotIn(SECRET_VALUE, context)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET_VALUE.encode(), path.read_bytes(), path)
        self.assertNotIn("claude", (PACKAGE / "deploy.py").read_text().lower())


# -- 15-18: database lock and tolerant readers -------------------------------------------


def kairo_cmd(*args):
    return [sys.executable, "-m", "kairo", *args]


class LockTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "kairo.db"

    def start_run(self):
        proc = subprocess.Popen(kairo_cmd("--run", "--db", str(self.db), "--socket",
                                          str(self.dir / "k.sock"), "--reassess", "0"),
                                env=_env(PACKAGE.parent), stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        deadline = time.monotonic() + 15
        while not (self.dir / "k.sock").exists():
            self.assertLess(time.monotonic(), deadline, "runtime did not start")
            time.sleep(0.05)
        return proc

    def once(self, db):
        return subprocess.run(kairo_cmd("--db", str(db)), env=_env(PACKAGE.parent),
                              capture_output=True, text=True, timeout=30)

    def test_15_16_one_database_one_runtime(self):
        live = self.start_run()
        memory = Memory(self.db)
        memory.put("action", "live", {"id": "live", "kind": "process.run", "status": "started"})
        second = self.once(self.db)
        self.assertEqual(second.returncode, 2)
        self.assertIn("another Kairo runtime is using", second.stderr)
        rerun = subprocess.run(kairo_cmd("--run", "--db", str(self.db), "--socket",
                                         str(self.dir / "other.sock")),
                               env=_env(PACKAGE.parent), capture_output=True, text=True, timeout=30)
        self.assertEqual(rerun.returncode, 2)
        self.assertEqual(memory.get("action", "live")["status"], "started")  # not "interrupted"
        self.assertEqual(self.once(self.dir / "independent.db").returncode, 0)  # per database
        situation = subprocess.run(kairo_cmd("--situation", "--db", str(self.db)),
                                   env=_env(PACKAGE.parent), capture_output=True, timeout=30)
        self.assertEqual(situation.returncode, 0)  # a read-only view is not a runtime
        live.send_signal(signal.SIGTERM)
        self.assertEqual(live.wait(15), 0)
        self.assertEqual(self.once(self.db).returncode, 0)  # released on exit
        memory.close()

    def test_15_lock_is_released_when_the_owner_dies(self):
        live = self.start_run()
        live.kill()
        live.wait(15)
        self.assertEqual(self.once(self.db).returncode, 0)

    def test_15_lock_in_process(self):
        fd = lock_database(self.db)
        with self.assertRaises(DatabaseLocked):
            lock_database(self.db)
        os.close(fd)
        os.close(lock_database(self.db))


class TolerantReaderTest(unittest.TestCase):
    def test_17_unknown_fields_are_ignored(self):
        memory = Memory()
        memory.put("directive", "d", {"statement": "s", "active": True, "id": "d",
                                      "priority": 7, "added_in": "v2"})
        memory.put("work", "w", {"objective": "o", "why": "y", "id": "w", "state": "active",
                                 "focus": "high"})
        memory.put("message", "m", {"sender": "human", "text": "hi", "at": 1.0, "id": "m",
                                    "channel": "x"})
        runtime = Runtime(memory)
        context = runtime.context()
        self.assertEqual([d.statement for d in context.directives], ["s"])
        self.assertEqual([m.text for m in context.messages], ["hi"])
        self.assertEqual(context.runtime["unreadable_records"], [])
        self.assertEqual([w.id for w in runtime.work.open()], ["w"])
        outcome = runtime.work.apply([update("w", understanding="still workable")])
        self.assertEqual(outcome.rejected, [])
        self.assertNotIn("focus", memory.get("work", "w"))  # documented: an older writer drops it

    def test_18_known_fields_of_the_wrong_type_are_still_corruption(self):
        for data in ({"statement": 5, "id": "d"}, {"statement": "s", "active": "yes", "id": "d"},
                     {"statement": "s", "created_at": "today", "id": "d"}, "not an object"):
            with self.assertRaises((TypeError, ValueError), msg=data):
                from_record(Directive, data)
        with self.assertRaises(ValueError):
            work_from_record({"objective": "o", "why": "y", "state": "sideways"} | {"id": 5})
        with self.assertRaises(TypeError):  # a required field missing
            from_record(Directive, {"id": "d"})
        # Fields Phase 6 shows as unknown rather than rejecting stay tolerated.
        work = work_from_record({"objective": "o", "why": "y", "understanding_at": "yesterday"})
        self.assertEqual(work.understanding_at, "yesterday")
        memory = Memory()
        memory.put("work", "bad", {"objective": 5, "why": "y", "id": "bad"})
        self.assertEqual(WorkLedger(memory).all(), [])  # skipped as corrupt, as before


# -- 19-35, 40, 41: the runtime side, with a stand-in for the release layout -----------


class FakeDeployment:
    """The runtime's view of a deployment configuration (running release, facts,
    the deploy step), without git or preflight."""

    def __init__(self, running: Path, target: str = "b" * 40, switch: bool = True):
        self.running = running
        self.target = target
        self.do_switch = switch
        self.calls = 0

    @property
    def running_revision(self):
        return self.running.name if self.running else None

    def facts(self):
        return {"running": {"revision": self.running_revision, "release": str(self.running),
                            "digest": "d" * 64},
                "current": {"revision": self.target}, "previous": {"revision": None},
                "repository": {"path": "/dev/repo", "head": self.target, "branch": "kairo",
                               "dirty_files": 0, "head_is_running": False}}

    def deploy(self, action):
        self.calls += 1
        if not self.do_switch:
            return ActionResult(action.id, executed=True, output={
                "switched": False, "stage": "tests", "error": "suite failed", "from":
                self.running_revision, "to": action.params["revision"]})
        return ActionResult(action.id, executed=True, output={
            "switched": True, "stage": "switch", "from": self.running_revision,
            "to": action.params["revision"]}, restart=True)


def release_dir(base: Path, sha: str) -> Path:
    path = base / sha
    path.mkdir(parents=True, exist_ok=True)
    (path / "f").write_text(sha)
    return path


@dataclass
class Plan:
    decisions: list
    contexts: list = field(default_factory=list)
    name: str = "plan"

    def decide(self, context):
        self.contexts.append(context)
        step = self.decisions[min(len(self.contexts), len(self.decisions)) - 1]
        if isinstance(step, Exception):
            raise step
        return step(context) if callable(step) else step


def deploy_action(sha, work=None):
    return Action("runtime.deploy", {"revision": sha}, reason="deploy", work_id=work)


class RestartTest(unittest.TestCase):
    A, B = "a" * 40, "b" * 40

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "kairo.db"

    def process(self, running_sha, cognition, **fake):
        """One Kairo 'process': a Runtime on the shared database, running a release."""
        memory = Memory(self.db)
        self.addCleanup(memory.close)
        deployment = FakeDeployment(release_dir(self.dir / "releases", running_sha), **fake)
        return Runtime(memory, Environment(deployment=deployment), cognition=cognition)

    def run_loop(self, runtime, timeout=10):
        thread = threading.Thread(target=runtime.run_forever, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            runtime.request_stop()
            thread.join(5)
        return thread

    def deploy_from_a(self, work_and_more=True):
        """Process A: work A and B exist; B's attempt deploys revision B."""
        first = Plan([Decision(work=[create("a", "Research the external system"),
                                     create("b", "Fix interrupted-action reconstruction")],
                               actions=[deploy_action(self.B, "b"),
                                        Action("process.run", {"argv": ["true"]}, reason="after")],
                               sleep=True)])
        runtime = self.process(self.A, first)
        thread = self.run_loop(runtime)
        self.assertFalse(thread.is_alive())
        return runtime

    def test_19_29_deploy_persists_everything_then_exits_75(self):
        runtime = self.deploy_from_a()
        self.assertEqual(runtime.exit_code, deploy.RESTART_EXIT)
        self.assertIs(runtime.state, State.STOPPED)
        records = runtime.memory.all("action")
        self.assertEqual([r["kind"] for r in records], ["runtime.deploy"])  # nothing after it ran
        [record] = records
        self.assertEqual(record["status"], "finished")  # never left "started"/interrupted
        self.assertEqual(action_state(record), "awaiting_confirmation")
        [cycle] = runtime.memory.all("cycle")
        self.assertEqual(cycle["actions"][0]["kind"], "runtime.deploy")
        lifecycle = runtime.memory.get("runtime", "lifecycle")
        self.assertEqual(lifecycle["state"], "stopped")
        self.assertEqual(lifecycle["restart_for"], record["id"])
        self.assertIn("restart requested by deployment", lifecycle["reason"])

    def test_20_a_human_stop_exits_normally(self):
        runtime = self.process(self.A, Plan([Decision(sleep=True)]))
        thread = threading.Thread(target=runtime.run_forever, daemon=True)
        thread.start()
        runtime.wait_for(State.SLEEPING, 5)
        runtime.request_stop()
        thread.join(5)
        self.assertEqual(runtime.exit_code, 0)
        self.assertNotIn("restart_for", runtime.memory.get("runtime", "lifecycle"))

    def test_21_24_30_31_successor_confirms_and_work_survives(self):
        old = self.deploy_from_a()
        works_before = {w["id"]: w for w in old.memory.all("work")}
        [record] = old.memory.all("action")
        evidence_attempt = Decision(work=[set_state(next(i for i, w in works_before.items()
                                                         if w["objective"].startswith("Fix")),
                                                    "completed", "done", evidence=[record["id"]])],
                                    sleep=True)
        new = self.process(self.B, Plan([evidence_attempt, Decision(sleep=True)]))
        new.start()
        pending = new.memory.get("action", record["id"])
        self.assertEqual(action_state(pending), "awaiting_confirmation")  # started is not confirmed
        self.assertEqual(pending["verification"]["evidence"]["successor_started"]["revision"], self.B)
        self.assertIn("restarted after deployment", new.reason)
        report = new.cycle()
        # 22: in that same cycle the deployment was still awaiting confirmation: not evidence.
        [rejected] = report.cognition["work"]["rejected"]
        self.assertIn("awaiting_confirmation", rejected["reason"])
        confirmed = new.memory.get("action", record["id"])
        self.assertEqual(action_state(confirmed), "verified_successful")
        ev = confirmed["verification"]["evidence"]
        self.assertEqual((ev["stage"], ev["revision"]), ("confirmation", self.B))
        for key in ("digest", "process_started_at", "cycle_at"):
            self.assertIsNotNone(ev[key], key)
        works_after = {w["id"]: w for w in new.memory.all("work")}
        self.assertEqual(works_after, works_before)  # 30/31: both work items exactly as they were
        code = build_situation(new.context())["kairo"]["code"]
        self.assertEqual((code["running"]["revision"], code["running"]["status"]),
                         (self.B, "confirmed"))

    def test_23_a_successor_running_another_revision_fails_the_deployment(self):
        old = self.deploy_from_a()
        [record] = old.memory.all("action")
        fallen_back = self.process(self.A, Plan([Decision(sleep=True)]))  # previous release runs
        fallen_back.start()
        failed = fallen_back.memory.get("action", record["id"])
        self.assertEqual(action_state(failed), "verified_failed")
        ev = failed["verification"]["evidence"]
        self.assertEqual((ev["stage"], ev["running"], ev["target"]), ("confirmation", self.A, self.B))
        fallen_back.cycle()
        self.assertEqual(action_state(fallen_back.memory.get("action", record["id"])),
                         "verified_failed")  # a later usable cycle cannot confirm it

    def test_25_probation_code_failure_exits_external_failure_waits(self):
        old = self.deploy_from_a()
        [record] = old.memory.all("action")
        external = self.process(self.B, Plan([CognitionError("unavailable", "down")]))
        external.start()
        external.cycle()
        self.assertEqual(external.exit_code, 0)
        self.assertFalse(external._stop_requested)
        self.assertEqual(action_state(external.memory.get("action", record["id"])),
                         "awaiting_confirmation")
        external.stop()
        broken = self.process(self.B, Plan([CognitionError("invalid_output", "garbage")]))
        thread = self.run_loop(broken)
        self.assertFalse(thread.is_alive())
        self.assertEqual(broken.exit_code, deploy.PROBATION_EXIT)
        rec = broken.memory.get("action", record["id"])
        self.assertEqual(action_state(rec), "awaiting_confirmation")
        self.assertEqual(rec["verification"]["evidence"]["probation_failure"]["failure"],
                         "invalid_output")

    def test_no_cognition_confirms_after_a_completed_cycle(self):
        old = self.deploy_from_a()
        [record] = old.memory.all("action")
        new = self.process(self.B, None)
        new.start()
        new.cycle()
        self.assertEqual(action_state(new.memory.get("action", record["id"])),
                         "verified_successful")

    def test_32_33_34_failed_deploy_is_ordinary_evidence_and_not_blindly_repeated(self):
        plan = Plan([
            Decision(work=[create("b", "Fix a recovery bug")], actions=[deploy_action(self.B, "b")],
                     sleep=False),
            lambda ctx: Decision(actions=[deploy_action(self.B, ctx.open_work[0]["id"])], sleep=False),
            lambda ctx: Decision(work=[update(ctx.open_work[0]["id"],
                                              understanding="the suite failed on X; fixed X")],
                                 actions=[deploy_action(self.B, ctx.open_work[0]["id"])], sleep=True),
        ])
        runtime = self.process(self.A, plan, switch=False)
        runtime.start()
        runtime.cycle()
        s = build_situation(runtime.context())
        [work] = s["work"]["open"]
        self.assertEqual(work["recovery"]["latest_failure"]["failure"], "verification_failed")
        report = runtime.cycle()
        [refused] = report.cognition["work"]["rejected"]
        self.assertEqual(refused["op"], "action_refused")
        self.assertEqual(runtime.environment.deployment.calls, 1)
        runtime.cycle()  # understanding changed: a justified retry runs
        self.assertEqual(runtime.environment.deployment.calls, 2)

    def test_35_40_interrupted_deploy_is_indeterminate_and_not_blindly_repeated(self):
        runtime = self.process(self.A, None)
        work = runtime.work.apply([create("b", "Fix it")]).refs["b"]
        runtime.memory.put("action", "cut", {
            "id": "cut", "kind": "runtime.deploy", "params": {"revision": self.B}, "work_id": work,
            "reason": "deploy", "status": "started", "started_at": time.time()})
        runtime.start()
        record = runtime.memory.get("action", "cut")
        self.assertEqual(action_state(record), "interrupted")
        self.assertIn("interrupted", INDETERMINATE)
        identity = attempt_identity("runtime.deploy", redact({"revision": self.B}))
        self.assertIsNotNone(runtime.work.unsettled_repeat(work, identity))
        code = build_situation(runtime.context())["kairo"]["code"]  # the actual state to check
        self.assertEqual(code["running"]["revision"], self.A)
        self.assertEqual(code["current_link"], self.B)
        self.assertEqual(code["recent_deployments"][-1]["state"], "interrupted")
        # 40: no cognition, nothing to do: no work, no action, no deployment is invented.
        before = (runtime.memory.count("work"), runtime.memory.count("action"))
        for _ in range(3):
            runtime.cycle()
            runtime.wake("timer")
        self.assertEqual((runtime.memory.count("work"), runtime.memory.count("action")), before)

    def test_41_a_confirmed_deployment_creates_nothing_further(self):
        old = self.deploy_from_a()
        idle = Plan([Decision(sleep=True)])
        new = self.process(self.B, idle)
        thread = threading.Thread(target=new.run_forever, daemon=True)
        thread.start()
        new.wait_for(State.SLEEPING, 5)
        counts = {k: new.memory.count(k) for k in ("action", "work")}
        time.sleep(0.3)
        self.assertIs(new.state, State.SLEEPING)  # sleeps; no follow-up maintenance
        self.assertEqual(len(idle.contexts), 1)
        self.assertEqual({k: new.memory.count(k) for k in ("action", "work")}, counts)
        self.assertEqual(counts["action"], 1)
        new.request_stop()
        thread.join(5)

    def test_39_code_context_is_bounded_and_only_present_when_configured(self):
        runtime = self.process(self.A, None)
        for i in range(8):
            runtime.memory.put("action", f"d{i}", {
                "id": f"d{i}", "kind": "runtime.deploy", "params": {"revision": self.B},
                "status": "finished", "started_at": i, "finished_at": i,
                "result": {"executed": True, "output": {"from": self.A, "to": self.B,
                                                        "stage": "tests", "switched": False,
                                                        "stages": [{"summary": "x" * 600}] * 5}},
                "verification": {"outcome": "failure", "evidence": {"stage": "tests"}}})
        situation = build_situation(runtime.context())
        code = situation["kairo"]["code"]
        self.assertEqual(len(code["recent_deployments"]), 5)
        self.assertLess(len(json.dumps(code)), 2500)
        self.assertNotIn("stages", json.dumps(code))
        plain = Runtime(Memory())
        self.assertNotIn("code", build_situation(plain.context())["kairo"])
        self.assertNotIn("runtime.deploy", plain.environment.actions())


class ImplementationMaintenanceTest(unittest.TestCase):
    def test_37_implementation_changes_need_no_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            impls = Path(tmp) / "implementations"
            pkg = impls / "tool-pkg"
            pkg.mkdir(parents=True)
            (pkg / "run.sh").write_text("#!/bin/sh\necho v1\n")
            (pkg / "run.sh").chmod(0o755)
            (pkg / "implementation.json").write_text(json.dumps({
                "kairo_implementation": 1, "id": "tool-pkg", "description": "d",
                "tools": [{"name": "go", "description": "run it", "run": ["./run.sh"]}]}))
            deployment = FakeDeployment(release_dir(Path(tmp) / "releases", "a" * 40))
            runtime = Runtime(Memory(), Environment(Implementations(impls, "all"), deployment))
            first = runtime.act(Action("impl.tool-pkg.go"))
            (pkg / "run.sh").write_text("#!/bin/sh\necho v2\n")  # maintained in place
            second = runtime.act(Action("impl.tool-pkg.go"))
            self.assertEqual((first.result.output["stdout"], second.result.output["stdout"]),
                             ("v1\n", "v2\n"))
            records = runtime.memory.all("action")
            self.assertNotEqual(records[0]["implementation"]["digest"],
                                records[1]["implementation"]["digest"])
            self.assertEqual(deployment.calls, 0)
            self.assertEqual(runtime.exit_code, 0)


# -- 26-29: the external fallback --------------------------------------------------------


class FallbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for sha in ("a" * 40, "b" * 40):
            release_dir(self.root / "releases", sha)
            (self.root / "releases" / sha / ".kairo-release").write_text("{}")
        self.calls = self.root / "systemctl.log"
        stub = self.root / "systemctl"
        stub.write_text(f"#!/bin/sh\necho \"$@\" >> {self.calls}\n")
        stub.chmod(0o755)
        self.env = {**os.environ, "KAIRO_SYSTEMCTL": str(stub)}

    def fallback(self):
        return subprocess.run(["sh", str(FALLBACK), str(self.root), "kairo-test.service"],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def links(self, current=None, previous=None):
        for name, sha in (("current", current), ("previous", previous)):
            if sha:
                _replace_link(self.root, name, f"releases/{sha * 40}")

    def current(self):
        return os.readlink(self.root / "current")

    def started(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_27_28_switches_to_previous_once(self):
        self.links(current="b", previous="a")
        proc = self.fallback()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.current(), "releases/" + "a" * 40)
        self.assertEqual(self.started(), ["reset-failed kairo-test.service",
                                          "start --no-block kairo-test.service"])
        again = self.fallback()  # the previous release fails too: no second switch, no loop
        self.assertEqual(again.returncode, 0)
        self.assertIn("leaving kairo-test.service stopped", again.stderr)
        self.assertEqual(self.current(), "releases/" + "a" * 40)
        self.assertEqual(len(self.started()), 2)
        self.assertFalse(list(self.root.glob(".current*")))

    def test_29_no_valid_previous_leaves_kairo_stopped(self):
        self.links(current="b")
        self.assertEqual(self.fallback().returncode, 0)
        _replace_link(self.root, "previous", "releases/not-a-release")
        self.assertEqual(self.fallback().returncode, 0)
        self.assertEqual(self.current(), "releases/" + "b" * 40)
        self.assertEqual(self.started(), [])

    def test_the_fallback_is_tiny_and_never_touches_the_database(self):
        text = FALLBACK.read_text()
        code = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertLess(len(code), 30)
        for forbidden in ("sqlite", ".db", "snapshot", "while ", "for ", "python", "claude"):
            self.assertNotIn(forbidden, "\n".join(code))
        unit = (ROOT / "deploy" / "kairo.service").read_text()
        for line in ("Restart=on-failure", "RestartMode=direct", "RestartForceExitStatus=75",
                     "SuccessExitStatus=75",
                     "StartLimitBurst=", "OnFailure=kairo-fallback.service"):
            self.assertIn(line, unit)


# -- 50: the mandatory real-process end-to-end test -------------------------------------

FAKE_COGNITION = r'''#!{python}
"""Fake provider executable for the end-to-end test: a scripted plan of decisions,
one per call, with placeholders filled from the situation it is shown."""
import json, os, re, subprocess, sys
plan = json.load(open(os.environ["E2E_PLAN"]))
log = os.environ["E2E_LOG"]
os.makedirs(log, exist_ok=True)
n = len(os.listdir(log))
prompt = sys.stdin.read()
with open(os.path.join(log, "call-%03d.json" % n), "w") as f:
    f.write(prompt.split("\n", 1)[1])
situation = json.loads(prompt.split("\n", 1)[1])
step = plan[min(n, len(plan) - 1)]
head = subprocess.run(["git", "-C", os.environ["E2E_REPO"], "rev-parse", "HEAD"],
                      capture_output=True, text=True).stdout.strip()

def work(objective):
    return next(w["id"] for w in situation["work"]["open"] if w["objective"] == objective)

def action(kind, purpose):
    return next(a["id"] for a in reversed(situation["history"]["actions"]["items"])
                if a["kind"] == kind and (not purpose or a["purpose"] == purpose))

def fill(v):
    if isinstance(v, list):
        return [fill(x) for x in v]
    if isinstance(v, dict):
        return {{k: fill(x) for k, x in v.items()}}
    if v == "{{HEAD}}":
        return head
    if isinstance(v, str) and (m := re.fullmatch(r"\{{work:(.+)\}}", v)):
        return work(m.group(1))
    if isinstance(v, str) and (m := re.fullmatch(r"\{{action:([^:]+):(.*)\}}", v)):
        return action(m.group(1), m.group(2))
    return v

decision = fill(step)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps(decision), "structured_output": decision,
                  "num_turns": 1, "duration_api_ms": 1, "total_cost_usd": 0,
                  "modelUsage": {{"fake": {{}}}}}}))
'''


def d(actions=(), work=(), sleep=True, reason="assessed"):
    return {"reason": reason, "actions": list(actions), "replies": [], "sleep": sleep,
            "wake_after": None, "work": list(work)}


def act(kind, params, work=None, reason="attempt"):
    return {"kind": kind, "params": params, "reason": reason, "work": work}


def w_create(ref, objective):
    return {"op": "create", "ref": ref, "objective": objective, "why": "observed",
            "directive_id": None, "strategy": "diagnose, fix, test, deploy",
            "next_step": "start"}


def w_update(work, understanding):
    return {"op": "update", "work_id": work, "understanding": understanding, "strategy": None,
            "next_step": None}


def w_complete(work, evidence):
    return {"op": "set_state", "work_id": work, "state": "completed", "reason": "fixed and running",
            "wait_seconds": None, "evidence": evidence}


WORK_A, WORK_B, WORK_C = "Research the external system", "Fix the runtime defect", "Ship a candidate"
PY = sys.executable


def e2e_plan(repo: Path):
    marker = "open('src/kairo/e2e_marker.py', 'w').write('FIXED = True\\n')"
    regression = ("open('tests/test_marker.py', 'w').write('import unittest\\nfrom kairo import "
                  "e2e_marker\\nclass T(unittest.TestCase):\\n    def test_fixed(self):\\n"
                  "        self.assertTrue(e2e_marker.FIXED)\\n')")
    commit = ["sh", "-c", "git add -A && git -c user.name=Kairo -c user.email=kairo@localhost "
                          "-c commit.gpgsign=false commit -qm 'Fix the runtime defect'"]
    tests = ["env", "PYTHONPATH=src", PY, "-m", "unittest", "discover", "-s", "tests", "-t", "tests"]
    cwd = str(repo)
    b = "{work:" + WORK_B + "}"
    c = "{work:" + WORK_C + "}"
    return [
        # 0 (release A): two pieces of ordinary work.
        d(work=[w_create("a", WORK_A), w_create("b", WORK_B)], sleep=False),
        # 1: fix in the development repository, regression test, commit, run the tests.
        d(actions=[act("process.run", {"argv": [PY, "-c", marker], "cwd": cwd}, b, "fix"),
                   act("process.run", {"argv": [PY, "-c", regression], "cwd": cwd}, b, "test"),
                   act("process.run", {"argv": commit, "cwd": cwd}, b, "commit"),
                   act("process.run", {"argv": tests, "cwd": cwd}, b, "run tests")],
          work=[w_update(b, "reproduced and fixed; regression test added")], sleep=False),
        # 2: deploy the commit. The process exits 75 after this cycle.
        d(actions=[act("runtime.deploy", {"revision": "{HEAD}"}, b, "deploy")], sleep=False),
        # 3 (release B): the first usable cycle confirms the deployment.
        d(sleep=False, reason="check the deployment"),
        # 4: complete B with the confirmed deployment and the test run as evidence.
        d(work=[w_complete(b, ["{action:runtime.deploy:deploy}",
                               "{action:process.run:run tests}"])]),
        # 5 (woken): a candidate whose own suite fails is refused by preflight.
        d(work=[w_create("c", WORK_C)],
          actions=[act("runtime.deploy", {"revision": "{HEAD}"}, "c", "deploy")], sleep=False),
        # 6: the identical deploy, without reassessing, is refused by the runtime.
        d(actions=[act("runtime.deploy", {"revision": "{HEAD}"}, c, "deploy")]),
        # 7 (woken): reassessed; deploy the new candidate (it crashes after the switch).
        d(work=[w_update(c, "the failing test was fixed in a new commit")],
          actions=[act("runtime.deploy", {"revision": "{HEAD}"}, c, "deploy")], sleep=False),
        # 8 (release B again, after the fallback): nothing more.
        d(),
    ]


class Supervisor(threading.Thread):
    """Applies deploy/kairo.service's rules to real Kairo processes: start the
    release 'current' selects; exit 75 restarts at once; exit 0 stays stopped;
    other exits restart until the start limit, then the real kairo-fallback runs
    (with systemctl stubbed: this loop is the service manager)."""

    LIMIT = 3

    def __init__(self, root, args, env):
        super().__init__(daemon=True)
        self.root, self.args, self.env = root, args, env
        self.exits = []      # (release revision, exit status)
        self.fallbacks = []  # (stdout+stderr, current before, current after)
        self.proc = None

    def run(self):
        failures = 0
        while True:
            release = (self.root / "current").resolve()
            self.proc = subprocess.Popen([PY, "-m", "kairo", "--run", *self.args],
                                         env={**self.env, "PYTHONPATH": str(release / "src")},
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         text=True)
            _, err = self.proc.communicate()
            self.exits.append((release.name, self.proc.returncode, err[-2000:]))
            if self.proc.returncode == 0:
                return
            if self.proc.returncode == deploy.RESTART_EXIT:
                failures = 0
                continue
            failures += 1
            if failures < self.LIMIT:
                continue
            before = os.readlink(self.root / "current")
            proc = subprocess.run(["sh", str(FALLBACK), str(self.root), "kairo.service"],
                                  env={**os.environ, "KAIRO_SYSTEMCTL": "true"},
                                  capture_output=True, text=True, timeout=30)
            after = os.readlink(self.root / "current")
            self.fallbacks.append((proc.stderr, before, after))
            failures = 0
            if after == before:
                return  # nothing to fall back to: stay stopped


class EndToEndTest(unittest.TestCase):
    TIMEOUT = 180

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "deploy"
        self.addCleanup(_remove, self.root)
        self.repo = Repo(self.base)
        self.db = self.base / "kairo.db"
        self.sock = self.base / "kairo.sock"
        self.log = self.base / "calls"
        fake = self.base / "fake-provider"
        fake.write_text(FAKE_COGNITION.format(python=PY))
        fake.chmod(0o755)
        plan = self.base / "plan.json"
        plan.write_text(json.dumps(e2e_plan(self.repo.path)))
        self.env = _env(PACKAGE.parent, E2E_PLAN=str(plan), E2E_LOG=str(self.log),
                        E2E_REPO=str(self.repo.path))
        self.args = ["--db", str(self.db), "--socket", str(self.sock), "--reassess", "0",
                     "--repository", str(self.repo.path), "--releases", str(self.root),
                     "--cognition", "claude", "--provider-opt", f"claude.executable={fake}"]
        self.max_runtimes = 0
        self._watching = True
        self.watcher = threading.Thread(target=self._watch, daemon=True)
        self.watcher.start()
        self.addCleanup(self._stop_watching)

    def _watch(self):
        """Count live Kairo runtimes on this database, all the time. Between fork
        and exec, a runtime's own subprocesses briefly show its command line; a
        process counts only if it is seen in two consecutive samples, which any
        real runtime (even one the lock refuses) is."""
        previous: set = set()
        while self._watching:
            matching = set()
            for pid in os.listdir("/proc"):
                try:
                    cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                except (OSError, ValueError):
                    continue
                if b"--run" in cmd and str(self.db).encode() in cmd and b"kairo" in cmd:
                    matching.add(pid)
            runtimes = matching & previous
            if len(runtimes) > self.max_runtimes:
                self.max_runtimes = len(runtimes)
                self.seen = sorted(runtimes)
            previous = matching
            time.sleep(0.02)

    def _stop_watching(self):
        self._watching = False
        self.watcher.join(5)

    def calls(self):
        return len(list(self.log.iterdir())) if self.log.exists() else 0

    def db_get(self, kind, id=None):
        with sqlite3.connect(f"file:{self.db}?mode=ro", uri=True) as c:
            if id is not None:
                row = c.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
                return json.loads(row[0]) if row else None
            return [json.loads(r[0]) for r in
                    c.execute("SELECT data FROM records WHERE kind=? ORDER BY seq", (kind,))]

    def wait(self, what, predicate):
        deadline = time.monotonic() + self.TIMEOUT
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (sqlite3.Error, OSError, json.JSONDecodeError, StopIteration, KeyError):
                pass
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}; exits: {self.supervisor.exits}")

    def sleeping_after(self, calls):
        return lambda: (self.calls() >= calls and
                        (self.db_get("runtime", "lifecycle") or {}).get("state") == "sleeping"
                        and self.sock.exists())

    def message(self, text):
        from kairo.ipc import request
        response = request(self.sock, {"op": "message", "text": text})
        self.assertTrue(response["ok"], response)

    def deploys(self):
        return [a for a in self.db_get("action") if a["kind"] == "runtime.deploy"]

    def work(self, objective):
        return next(w for w in self.db_get("work") if w["objective"] == objective)

    def situation(self, call):
        return json.loads((self.log / f"call-{call:03d}.json").read_text())

    def test_self_maintenance_deploy_restart_confirm_and_fallback(self):
        # A. The operator's first release, started under supervision.
        sha_a = bootstrap(self.repo, self.root).name
        self.supervisor = Supervisor(self.root, self.args, self.env)
        self.supervisor.start()
        self.addCleanup(lambda: self.supervisor.proc and self.supervisor.proc.poll() is None
                        and self.supervisor.proc.kill())

        # B-M. Work A and B; B fixes, tests, commits, deploys; the restart confirms it.
        self.wait("the deployment to be confirmed and work B completed", self.sleeping_after(5))
        sha_b = self.repo.head
        self.assertNotEqual(sha_b, sha_a)
        self.assertEqual([(r, c) for r, c, _ in self.supervisor.exits], [(sha_a, 75)])
        [deployed] = self.deploys()
        self.assertEqual(action_state(deployed), "verified_successful")
        ev = deployed["verification"]["evidence"]
        self.assertEqual((ev["stage"], ev["revision"]), ("confirmation", sha_b))
        self.assertEqual(deployed["result"]["output"]["to"], sha_b)
        self.assertEqual(os.readlink(self.root / "current"), f"releases/{sha_b}")
        self.assertEqual(os.readlink(self.root / "previous"), f"releases/{sha_a}")
        before_confirmation = self.situation(3)["kairo"]["code"]
        self.assertEqual(before_confirmation["running"]["revision"], sha_b)
        self.assertEqual(before_confirmation["running"]["status"], "awaiting_confirmation")
        self.assertEqual(self.situation(4)["kairo"]["code"]["running"]["status"], "confirmed")
        self.assertTrue(self.situation(4)["kairo"]["code"]["repository"]["head_is_running"])
        # N. Work B is completed on verified deployment evidence.
        work_b = self.work(WORK_B)
        self.assertEqual((work_b["state"], work_b["completion_basis"]), ("completed", "verified"))
        self.assertEqual({e["state"] for e in work_b["evidence"]},
                         {"verified_successful", "executed_unverified"})
        # O. Work A is exactly as it was created.
        work_a = self.work(WORK_A)
        self.assertEqual((work_a["state"], work_a["history"][0]["event"], len(work_a["history"])),
                         ("active", "created", 1))
        # P. A second runtime on the same database is refused while this one runs.
        second = subprocess.run([PY, "-m", "kairo", "--db", str(self.db)],
                                env={**self.env, "PYTHONPATH": str((self.root / "current").resolve() / "src")},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 2, second.stderr)

        # Q-R. A candidate whose own suite fails: preflight refuses, nothing switches.
        self.repo.write("tests/test_fail.py", "import unittest\nclass T(unittest.TestCase):\n"
                        "    def test_x(self):\n        self.fail('broken candidate')\n")
        sha_c1 = self.repo.commit("broken candidate")
        self.message("a new candidate is committed")
        self.wait("the refused deployment and the refused repeat", self.sleeping_after(7))
        refused = self.deploys()[-1]
        self.assertEqual(refused["params"]["revision"], sha_c1)
        self.assertEqual(action_state(refused), "verified_failed")
        self.assertEqual(refused["result"]["output"]["stage"], "tests")
        self.assertEqual(len(self.deploys()), 2)  # the blind repeat never ran
        last_cycle = self.db_get("cycle")[-1]
        self.assertEqual(last_cycle["cognition"]["work"]["rejected"][0]["op"], "action_refused")
        self.assertEqual(os.readlink(self.root / "current"), f"releases/{sha_b}")

        # S-Y. A candidate that passes preflight but cannot start: switch, crash,
        # one fallback to the previous release, which records the deployment failed.
        git(self.repo.path, "rm", "-q", "tests/test_fail.py")
        self.repo.edit("src/kairo/__main__.py", "    logging.basicConfig(level=logging.INFO",
                       "    raise SystemExit('e2e: this release cannot start')\n"
                       "    logging.basicConfig(level=logging.INFO")
        sha_c2 = self.repo.commit("candidate that cannot start")
        self.message("fixed the failing test")
        self.wait("the fallback and the previous release running again", self.sleeping_after(9))
        exits = [(r, c) for r, c, _ in self.supervisor.exits]
        self.assertEqual(exits[:2], [(sha_a, 75), (sha_b, 75)])
        self.assertEqual(exits[2:], [(sha_c2, 1)] * Supervisor.LIMIT)
        self.assertEqual(len(self.supervisor.fallbacks), 1)
        _, before, after = self.supervisor.fallbacks[0]
        self.assertEqual((before, after), (f"releases/{sha_c2}", f"releases/{sha_b}"))
        failed = self.deploys()[-1]
        self.assertEqual(failed["params"]["revision"], sha_c2)
        self.assertEqual(action_state(failed), "verified_failed")
        ev = failed["verification"]["evidence"]
        self.assertEqual((ev["stage"], ev["running"], ev["target"]), ("confirmation", sha_b, sha_c2))
        self.assertIn("M src/kairo/__main__.py", failed["result"]["output"]["trust_critical_changed"])
        self.assertEqual(self.work(WORK_A), work_a)  # X. untouched through all of it
        code = self.situation(8)["kairo"]["code"]
        self.assertEqual((code["running"]["revision"], code["current_link"], code["previous"]),
                         (sha_b, sha_b, sha_b))
        # Y. A second fallback would find current == previous and do nothing.
        again = subprocess.run(["sh", str(FALLBACK), str(self.root)], capture_output=True,
                               text=True, env={**os.environ, "KAIRO_SYSTEMCTL": "false"})
        self.assertIn("leaving kairo.service stopped", again.stderr)
        self.assertEqual(os.readlink(self.root / "current"), f"releases/{sha_b}")

        # A human stop ends supervision normally.
        from kairo.ipc import request
        request(self.sock, {"op": "stop"})
        self.supervisor.join(30)
        self.assertFalse(self.supervisor.is_alive())
        self.assertEqual(self.supervisor.exits[-1][:2], (sha_b, 0))
        self.assertEqual(self.max_runtimes, 1, getattr(self, "seen", None))  # never two at once
        self.assertEqual(self.db_get("runtime", "lifecycle")["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
