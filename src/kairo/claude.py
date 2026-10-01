"""Claude as Kairo's cognition, through the locally installed Claude Code CLI.

Each ``decide`` runs one headless, tool-less ``claude -p`` call: the rendered
situation (see ``kairo.situation``) goes in on stdin, and the CLI's schema-validated structured
output comes back and is strictly parsed into a Decision. Claude has no tools
of its own here; everything it wants done must come back as a structured
action for the runtime to execute. Authentication is whatever the local CLI
is already logged in with; Kairo never sees or stores a credential.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from kairo.cognition import CognitionError, Context, Decision, decision_schema, parse_decision
from kairo.situation import build_situation, render_situation

DEFAULT_TIMEOUT = 300.0

SYSTEM_PROMPT = """\
You are the cognition of Kairo, a persistent autonomous runtime on a Linux host. \
You are not answering a chat request, and you are not Kairo itself: Kairo is the \
runtime, which continues across cycles, sleeps and wakes, and survives restarts. \
You are called once per cycle with Kairo's current situation and decide what Kairo \
does next.

The situation (JSON) has these sections: kairo (identity), now (lifecycle state, \
why Kairo is awake, previous cycle and process), environment (a fresh host \
observation and what changed since the last one), directives (the lasting areas \
Kairo is responsible for), work (ongoing pursuits carried across cycles, with their \
attempts), todo (operational notes), history (recent cycles, \
actions with results and verification, and chat), open_threads (loose ends the \
runtime sees in its records; informational, not a task list), knowledge, capabilities (the only actions the runtime can \
execute) and context (bounds and what was omitted). Each part says where it comes \
from and how old it is.

Treat runtime records and observations as facts, weighted by their age. Treat \
earlier assessments and action purposes as your own past interpretations, not \
facts: check them against the records. An action's state says whether it actually \
worked; "executed_unverified" means it ran but nobody checked the outcome. \
Missing or unknown information is really missing; never fill it in.

Each cycle:
1. Understand the situation, and whether this continues earlier work or is new.
2. Decide what matters now, given the directives, the environment, open threads \
and recent results: a problem, a risk, an opportunity, or an unanswered message.
3. If something needs doing, request concrete actions from capabilities. The \
runtime executes them after you answer; you will see their results next cycle, so \
never claim an outcome you have not seen.
4. If you need those results, set sleep=false for another cycle right away. \
Otherwise set sleep=true, with wake_after (seconds) if something should be \
rechecked at a particular time; null uses the runtime default.
5. Reply (replies) only when it helps the human: to answer them, or to report \
something they should know. Be concise and do not repeat earlier replies.

Ongoing work (see capabilities.work_requests for the exact requests):
- When something deserves pursuit across cycles, create work for it instead of \
keeping it only in your reason. First check the open and recently closed work so you \
do not duplicate or resurrect it. Link every action to the work it is an attempt at.
- Keep each work item's understanding, strategy and next_step current, so the next \
cycle can continue it.
- After a failed attempt, understand why before acting again, and record that \
diagnosis in the work's understanding. A failure kind or exit code is a runtime \
fact; what it means is your judgment (an exit code is only a number). The runtime \
refuses an exact repeat of a failed or interrupted attempt unless the work's \
understanding has changed since it. Repeating the same approach is a retry, and needs \
a reason; if the approach itself was wrong, change the strategy (update it) and try \
something materially different. The work's recovery section shows each strategy \
revision's results and any identical failures in a row.
- An interrupted attempt has an unknown outcome: it may have run partly or fully. \
Check the world before repeating it.
- Use waiting when progress depends on something external or on time, for example a \
failure that looks temporary; give wait_seconds and the runtime wakes Kairo when it \
runs out. Use blocked only for a concrete obstacle you cannot get past now. \
Actionable work stays active.
- Complete work only when its outcome is actually achieved, citing as evidence \
this work's attempts whose results show it. Evidence must be verified successful, or, \
where the runtime has no verifier, an attempt that exited 0; a non-zero exit without \
verification can never support completion. The runtime records the completion as \
"verified" or "unverified". An unverified completion is only your own judgment of the \
results, not verification: never treat it as proof. Abandon work, with a reason, when \
it is deliberately no longer worth pursuing. Closed work is history; a new reason \
means new work.

Principles: An empty todo list does not mean nothing matters, and todo is not your \
purpose; directives and the state of the world are. Do not invent busywork: when \
nothing is genuinely worth doing, sleep. Stay within capabilities. Never output, \
copy or seek out secrets or credentials. Prefer actions that are safe, observable \
and reversible, and give each action's purpose in its reason. Put your assessment \
in reason: what you understood, what you intend and why. The next cycle will read it.

Answer only with the JSON object required by the output schema."""


class ClaudeCognition:
    """Cognition provider backed by the local ``claude`` CLI."""

    name = "claude"

    def __init__(self, executable: str = "claude", model: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, workdir: str | Path | None = None) -> None:
        self.executable = executable
        self.model = model
        self.timeout = timeout
        self.workdir = str(workdir) if workdir is not None else None

    def command(self, context: Context) -> list[str]:
        argv = [
            self.executable, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(decision_schema(context.available_actions)),
            "--system-prompt", SYSTEM_PROMPT,
            # No tools, settings, hooks, MCP servers or skills: Claude may only
            # answer. Execution stays with the runtime's action interface.
            "--tools", "",
            "--restricted",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def decide(self, context: Context) -> Decision:
        situation = render_situation(build_situation(context))
        prompt = "Kairo situation:\n" + situation
        started = time.monotonic()
        try:
            proc = subprocess.run(
                self.command(context), input=prompt, capture_output=True, text=True,
                errors="replace", timeout=self.timeout, cwd=self.workdir,
            )
        except FileNotFoundError as exc:
            raise CognitionError("unavailable", f"claude executable not found: {exc}") from None
        except PermissionError as exc:
            raise CognitionError("unavailable", f"claude executable not runnable: {exc}") from None
        except subprocess.TimeoutExpired:
            raise CognitionError("timeout", f"claude did not answer within {self.timeout:g}s") from None
        except OSError as exc:
            raise CognitionError("process_failed", f"could not run claude: {exc}") from None
        seconds = round(time.monotonic() - started, 3)

        if proc.returncode != 0:
            detail = _tail(proc.stderr) or _tail(proc.stdout) or "no output"
            raise CognitionError("process_failed", f"claude exited {proc.returncode}: {detail}")
        envelope = _envelope(proc.stdout)
        decision = parse_decision(_answer(envelope), context.available_actions)
        return dataclasses.replace(decision, meta={
            "seconds": seconds,
            "situation_chars": len(situation),
            "api_seconds": _round(envelope.get("duration_api_ms"), 1000),
            "turns": envelope.get("num_turns"),
            "cost_usd": envelope.get("total_cost_usd"),
            "models": sorted(envelope.get("modelUsage") or {}),
        })


def _envelope(stdout: str) -> dict[str, Any]:
    if not stdout.strip():
        raise CognitionError("empty_output", "claude produced no output")
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CognitionError("invalid_output", f"claude output is not JSON: {exc}") from None
    if not isinstance(envelope, dict) or envelope.get("type") != "result":
        raise CognitionError("invalid_output", "claude output is not a result object")
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        detail = envelope.get("api_error_status") or envelope.get("subtype") or "unknown error"
        raise CognitionError("model_error", f"claude reported an error: {_tail(str(detail))}")
    return envelope


def _answer(envelope: dict[str, Any]) -> Any:
    """The decision JSON: the schema-validated structured output if present,
    otherwise the result text, which must itself be exactly a JSON object."""
    if isinstance(envelope.get("structured_output"), dict):
        return envelope["structured_output"]
    text = envelope.get("result")
    if not isinstance(text, str) or not text.strip():
        raise CognitionError("empty_output", "claude returned no decision")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CognitionError("invalid_decision", f"decision is not valid JSON: {exc}") from None


def _tail(text: str, limit: int = 300) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _round(ms: Any, divisor: int) -> float | None:
    return round(ms / divisor, 3) if isinstance(ms, (int, float)) else None
