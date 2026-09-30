"""To-do: concrete intermediate work Kairo has noted for itself.

The to-do list is operational bookkeeping, not the source of autonomy. The
runtime never treats an empty list as "nothing to do"; deciding what matters
is cognition's job.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from kairo.memory import Collection, Memory


@dataclass(frozen=True)
class TodoItem:
    description: str
    done: bool = False
    directive_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class Todo(Collection[TodoItem]):
    def __init__(self, memory: Memory) -> None:
        super().__init__(memory, "todo", TodoItem)

    def add(self, description: str, directive_id: str | None = None) -> TodoItem:
        return self.save(TodoItem(description, directive_id=directive_id))

    def complete(self, id: str) -> TodoItem:
        item = self.get(id)
        if item is None:
            raise KeyError(id)
        return self.save(replace(item, done=True))

    def open(self) -> list[TodoItem]:
        return [i for i in self.all() if not i.done]
