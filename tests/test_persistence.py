import tempfile
import unittest
from pathlib import Path

from kairo import Chat, Directives, Memory, Message, Sender


class PersistenceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state" / "kairo.db"

    def tearDown(self):
        self._tmp.cleanup()

    def reopen(self, memory: Memory) -> Memory:
        memory.close()
        reopened = Memory(self.path)
        self.addCleanup(reopened.close)
        return reopened


class MemoryTest(PersistenceCase):
    def test_put_get_survives_reopen(self):
        memory = Memory(self.path)
        memory.put("note", "a", {"text": "hello", "n": 1})
        memory = self.reopen(memory)
        self.assertEqual(memory.get("note", "a"), {"text": "hello", "n": 1})

    def test_update_keeps_order_and_delete(self):
        memory = Memory(self.path)
        memory.put("k", "1", {"v": 1})
        memory.put("k", "2", {"v": 2})
        memory.put("k", "1", {"v": 10})
        self.assertEqual(memory.all("k"), [{"v": 10}, {"v": 2}])
        self.assertTrue(memory.delete("k", "1"))
        self.assertFalse(memory.delete("k", "1"))
        self.assertIsNone(memory.get("k", "1"))
        memory.close()

    def test_kinds_are_separate(self):
        memory = Memory()
        memory.put("a", "x", {"v": 1})
        self.assertIsNone(memory.get("b", "x"))
        self.assertEqual(memory.all("b"), [])
        memory.close()


class DirectivesTest(PersistenceCase):
    def test_create_read_update_remove_persist(self):
        memory = Memory(self.path)
        d = Directives(memory).add("Continuously maintain and improve the 1C environment.")
        other = Directives(memory).add("Keep the host healthy.")

        memory = self.reopen(memory)
        directives = Directives(memory)
        self.assertEqual(directives.get(d.id), d)
        self.assertEqual([x.id for x in directives.all()], [d.id, other.id])

        directives.set_active(d.id, False)
        self.assertEqual([x.id for x in directives.active()], [other.id])

        self.assertTrue(directives.remove(other.id))
        memory = self.reopen(memory)
        directives = Directives(memory)
        self.assertEqual(len(directives.all()), 1)
        self.assertFalse(directives.get(d.id).active)

    def test_set_active_unknown_raises(self):
        with self.assertRaises(KeyError):
            Directives(Memory()).set_active("missing", True)


class ChatTest(PersistenceCase):
    def test_messages_are_structured_and_persist(self):
        memory = Memory(self.path)
        chat = Chat(memory)
        m = chat.post(Sender.HUMAN, "How is the server?")
        chat.post(Sender.KAIRO, "Looking.")
        self.assertIsInstance(m, Message)
        self.assertIs(m.sender, Sender.HUMAN)

        memory = self.reopen(memory)
        history = Chat(memory).all()
        self.assertEqual([(x.sender, x.text) for x in history],
                         [(Sender.HUMAN, "How is the server?"), (Sender.KAIRO, "Looking.")])
        self.assertIs(history[0].sender, Sender.HUMAN)
        self.assertEqual(history[0], m)

    def test_recent_limits(self):
        chat = Chat(Memory())
        for i in range(5):
            chat.post(Sender.HUMAN, str(i))
        self.assertEqual([m.text for m in chat.recent(2)], ["3", "4"])


if __name__ == "__main__":
    unittest.main()
