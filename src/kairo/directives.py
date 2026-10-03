"""Directives: persistent reasons for which Kairo keeps operating.

A directive is an ongoing area of responsibility ("Continuously maintain and
improve the 1C environment"), not a task that gets completed, a schedule or a
command. The operator sets directives (over IPC, through the runtime); creating
one executes nothing: it changes what cognition is shown as Kairo's purpose.
A directive is never edited or deleted: it is deactivated (and can be activated
again), so work linked to it keeps meaning what it meant.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from kairo.memory import Collection, Memory


@dataclass(frozen=True)
class Directive:
    statement: str
    active: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # None for records written before timestamps existed: unknown, not invented.
    created_at: float | None = None
    # Who set it ("operator"); None for records written before this was recorded.
    origin: str | None = None
    # Its changes: {"at", "event": created|activated|deactivated, "by"}, newest last.
    history: list[dict[str, Any]] = field(default_factory=list)


HISTORY = 20  # changes remembered per directive


class Directives(Collection[Directive]):
    def __init__(self, memory: Memory) -> None:
        super().__init__(memory, "directive", Directive)

    def add(self, statement: str, origin: str | None = None) -> Directive:
        now = time.time()
        return self.save(Directive(statement, created_at=now, origin=origin,
                                   history=[{"at": now, "event": "created", "by": origin}]))

    def set_active(self, id: str, active: bool, by: str | None = None) -> Directive:
        directive = self.get(id)
        if directive is None:
            raise KeyError(id)
        event = {"at": time.time(), "event": "activated" if active else "deactivated", "by": by}
        return self.save(replace(directive, active=active,
                                 history=(directive.history + [event])[-HISTORY:]))

    def active(self) -> list[Directive]:
        return [d for d in self.all() if d.active]
