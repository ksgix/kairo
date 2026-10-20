"""Kairo's configuration file: the settings an operator changes, in one place.

A small TOML file beside the database. Everything in it has a command-line
equivalent, and a flag given on the command line wins, so the file is a
convenience and never a second source of truth the flags disagree with.

The file is operator input, like the flags: unknown keys and wrong types are
errors, not warnings, so a typo cannot silently leave Kairo on a default.
Credentials are never configuration: the provider's CLI authenticates itself.

    [cognition]
    providers = "claude"       # --cognition
    model = "opus"             # --model
    timeout = 300              # --cognition-timeout
    calls_per_day = 0          # 0: no budget (the default)

    [cognition.options.claude]  # --provider-opt claude.KEY=VALUE
    executable = "/home/kairo/.local/bin/claude"

    [runtime]
    reassess = 300             # --reassess
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = "kairo.toml"

# Every setting, by section: name -> (type, python type name for messages).
COGNITION = {
    "providers": str,
    "model": str,
    "timeout": float,
    "calls_per_day": int,
}
RUNTIME = {
    "reassess": float,
}
TYPE_NAMES = {str: "a string", float: "a number", int: "a whole number"}


class ConfigError(ValueError):
    """The configuration file is unreadable, malformed, or says something
    Kairo will not guess at. The operator fixes it; Kairo does not start."""


@dataclass(frozen=True)
class Config:
    """Settings read from a file. Every field is None when the file did not say,
    so the caller can tell "not configured" from "configured to the default"."""

    path: Path | None = None
    providers: str | None = None
    model: str | None = None
    timeout: float | None = None
    calls_per_day: int | None = None
    reassess: float | None = None
    # provider id -> {option: value}, as --provider-opt would give them
    options: dict[str, dict[str, str]] = field(default_factory=dict)

    def provider_opts(self) -> list[str]:
        """The options as ``ID.KEY=VALUE`` strings, the form the registry takes."""
        return [f"{provider}.{key}={value}"
                for provider, opts in sorted(self.options.items())
                for key, value in sorted(opts.items())]


def default_path(db: Path) -> Path:
    """Where the file lives when no --config is given: beside the database."""
    return db.parent / CONFIG_NAME


def load(path: Path, required: bool) -> Config:
    """Read and validate the file. A missing file is empty configuration unless
    it was asked for by name (``required``), in which case its absence is an
    error: an operator who passed --config meant that file."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise ConfigError(f"{path}: no such configuration file") from None
        return Config()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read ({exc})") from None
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: not valid TOML ({exc})") from None
    return _from_document(path, document)


def _from_document(path: Path, document: dict[str, Any]) -> Config:
    unknown = sorted(set(document) - {"cognition", "runtime"})
    if unknown:
        raise ConfigError(f"{path}: unknown section(s) {unknown}; "
                          f"known: ['cognition', 'runtime']")
    cognition = _table(path, document, "cognition")
    options = cognition.pop("options", {})
    runtime = _table(path, document, "runtime")
    values = {
        **_settings(path, "cognition", cognition, COGNITION),
        **_settings(path, "runtime", runtime, RUNTIME),
    }
    if values.get("timeout") is not None and values["timeout"] <= 0:
        raise ConfigError(f"{path}: cognition.timeout must be > 0")
    if values.get("reassess") is not None and values["reassess"] < 0:
        raise ConfigError(f"{path}: runtime.reassess must be >= 0")
    if values.get("calls_per_day") is not None and values["calls_per_day"] < 0:
        raise ConfigError(f"{path}: cognition.calls_per_day must be >= 0 (0: no budget)")
    return Config(path=path, options=_options(path, options), **values)


def _table(path: Path, document: dict[str, Any], name: str) -> dict[str, Any]:
    table = document.get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [{name}] must be a table")
    return dict(table)


def _settings(path: Path, section: str, table: dict[str, Any],
              known: dict[str, type]) -> dict[str, Any]:
    unknown = sorted(set(table) - set(known))
    if unknown:
        raise ConfigError(f"{path}: unknown setting(s) in [{section}]: {unknown}; "
                          f"known: {sorted(known)}")
    values: dict[str, Any] = {}
    for key, wanted in known.items():
        if key not in table:
            continue
        value = table[key]
        if isinstance(value, bool) or not isinstance(value, (wanted, int) if wanted is float
                                                     else wanted):
            raise ConfigError(f"{path}: {section}.{key} must be {TYPE_NAMES[wanted]}")
        values[key] = wanted(value)
    return values


def _options(path: Path, options: Any) -> dict[str, dict[str, str]]:
    """[cognition.options.<provider>] tables. Values are given to the provider as
    strings, exactly as --provider-opt would; nothing here is a credential."""
    if not isinstance(options, dict):
        raise ConfigError(f"{path}: [cognition.options] must be a table of provider tables")
    built: dict[str, dict[str, str]] = {}
    for provider, table in options.items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [cognition.options.{provider}] must be a table")
        entries: dict[str, str] = {}
        for key, value in table.items():
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ConfigError(f"{path}: cognition.options.{provider}.{key} must be "
                                  f"a string or number")
            entries[key] = str(value)
        built[provider] = entries
    return built
