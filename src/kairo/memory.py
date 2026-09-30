"""Persistent runtime memory.

Memory is a small local SQLite document store: every record is a JSON
document identified by ``(kind, id)``. It is deliberately schemaless so the
kinds of things Kairo remembers can evolve without migrations.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
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
        return self._type(**data) if data is not None else None

    def all(self) -> list[T]:
        return [self._type(**d) for d in self._memory.all(self._kind)]

    def count(self) -> int:
        return self._memory.count(self._kind)

    def recent(self, limit: int) -> list[T]:
        return [self._type(**d) for d in self._memory.recent(self._kind, limit)]

    def remove(self, id: str) -> bool:
        return self._memory.delete(self._kind, id)

    def __iter__(self) -> Iterator[T]:
        return iter(self.all())
