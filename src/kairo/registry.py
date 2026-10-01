"""Which cognition providers exist, and how to build a configured order of them.

A static table of factories, used only when Kairo is configured (the CLI); the
runtime never sees it. Adding a provider (e.g. Gemini) means adding an adapter
module and one entry here.

Order syntax: ``claude,claude@haiku``. Each entry is a provider instance id:
the provider kind, optionally ``@label`` to tell two instances of the same kind
apart. The first entry is asked first, the rest only on technical failure.

Options: ``<instance>.<key>=<value>``, validated by the provider. Credentials are
never options: command lines are visible to every user of the machine.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kairo.claude import ClaudeCognition
from kairo.cognition import Cognition, CognitionProvider

PROVIDERS: dict[str, Callable[..., CognitionProvider]] = {
    "claude": ClaudeCognition.from_options,
}

_INSTANCE = re.compile(r"^([a-z][a-z0-9_]*)(@[a-z0-9_-]+)?$")
_OPTION = re.compile(r"^([a-z][a-z0-9_]*(?:@[a-z0-9_-]+)?)\.([a-z_]+)=(.*)$", re.S)
_SECRET_KEY = re.compile(r"key|token|secret|passw|credential|auth", re.I)


def parse_order(text: str) -> list[tuple[str, str]]:
    """``"claude,claude@haiku"`` -> [(instance id, provider kind), ...]."""
    entries = [part.strip() for part in text.split(",")]
    if not all(entries):
        raise ValueError(f"empty provider in {text!r}")
    order = []
    for entry in entries:
        match = _INSTANCE.match(entry)
        if not match:
            raise ValueError(f"malformed provider {entry!r} (expected kind or kind@label)")
        if match.group(1) not in PROVIDERS:
            raise ValueError(f"unknown provider {match.group(1)!r}; known: {sorted(PROVIDERS)}")
        order.append((entry, match.group(1)))
    if len({instance for instance, _ in order}) != len(order):
        raise ValueError(f"provider listed twice in {text!r}; use kind@label for a second instance")
    return order


def parse_options(items: list[str], instances: set[str]) -> dict[str, dict[str, str]]:
    """``["claude.model=haiku"]`` -> {"claude": {"model": "haiku"}}."""
    options: dict[str, dict[str, str]] = {}
    for item in items:
        match = _OPTION.match(item)
        if not match:
            raise ValueError(f"malformed provider option {item!r} (expected instance.key=value)")
        instance, key, value = match.groups()
        if _SECRET_KEY.search(key):
            raise ValueError(f"{item.split('=', 1)[0]}: credentials are never passed on the "
                             "command line; providers read them from their own environment "
                             "variables or credential files")
        if instance not in instances:
            raise ValueError(f"option for {instance!r}, which is not in the provider order")
        if key in options.setdefault(instance, {}):
            raise ValueError(f"option {instance}.{key} given twice")
        options[instance][key] = value
    return options


def build_cognition(order_text: str, option_items: list[str] | None = None,
                    defaults: dict[str, dict[str, str]] | None = None,
                    workdir: str | Path | None = None) -> Cognition | None:
    """A Cognition over the configured providers, or None for ``"none"``.
    ``defaults`` holds per-kind options (from older shortcut flags); explicit
    per-instance options override them."""
    if order_text.strip() == "none":
        if option_items:
            raise ValueError("provider options given, but no cognition provider is configured")
        return None
    order = parse_order(order_text)
    options = parse_options(option_items or [], {instance for instance, _ in order})
    providers: list[Any] = []
    for instance, kind in order:
        merged = {**(defaults or {}).get(kind, {}), **options.get(instance, {})}
        providers.append(PROVIDERS[kind](instance, merged, workdir=workdir))
    return Cognition(providers)
