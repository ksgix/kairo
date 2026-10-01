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

from kairo.cognition import CognitionError, Context, Decision, parse_decision
from kairo.instructions import INSTRUCTIONS, cognition_request
from kairo.redact import scrubbed_env

# Kept under its old name: the instructions are Kairo's, not Claude's.
SYSTEM_PROMPT = INSTRUCTIONS

DEFAULT_TIMEOUT = 300.0

# Where the Claude CLI's credentials can live. Kairo never reads them for use;
# it only keeps them away from other processes and out of its records.
SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
SECRET_FILES = ("~/.claude/.credentials.json",)
OPTIONS = {"model", "timeout", "executable"}


class ClaudeCognition:
    """Cognition provider backed by the local ``claude`` CLI."""

    secret_env = SECRET_ENV
    secret_files = SECRET_FILES

    def __init__(self, executable: str = "claude", model: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, workdir: str | Path | None = None,
                 name: str = "claude") -> None:
        self.name = name  # this instance's id (e.g. "claude", or "claude@haiku")
        self.executable = executable
        self.model = model
        self.timeout = timeout
        self.workdir = str(workdir) if workdir is not None else None

    @classmethod
    def from_options(cls, name: str, options: dict[str, str],
                     workdir: str | Path | None = None) -> "ClaudeCognition":
        """Build from string options (CLI). Credentials are never options: the
        CLI authenticates itself (its own login or its environment variables)."""
        unknown = set(options) - OPTIONS
        if unknown:
            raise ValueError(f"{name}: unknown option(s) {sorted(unknown)}; "
                             f"allowed: {sorted(OPTIONS)}")
        try:
            timeout = float(options.get("timeout", DEFAULT_TIMEOUT))
        except ValueError:
            raise ValueError(f"{name}: timeout must be a number of seconds") from None
        if timeout <= 0:
            raise ValueError(f"{name}: timeout must be > 0")
        return cls(executable=options.get("executable", "claude"), model=options.get("model"),
                   timeout=timeout, workdir=workdir, name=name)

    def command(self, context: Context) -> list[str]:
        return self._command(cognition_request(context))

    def _command(self, request: Any) -> list[str]:
        argv = [
            self.executable, "-p",
            "--output-format", "json",
            "--json-schema", request.schema_json,
            "--system-prompt", request.instructions,
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
        request = cognition_request(context)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                self._command(request), input=request.prompt, capture_output=True, text=True,
                errors="replace", timeout=self.timeout, cwd=self.workdir,
                # Only Claude's own credentials, never another provider's.
                env=scrubbed_env(keep=self.secret_env),
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
            _raise_reported_error(proc.stdout)  # Claude's own error report, if it gave one
            detail = _tail(proc.stderr) or _tail(proc.stdout) or "no output"
            raise CognitionError("process_failed", f"claude exited {proc.returncode}: {detail}")
        envelope = _envelope(proc.stdout)
        decision = parse_decision(_answer(envelope), context.available_actions)
        return dataclasses.replace(decision, meta={
            "seconds": seconds,
            "situation_chars": len(request.prompt),
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
        raise _reported_error(envelope)
    return envelope


def _raise_reported_error(stdout: str) -> None:
    """If the CLI exited non-zero but printed a result envelope reporting an
    error, raise that classified error instead of a bare process failure."""
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return
    if isinstance(envelope, dict) and envelope.get("type") == "result" and (
            envelope.get("is_error") or envelope.get("subtype") != "success"):
        raise _reported_error(envelope)


def _reported_error(envelope: dict[str, Any]) -> CognitionError:
    """Map Claude's reported error to Kairo's outcome vocabulary. The API status
    decides when present; a model that ran and did not answer is model_error."""
    status = envelope.get("api_error_status")
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    text = str(envelope.get("result") or "")
    detail = f"claude reported an error: {_tail(str(status or envelope.get('subtype') or 'unknown error'))}"
    if status in (401, 403) or "/login" in text or "invalid api key" in text.lower():
        return CognitionError("auth_failed", detail)
    if status in (429, 529):
        return CognitionError("rate_limited", detail)
    if status is not None and 500 <= status < 600:
        return CognitionError("unavailable", detail)
    return CognitionError("model_error", detail)


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
