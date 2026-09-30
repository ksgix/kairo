"""Live smoke test against the real local Claude CLI. Opt-in; uses the model.

    KAIRO_LIVE_CLAUDE=1 [KAIRO_LIVE_MODEL=haiku] \
        PYTHONPATH=src python3 -m unittest discover -s tests -p test_claude_live.py -v
"""

import os
import shutil
import unittest

from kairo import Decision, Memory, Runtime
from kairo.claude import ClaudeCognition

LIVE = os.environ.get("KAIRO_LIVE_CLAUDE") == "1"


@unittest.skipUnless(LIVE, "set KAIRO_LIVE_CLAUDE=1 to run against the real Claude CLI")
class LiveClaudeSmokeTest(unittest.TestCase):
    def test_one_real_decision(self):
        self.assertIsNotNone(shutil.which("claude"), "claude CLI not on PATH")
        memory = Memory()
        self.addCleanup(memory.close)
        cognition = ClaudeCognition(model=os.environ.get("KAIRO_LIVE_MODEL", "haiku"),
                                    timeout=180)
        runtime = Runtime(memory, cognition=cognition)
        runtime.receive("Smoke test: please reply with one short sentence, then sleep.")
        runtime.start()

        decision = cognition.decide(runtime.context())

        self.assertIsInstance(decision, Decision)
        self.assertTrue(decision.replies, "expected a reply to the human message")
        for action in decision.actions:
            self.assertIn(action.kind, runtime.environment.actions())
        print(f"\nlive decision: sleep={decision.sleep} actions={len(decision.actions)} "
              f"replies={decision.replies!r} reason={decision.reason!r} meta={decision.meta}")


if __name__ == "__main__":
    unittest.main()
