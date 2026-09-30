"""Command-line entry point.

``python -m kairo [--db PATH]``
    Start, run one cycle, print the resulting state, stop.

``python -m kairo --run [--db PATH] [--socket PATH] [--reassess SECONDS] [--cognition claude]``
    Operate continuously in the foreground until SIGINT (Ctrl-C), SIGTERM or
    an IPC stop request. Other local processes reach it through the Unix
    socket (see ``kairo.ipc``). Without ``--cognition`` Kairo has no cognition
    and simply sleeps between wakes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path

from kairo.claude import ClaudeCognition
from kairo.cognition import CognitionProvider
from kairo.ipc import DEFAULT_SOCKET, IPCError, IPCServer
from kairo.memory import Memory
from kairo.runtime import Runtime

DEFAULT_DB = Path(os.environ.get("KAIRO_DB", Path.home() / ".local/share/kairo/kairo.db"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kairo")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="memory file (SQLite)")
    parser.add_argument("--run", action="store_true",
                        help="operate continuously in the foreground until interrupted")
    parser.add_argument("--reassess", type=float, default=300.0, metavar="SECONDS",
                        help="with --run: how long to sleep before waking to reassess "
                             "(0 = sleep until woken; default: %(default)s)")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET,
                        help="with --run: Unix socket for local IPC "
                             "(default: $KAIRO_SOCKET or %(default)s)")
    parser.add_argument("--cognition", choices=["none", "claude"], default="none",
                        help="cognition provider (default: none)")
    parser.add_argument("--model", help="with --cognition claude: model alias or name "
                                        "(default: the Claude CLI's default)")
    parser.add_argument("--cognition-timeout", type=float, default=300.0, metavar="SECONDS",
                        help="with --cognition claude: max seconds per decision "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)
    if args.reassess < 0:
        parser.error("--reassess must be >= 0")
    if args.cognition_timeout <= 0:
        parser.error("--cognition-timeout must be > 0")

    cognition: CognitionProvider | None = None
    if args.cognition == "claude":
        cognition = ClaudeCognition(model=args.model, timeout=args.cognition_timeout,
                                    workdir=args.db.parent)

    memory = Memory(args.db)
    try:
        if args.run:
            return _run(memory, args.socket, args.reassess or None, cognition)
        return _once(memory, args.db, cognition)
    finally:
        memory.close()


def _run(memory: Memory, socket_path: Path, reassess_after: float | None,
         cognition: CognitionProvider | None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    runtime = Runtime(memory, cognition=cognition, reassess_after=reassess_after)
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
    return 0


def _once(memory: Memory, db: Path, cognition: CognitionProvider | None) -> int:
    runtime = Runtime(memory, cognition=cognition)
    runtime.start()
    runtime.cycle()
    print(json.dumps({"memory": str(db), **runtime.status()}, indent=2))
    runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
