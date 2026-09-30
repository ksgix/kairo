"""Claude as Kairo's cognition, through the locally installed Claude Code CLI.

Each ``decide`` runs one headless, tool-less ``claude -p`` call: the rendered
runtime context goes in on stdin, and the CLI's schema-validated structured
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

from kairo.cognition import (
    CognitionError, Context, Decision, decision_schema, parse_decision, render_context,
)

DEFAULT_TIMEOUT = 300.0

SYSTEM_PROMPT = """\
You are the cognition of Kairo, a persistent autonomous runtime on a Linux host. \
You are not answering a chat request. Kairo keeps running across cycles, sleeps \
and wakes, and remembers across restarts; you are called once per cycle with its \
current context and decide what Kairo does next.

The context (JSON) contains: runtime (identity, lifecycle state, wake_reason, time), \
environment (a fresh observation of the host), directives (the lasting reasons Kairo \
operates), todo (operational notes), chat (recent messages between the human and \
Kairo), recent_actions (what was already executed, with results and verification) \
and available_actions (the only actions the runtime can execute, with parameter schemas).

Each cycle:
1. Read the context. Treat it as the only source of facts; never invent observations.
2. Consider the directives, the environment and recent action results, and decide \
whether anything meaningful needs attention now: a problem, a risk, an opportunity, \
or an unanswered human message.
3. If so, request concrete actions from available_actions. The runtime executes them \
after you answer; you see results in recent_actions on the next cycle. Nothing you \
request has happened yet, so never claim an outcome before you have seen its result.
4. Verification "unverifiable" means no automatic check exists: judge the outcome \
yourself from the recorded result, or observe again with a follow-up action.
5. If actions are in flight or you need their results, set sleep=false to get another \
cycle right away. Otherwise set sleep=true, and use wake_after (seconds) if something \
should be rechecked at a particular time; null uses the runtime default.
6. Reply (replies) only when it helps the human: to answer them, or to report \
something they should know. Be concise. Do not repeat earlier replies.

Principles: An empty todo list does not mean nothing matters, and todo is not your \
purpose; directives and the state of the world are. Equally, do not invent busywork: \
when nothing is genuinely worth doing, sleep. Stay within available_actions. Never \
output, copy or seek out secrets or credentials. Prefer actions that are safe, \
observable and reversible, and explain each action's purpose in its reason. \
Put a brief account of your assessment in reason.

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
        prompt = "Kairo cycle context:\n" + json.dumps(render_context(context), indent=1)
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
