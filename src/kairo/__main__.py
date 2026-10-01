"""Command-line entry point.

``python -m kairo [--db PATH]``
    Start, run one cycle, print the resulting state, stop.

``python -m kairo --situation [--db PATH]``
    Print the situation cognition would be shown now (bounded and redacted),
    without starting the runtime or calling any provider.

``python -m kairo --run [--db PATH] [--socket PATH] [--reassess SECONDS] [--cognition ORDER]``
    Operate continuously in the foreground until SIGINT (Ctrl-C), SIGTERM or
    an IPC stop request. Other local processes reach it through the Unix
    socket (see ``kairo.ipc``). Without ``--cognition`` Kairo has no cognition
    and simply sleeps between wakes. Exit status: 0 after a stop, 75 when a
    deployment selected a new release (the supervisor restarts it), 3 when a
    just-deployed release proved unusable before confirming itself.

``--repository PATH --releases PATH``
    Configure self-deployment (see ``kairo.deploy``): the development repository
    and the release layout the supervisor runs from. Adds ``runtime.deploy``.

``python -m kairo --init-release SHA --repository PATH --releases PATH``
    Operator bootstrap: build the first release and select it as ``current``.

``python -m kairo --preflight --db COPY``
    A candidate release's dry cycle against a copy of the database: start, one
    cycle with no provider call and no action, stop. Used by runtime.deploy.

``--run``, ``--once`` (the default) and ``--preflight`` take an exclusive lock on
the database: a second runtime on the same database fails immediately.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path

from kairo.cognition import Cognition
from kairo.deploy import Deployment, PreflightCognition, init_release
from kairo.environment import Environment
from kairo.implementations import ID, Implementations
from kairo.registry import PROVIDERS, build_cognition
from kairo.ipc import DEFAULT_SOCKET, IPCError, IPCServer
from kairo.memory import DatabaseLocked, Memory, lock_database
from kairo.runtime import Runtime
from kairo.situation import build_situation, render_situation

DEFAULT_DB = Path(os.environ.get("KAIRO_DB", Path.home() / ".local/share/kairo/kairo.db"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kairo")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="memory file (SQLite)")
    parser.add_argument("--run", action="store_true",
                        help="operate continuously in the foreground until interrupted")
    parser.add_argument("--situation", action="store_true",
                        help="print the situation cognition would see now, then exit")
    parser.add_argument("--reassess", type=float, default=300.0, metavar="SECONDS",
                        help="with --run: how long to sleep before waking to reassess "
                             "(0 = sleep until woken; default: %(default)s)")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET,
                        help="with --run: Unix socket for local IPC "
                             "(default: $KAIRO_SOCKET or %(default)s)")
    parser.add_argument("--cognition", default="none", metavar="ORDER",
                        help="cognition providers in the order they are asked, comma-separated "
                             "(later ones only on technical failure of earlier ones), e.g. "
                             f"claude or claude,claude@haiku; known: {', '.join(sorted(PROVIDERS))} "
                             "(default: none)")
    parser.add_argument("--provider-opt", action="append", default=[], metavar="ID.KEY=VALUE",
                        help="a provider option, e.g. claude@haiku.model=haiku (repeatable; "
                             "never credentials)")
    parser.add_argument("--implementations", default="none", metavar="IDS",
                        help="implementation packages to enable: comma-separated ids, 'all' or "
                             "'none' (default: none)")
    parser.add_argument("--implementations-dir", type=Path, metavar="PATH",
                        help="where implementation packages live "
                             "(default: <directory of --db>/implementations)")
    parser.add_argument("--model", help="shortcut: model for every claude provider "
                                        "(default: the Claude CLI's default)")
    parser.add_argument("--cognition-timeout", type=float, default=300.0, metavar="SECONDS",
                        help="shortcut: max seconds per decision for every claude provider "
                             "(default: %(default)s)")
    parser.add_argument("--repository", type=Path, metavar="PATH",
                        help="development repository Kairo's own code is committed in "
                             "(with --releases: enables runtime.deploy)")
    parser.add_argument("--releases", type=Path, metavar="PATH",
                        help="release layout (releases/, current, previous, snapshots/) the "
                             "supervisor runs Kairo from")
    parser.add_argument("--init-release", metavar="SHA",
                        help="operator bootstrap: build the release for a commit (its test suite "
                             "must pass) and select it as current if none is (needs --repository "
                             "and --releases)")
    parser.add_argument("--preflight", action="store_true",
                        help="dry cycle of this code against --db (a copy): no provider call, "
                             "no action; prints a JSON report")
    args = parser.parse_args(argv)
    if (args.repository is None) != (args.releases is None):
        parser.error("--repository and --releases go together")
    if args.init_release is not None:
        if args.repository is None:
            parser.error("--init-release needs --repository and --releases")
        return init_release(args.repository, args.releases, args.init_release)
    if args.reassess < 0:
        parser.error("--reassess must be >= 0")
    if args.cognition_timeout <= 0:
        parser.error("--cognition-timeout must be > 0")

    claude_defaults = {"timeout": str(args.cognition_timeout)}
    if args.model:
        claude_defaults["model"] = args.model
    try:
        cognition = build_cognition(args.cognition, args.provider_opt,
                                    defaults={"claude": claude_defaults}, workdir=args.db.parent)
    except ValueError as exc:
        parser.error(str(exc))
    enabled: str | set[str] = "all" if args.implementations == "all" else set()
    if args.implementations not in ("all", "none"):
        enabled = {i.strip() for i in args.implementations.split(",")}
        if bad := sorted(i for i in enabled if not ID.match(i)):
            parser.error(f"invalid implementation id(s): {bad}")
    implementations_dir = args.implementations_dir or args.db.parent / "implementations"
    deployment = None
    if args.repository is not None and not args.preflight:
        try:
            deployment = Deployment(args.repository, args.releases, args.db,
                                    preflight_args=_preflight_args(args, implementations_dir))
        except ValueError as exc:
            parser.error(str(exc))
    environment = Environment(Implementations(implementations_dir, enabled), deployment)

    if not args.situation:  # one database, one runtime
        try:
            lock_database(args.db)  # held until this process ends
        except DatabaseLocked as exc:
            print(f"kairo: {exc}", file=sys.stderr)
            return 2
    memory = Memory(args.db)
    try:
        if args.preflight:
            return _preflight(memory, environment)
        if args.situation:
            runtime = Runtime(memory, environment, cognition=cognition,
                              reassess_after=args.reassess or None)
            print(render_situation(build_situation(runtime.context())))
            return 0
        if args.run:
            return _run(memory, args.socket, args.reassess or None, cognition, environment)
        return _once(memory, args.db, cognition, environment)
    finally:
        memory.close()


def _run(memory: Memory, socket_path: Path, reassess_after: float | None,
         cognition: Cognition | None, environment: Environment) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    runtime = Runtime(memory, environment, cognition=cognition, reassess_after=reassess_after)
    server = IPCServer(runtime, socket_path)
    try:
        server.start()
    except (IPCError, OSError) as exc:
        print(f"kairo: cannot open IPC socket: {exc}", file=sys.stderr)
        return 1
    previous = {sig: signal.signal(sig, lambda *_: runtime.request_stop())
                for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        runtime.run_forever()
    finally:
        server.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return runtime.exit_code


def _once(memory: Memory, db: Path, cognition: Cognition | None,
          environment: Environment) -> int:
    runtime = Runtime(memory, environment, cognition=cognition)
    runtime.start()
    runtime.cycle()
    print(json.dumps({"memory": str(db), **runtime.status()}, indent=2))
    runtime.stop()
    return runtime.exit_code


def _preflight(memory: Memory, environment: Environment) -> int:
    cognition = PreflightCognition()
    runtime = Runtime(memory, environment, cognition=cognition)
    runtime.start()
    report = runtime.cycle()
    runtime.stop()
    result = report.cognition.get("result")
    print(json.dumps({"ok": result == "decided", "cycle": result,
                      "situation_chars": cognition.situation_chars}))
    return 0 if result == "decided" else 1


def _preflight_args(args: argparse.Namespace, implementations_dir: Path) -> list[str]:
    """This runtime's configuration, for a candidate's dry cycle: the same
    providers (built and validated, never called) and implementations."""
    out = ["--cognition", args.cognition, "--implementations", args.implementations,
           "--implementations-dir", str(Path(implementations_dir).resolve()),
           "--cognition-timeout", str(args.cognition_timeout)]
    for option in args.provider_opt:
        out += ["--provider-opt", option]
    if args.model:
        out += ["--model", args.model]
    return out


if __name__ == "__main__":
    raise SystemExit(main())
