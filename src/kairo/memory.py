"""Persistent runtime memory.

Memory is a small local SQLite document store: every record is a JSON
document identified by ``(kind, id)``. It is deliberately schemaless so the
kinds of things Kairo remembers can evolve without migrations.

Records are read tolerantly: a field this code does not know (written by a newer
release) is ignored, so rolling back to an older release never hides a whole
record. Known fields are still type-checked; a known field of the wrong type is
corruption. An older release that rewrites such a record drops the fields it
does not know.
"""

from __future__ import annotations

import dataclasses
import enum
import fcntl
import functools
import json
import os
import sqlite3
import threading
import types
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL,
    id   TEXT NOT NULL,
    seq  INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS records_seq ON records (seq);
CREATE INDEX IF NOT EXISTS records_kind_seq ON records (kind, seq);
"""


class Memory:
    """Persistent key/document storage backed by a single SQLite file.

    Safe to share between threads: every operation is serialised by a lock.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def put(self, kind: str, id: str, data: dict[str, Any]) -> None:
        # seq preserves first-insertion order across updates.
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO records (kind, id, seq, data)
                VALUES (?, ?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM records), ?)
                ON CONFLICT (kind, id) DO UPDATE SET data = excluded.data
                """,
                (kind, id, json.dumps(data)),
            )

    def get(self, kind: str, id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM records WHERE kind = ? AND id = ?", (kind, id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM records WHERE kind = ? ORDER BY seq", (kind,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def recent(self, kind: str, limit: int) -> list[dict[str, Any]]:
        """The last ``limit`` records of a kind, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM records WHERE kind = ? ORDER BY seq DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def recent_where(self, kind: str, field: str, value: str, limit: int) -> list[dict[str, Any]]:
        """The last ``limit`` records of a kind whose top-level ``field`` equals
        ``value``, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM records WHERE kind = ? AND json_extract(data, ?) = ? "
                "ORDER BY seq DESC LIMIT ?",
                (kind, f"$.{field}", value, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def page(self, kind: str, limit: int, after: int | None = None
             ) -> tuple[list[tuple[int, dict[str, Any]]], bool, bool]:
        """One page of a kind in first-insertion order, each record with its
        ``seq``: those after ``after`` (oldest first), or without it the last
        ``limit``. Also returns whether records exist before and after the page."""
        with self._lock:
            if after is None:
                rows = self._db.execute(
                    "SELECT seq, data FROM records WHERE kind = ? ORDER BY seq DESC LIMIT ?",
                    (kind, limit)).fetchall()[::-1]
            else:
                rows = self._db.execute(
                    "SELECT seq, data FROM records WHERE kind = ? AND seq > ? ORDER BY seq LIMIT ?",
                    (kind, after, limit)).fetchall()
            low = rows[0][0] if rows else (after if after is not None else 0) + 1
            high = rows[-1][0] if rows else (after if after is not None else 0)
            before, beyond = (bool(self._db.execute(
                f"SELECT 1 FROM records WHERE kind = ? AND seq {op} ? LIMIT 1",
                (kind, bound)).fetchone()) for op, bound in (("<", low), (">", high)))
        return [(seq, json.loads(data)) for seq, data in rows], before, beyond

    def stream(self, kinds: tuple[str, ...], limit: int, before: int | None = None
               ) -> tuple[list[tuple[int, str, dict[str, Any]]], bool]:
        """The last ``limit`` records of several kinds together, newest first, each
        with its ``seq`` and kind: those before ``before`` if given. ``seq`` is one
        counter across all kinds, so this is the order in which the records were
        first written. Also returns whether older ones exist."""
        marks = ", ".join("?" for _ in kinds)
        bound = "AND seq < ?" if before is not None else ""
        args = (*kinds, *((before,) if before is not None else ()), limit + 1)
        with self._lock:
            rows = self._db.execute(
                f"SELECT seq, kind, data FROM records WHERE kind IN ({marks}) {bound} "
                "ORDER BY seq DESC LIMIT ?", args).fetchall()
        return [(seq, kind, json.loads(data)) for seq, kind, data in rows[:limit]], \
            len(rows) > limit

    def fields(self, kind: str, paths: tuple[str, ...], limit: int) -> list[tuple[Any, ...]]:
        """The values at ``paths`` (dotted) in the last ``limit`` records of a kind,
        newest first, without loading whole documents: for totals over many
        records. A missing value is None; an object or list comes back as JSON text."""
        columns = ", ".join("json_extract(data, ?)" for _ in paths)
        with self._lock:
            return self._db.execute(
                f"SELECT {columns} FROM records WHERE kind = ? ORDER BY seq DESC LIMIT ?",
                (*(f"$.{path}" for path in paths), kind, limit)).fetchall()

    def count(self, kind: str) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM records WHERE kind = ?", (kind,)
            ).fetchone()[0]

    def delete(self, kind: str, id: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM records WHERE kind = ? AND id = ?", (kind, id)
            )
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._db.close()


class DatabaseLocked(RuntimeError):
    """Another live Kairo runtime already owns this database."""


def lock_database(path: str | Path) -> int:
    """Take the exclusive runtime lock for one database: one DB, one active
    runtime. Held (by the returned descriptor) for the rest of the process's life
    and released by the OS when the process ends, however it ends. Per database,
    not system-wide: other databases (tests, preflight copies) are unaffected."""
    lock_path = f"{path}.lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise DatabaseLocked(f"another Kairo runtime is using {path}") from None
    return fd


def backup_database(source: str | Path, target: str | Path) -> None:
    """A consistent copy of a live database (SQLite backup API), readable while
    the owning runtime keeps writing. The copy is mode 0600."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def from_record[T](type_: type[T], data: Any, lenient: frozenset[str] = frozenset()) -> T:
    """Build a record dataclass from stored JSON: unknown fields are ignored,
    known fields must have their declared type (TypeError/ValueError if not),
    except ``lenient`` ones, whose readers already handle bad values themselves
    (showing them as unknown) and must keep doing so."""
    if not isinstance(data, dict):
        raise TypeError(f"{type_.__name__} record is not an object")
    hints = _hints(type_)
    values = {k: v for k, v in data.items() if k in hints}
    for name, value in values.items():
        if name not in lenient and not _conforms(value, hints.get(name, Any)):
            raise ValueError(f"{type_.__name__}.{name} has the wrong type "
                             f"({type(value).__name__})")
    return type_(**values)


@functools.cache
def _hints(type_: type) -> dict[str, Any]:
    """Declared field types of a record dataclass (computed once per type)."""
    hints = typing.get_type_hints(type_)
    return {f.name: hints.get(f.name, Any) for f in dataclasses.fields(type_)}


def _conforms(value: Any, hint: Any) -> bool:
    if hint is Any:
        return True
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        return any(_conforms(value, h) for h in typing.get_args(hint))
    if hint is type(None):
        return value is None
    if origin is not None:
        return isinstance(value, origin)
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return value in {m.value for m in hint}
    if hint is bool:
        return isinstance(value, bool)
    if hint in (int, float):  # JSON numbers; a bool is not a number here
        return isinstance(value, (int, float)) and not isinstance(value, bool) and \
            (hint is float or isinstance(value, int))
    if isinstance(hint, type):
        return isinstance(value, hint)
    return True


class Collection[T]:
    """Typed view over one ``kind`` of record, for dataclasses with an ``id``."""

    def __init__(self, memory: Memory, kind: str, type_: type[T]) -> None:
        self._memory = memory
        self._kind = kind
        self._type = type_

    def save(self, item: T) -> T:
        self._memory.put(self._kind, item.id, dataclasses.asdict(item))  # type: ignore[attr-defined]
        return item

    def get(self, id: str) -> T | None:
        data = self._memory.get(self._kind, id)
        return from_record(self._type, data) if data is not None else None

    def all(self) -> list[T]:
        return [from_record(self._type, d) for d in self._memory.all(self._kind)]

    def count(self) -> int:
        return self._memory.count(self._kind)

    def recent(self, limit: int) -> list[T]:
        return [from_record(self._type, d) for d in self._memory.recent(self._kind, limit)]

    def remove(self, id: str) -> bool:
        return self._memory.delete(self._kind, id)

    def __iter__(self) -> Iterator[T]:
        return iter(self.all())
