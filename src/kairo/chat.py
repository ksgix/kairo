"""Chat: the human interaction surface into the running runtime.

Messages are persisted so a conversation with Kairo survives restarts.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from kairo.memory import Collection, Memory


class Sender(StrEnum):
    HUMAN = "human"
    KAIRO = "kairo"


@dataclass(frozen=True)
class Message:
    sender: Sender
    text: str
    at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sender", Sender(self.sender))


class Chat(Collection[Message]):
    def __init__(self, memory: Memory) -> None:
        super().__init__(memory, "message", Message)

    def post(self, sender: Sender, text: str, id: str | None = None) -> Message:
        return self.save(Message(sender, text, id=id) if id else Message(sender, text))
