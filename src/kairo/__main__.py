"""Command-line entry point.

``python -m kairo [--db PATH]``
    Start, run one cycle, print the resulting state, stop.

``python -m kairo --situation [--db PATH] [--socket PATH]``
    Print the situation cognition would be shown now (bounded and redacted).
    If a runtime is running on the database, the situation comes from that
    runtime over IPC (its own release, lifecycle and records); otherwise it is a
    preview built here, which claims no running release,
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
from kairo.config import CONFIG_NAME, ConfigError, default_path, load
from kairo.deploy import Deployment, PreflightCognition, init_release
from kairo.environment import Environment, parse_probe
from kairo.implementations import ID, Implementations
from kairo.registry import PROVIDERS, build_cognition
from kairo.ipc import DEFAULT_SOCKET, IPCError, IPCServer
from kairo.memory import DatabaseLocked, Memory, lock_database
from kairo.runtime import Runtime
from kairo.situation import build_situation

DEFAULT_DB = Path(os.environ.get("KAIRO_DB", Path.home() / ".local/share/kairo/kairo.db"))
DEFAULT_REASSESS = 300.0
DEFAULT_COGNITION_TIMEOUT = 300.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kairo")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="memory file (SQLite)")
    parser.add_argument("--run", action="store_true",
                        help="operate continuously in the foreground until interrupted")
    parser.add_argument("--situation", action="store_true",
                        help="print the situation cognition would see now, then exit")
    parser.add_argument("--reassess", type=float, default=None, metavar="SECONDS",
                        help="with --run: how long to sleep before waking to reassess "
                             f"(0 = sleep until woken; default: {DEFAULT_REASSESS})")
    parser.add_argument("--config", type=Path, metavar="PATH",
                        help="configuration file for the settings below "
                             f"(default: {CONFIG_NAME} beside --db, if it exists); a flag given "
                             "here wins over the file")
    parser.add_argument("--calls-per-day", type=int, default=None, metavar="N",
                        help="at most N cycles per UTC day may ask a cognition provider; beyond "
                             "it the runtime sleeps until the day turns (0 = no budget, the "
                             "default)")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET,
                        help="with --run: Unix socket for local IPC "
                             "(default: $KAIRO_SOCKET or %(default)s)")
    parser.add_argument("--cognition", default=None, metavar="ORDER",
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
    parser.add_argument("--probe", action="append", default=[], metavar="NAME=COMMAND",
                        help="a fixed command the runtime itself runs at every observation "
                             "(no shell; exit code and output become the observation "
                             "probe.NAME), e.g. web='systemctl is-active nginx'; repeatable. "
                             "With probes, a timer wake at which nothing changed and no work "
                             "is active does not consult cognition")
    parser.add_argument("--model", help="shortcut: model for every claude provider "
                                        "(default: the Claude CLI's default)")
    parser.add_argument("--cognition-timeout", type=float, default=None, metavar="SECONDS",
                        help="shortcut: max seconds per decision for every claude provider "
                             f"(default: {DEFAULT_COGNITION_TIMEOUT})")
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
    try:  # the file fills in what the flags did not say; a flag always wins
        _apply_config(args)
    except ConfigError as exc:
        parser.error(str(exc))
    if args.reassess < 0:
        parser.error("--reassess must be >= 0")
    if args.cognition_timeout <= 0:
        parser.error("--cognition-timeout must be > 0")
    if args.calls_per_day < 0:
        parser.error("--calls-per-day must be >= 0 (0: no budget)")

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
                                    preflight_args=_preflight_args(args, implementations_dir),
                                    # a preview is not a runtime: it runs no release
                                    running=os.devnull if args.situation else None)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        probes = dict(parse_probe(spec) for spec in args.probe)
        if len(probes) != len(args.probe):
            raise ValueError("probe names must be distinct")
        environment = Environment(Implementations(implementations_dir, enabled), deployment,
                                  probes=probes)
    except ValueError as exc:
        parser.error(str(exc))

    try:  # one database, one runtime
        lock = lock_database(args.db)  # held until this process ends
    except DatabaseLocked as exc:
        if args.situation:  # a live runtime owns the database: show its own situation
            return _live_situation(args.socket, args.db)
        print(f"kairo: {exc}", file=sys.stderr)
        return 2
    memory = Memory(args.db)
    try:
        if args.preflight:
            return _preflight(memory, environment)
        if args.situation:  # no runtime is running: a preview, then release the lock
            runtime = Runtime(memory, environment, cognition=cognition,
                              reassess_after=args.reassess or None,
                              calls_per_day=args.calls_per_day)
            print(json.dumps(build_situation(runtime.context()), indent=1, ensure_ascii=False))
            os.close(lock)
            return 0
        if args.run:
            return _run(memory, args.socket, args.reassess or None, cognition,
                        environment, args.calls_per_day)
        return _once(memory, args.db, cognition, environment)
    finally:
        memory.close()


def _run(memory: Memory, socket_path: Path, reassess_after: float | None,
         cognition: Cognition | None, environment: Environment,
         calls_per_day: int | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    runtime = Runtime(memory, environment, cognition=cognition, reassess_after=reassess_after,
                      calls_per_day=calls_per_day)
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


def _live_situation(socket_path: Path, db: Path) -> int:
    """The situation of the runtime that owns ``db``, computed by that runtime,
    so it describes the release and state actually running."""
    from kairo.ipc import IPCError, request
    try:
        response = request(socket_path, {"op": "situation"}, timeout=60)
    except (OSError, IPCError, ValueError) as exc:
        print(f"kairo: a running Kairo owns {db}; its situation is read from it over IPC, but "
              f"{socket_path} is not reachable ({exc}); pass --socket", file=sys.stderr)
        return 2
    if not response.get("ok"):
        print(f"kairo: the running Kairo refused: {response.get('error')}", file=sys.stderr)
        return 1
    print(json.dumps(response["result"], indent=1, ensure_ascii=False))
    return 0


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


def _apply_config(args: argparse.Namespace) -> None:
    """Resolve each setting the configuration file can supply, in order:
    the command line, then the file, then Kairo's default. Mutates ``args`` so
    everything downstream — including the dry cycle's argv — sees one effective
    configuration and never reads the file again."""
    path = args.config or default_path(args.db)
    config = load(path, required=args.config is not None)
    for flag, configured, fallback in (
        ("cognition", config.providers, "none"),
        ("model", config.model, None),
        ("cognition_timeout", config.timeout, DEFAULT_COGNITION_TIMEOUT),
        ("reassess", config.reassess, DEFAULT_REASSESS),
        ("calls_per_day", config.calls_per_day, 0),
    ):
        if getattr(args, flag) is None:
            setattr(args, flag, configured if configured is not None else fallback)
    # Options the command line already gives for the same instance.key win; the
    # rest of the file's options are added, because giving both is an error.
    given = {item.split("=", 1)[0] for item in args.provider_opt}
    args.provider_opt = [item for item in config.provider_opts()
                         if item.split("=", 1)[0] not in given] + args.provider_opt


def _preflight_args(args: argparse.Namespace, implementations_dir: Path) -> list[str]:
    """This runtime's configuration, for a candidate's dry cycle: the same
    providers (built and validated, never called) and implementations.

    Only settings that differ from the default are passed. The candidate may be
    an older release than this one — a rollback is a deployment like any other —
    and a flag it has never heard of would fail its dry cycle for no reason."""
    out = ["--cognition", args.cognition, "--implementations", args.implementations,
           "--implementations-dir", str(Path(implementations_dir).resolve()),
           "--cognition-timeout", str(args.cognition_timeout)]
    for option in args.provider_opt:
        out += ["--provider-opt", option]
    for probe in args.probe:
        out += ["--probe", probe]
    if args.model:
        out += ["--model", args.model]
    # Not the call budget: a dry cycle calls no provider, so it spends nothing,
    # and a spent budget must never be the reason a release cannot be checked.
    return out


if __name__ == "__main__":
    raise SystemExit(main())
