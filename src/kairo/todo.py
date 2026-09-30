"""To-do: concrete intermediate work Kairo has noted for itself.

The to-do list is operational bookkeeping, not the source of autonomy. The
runtime never treats an empty list as "nothing to do"; deciding what matters
is cognition's job.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace

from kairo.memory import Collection, Memory


@dataclass(frozen=True)
class TodoItem:
    description: str
    done: bool = False
    directive_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # None for records written before timestamps existed: unknown, not invented.
    created_at: float | None = None
    done_at: float | None = None


class Todo(Collection[TodoItem]):
    def __init__(self, memory: Memory) -> None:
        super().__init__(memory, "todo", TodoItem)

    def add(self, description: str, directive_id: str | None = None) -> TodoItem:
        return self.save(TodoItem(description, directive_id=directive_id, created_at=time.time()))

    def complete(self, id: str) -> TodoItem:
        item = self.get(id)
        if item is None:
            raise KeyError(id)
        return self.save(replace(item, done=True, done_at=time.time()))

    def open(self) -> list[TodoItem]:
        return [i for i in self.all() if not i.done]

    def done(self) -> list[TodoItem]:
        return [i for i in self.all() if i.done]
