"""Directives: persistent reasons for which Kairo keeps operating.

A directive is an ongoing area of responsibility ("Continuously maintain and
improve the 1C environment"), not a task that gets completed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from kairo.memory import Collection, Memory


@dataclass(frozen=True)
class Directive:
    statement: str
    active: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class Directives(Collection[Directive]):
    def __init__(self, memory: Memory) -> None:
        super().__init__(memory, "directive", Directive)

    def add(self, statement: str) -> Directive:
        return self.save(Directive(statement))

    def set_active(self, id: str, active: bool) -> Directive:
        directive = self.get(id)
        if directive is None:
            raise KeyError(id)
        return self.save(replace(directive, active=active))

    def active(self) -> list[Directive]:
        return [d for d in self.all() if d.active]
