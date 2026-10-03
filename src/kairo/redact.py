"""Keep secrets and oversized data out of what Kairo persists and shows cognition.

Actions run with the runtime's environment, so their output can contain
credential values (``env`` alone would print them). Values of environment
variables whose names look secret are replaced with a marker, and long strings
are truncated, before anything is written to memory or sent to a provider.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# An environment variable's name marks its value as secret when it contains one
# of these (any case, anywhere: GITHUB_TOKEN2, APIKEYID, KEYPASS, accessKeyId) ...
_SECRET_PART = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION")
# ... outside these whole words, which contain one but name no secret. Exactly
# these words are exempt, so the rule is never weaker than plain substring
# matching except for them (GIT_AUTHOR_NAME, authorName, KEYBOARD_LAYOUT,
# TOKENIZER_MODEL). Words split at non-letters and at camelCase boundaries.
_NOT_SECRET_WORDS = frozenset({"AUTHOR", "AUTHORS", "AUTHORED", "AUTHORITY", "AUTHORITIES",
                               "XAUTHORITY", "KEYBOARD", "KEYBOARDS", "TOKENIZER", "TOKENIZERS"})
_NAME_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")
MIN_SECRET_LENGTH = 8  # shorter values are too likely to match ordinary text
MARKER = "[redacted]"
FILE_SECRET_LENGTH = 20  # strings at least this long in a credential file are treated as secrets

# Credentials declared by cognition providers and implementations: environment
# variable names (secret whatever they are called) and credential files. Their
# values are always redacted, and kept away from processes that do not own them
# (see scrubbed_env). Each name has one owner; a provider's claim always wins,
# so no implementation can claim a provider's credential.
PROVIDER = "provider"
_PROTECTED_ENV: set[str] = set()
_OWNERS: dict[str, str] = {}
_PROTECTED_FILES: set[str] = set()
_file_cache: dict[str, tuple[float, frozenset[str]]] = {}


def protect_env(names: Any, owner: str = PROVIDER) -> None:
    for name in names:
        if isinstance(name, str) and name:
            _PROTECTED_ENV.add(name)
            if owner == PROVIDER or name not in _OWNERS:
                _OWNERS[name] = owner


def env_owner(name: str) -> str | None:
    return _OWNERS.get(name)


def protect_files(paths: Any) -> None:
    _PROTECTED_FILES.update(os.path.expanduser(str(p)) for p in paths if p)


def protected_files() -> frozenset[str]:
    return frozenset(_PROTECTED_FILES)


def scrubbed_env(keep: Any = ()) -> dict[str, str]:
    """The process environment without provider credentials, except ``keep``
    (a provider's own). For subprocesses: actions keep none."""
    keep = set(keep)
    return {k: v for k, v in os.environ.items() if k not in _PROTECTED_ENV or k in keep}


def _file_secrets(path: str) -> frozenset[str]:
    """Long string values in a credential file (tokens), cached by mtime. Read
    only to recognise them in output, never stored or shown."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return frozenset()
    cached = _file_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    found: set[str] = set()
    try:
        text = Path(path).read_text(errors="replace")
        try:
            def walk(v: Any) -> None:
                if isinstance(v, str) and len(v) >= FILE_SECRET_LENGTH:
                    found.add(v)
                elif isinstance(v, dict):
                    for x in v.values():
                        walk(x)
                elif isinstance(v, list):
                    for x in v:
                        walk(x)
            walk(json.loads(text))
        except ValueError:  # not JSON: treat long lines as secrets
            found.update(line.strip() for line in text.splitlines()
                         if len(line.strip()) >= FILE_SECRET_LENGTH)
    except OSError:
        pass
    _file_cache[path] = (mtime, frozenset(found))
    return _file_cache[path][1]


def secret_name(name: str) -> bool:
    """Whether an environment variable's name marks its value as a secret."""
    kept = _NAME_WORD.sub(lambda m: "_" if m.group().upper() in _NOT_SECRET_WORDS else m.group(),
                          name)
    return bool(_SECRET_PART.search(kept.upper()))


def secret_values(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    values = {v for k, v in env.items()
              if (secret_name(k) or k in _PROTECTED_ENV) and len(v) >= MIN_SECRET_LENGTH}
    for path in _PROTECTED_FILES:
        values |= _file_secrets(path)
    return sorted(values, key=len, reverse=True)  # longest first: no partial leftovers


def head_tail(text: Any, limit: int) -> Any:
    """``text`` cut to at most ``limit`` characters keeping both its beginning and
    its end (where results and errors usually are), with an explicit marker for
    the middle left out. Unchanged when it fits, or when it is not a string.
    Redact before cutting, so a secret can never be split across the cut."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    reserve = 48  # room for the marker
    if limit <= reserve + 2:
        return text[:limit]
    head = (limit - reserve) // 2
    tail = limit - reserve - head
    marker = f"\n[truncated {len(text) - head - tail} chars in the middle]\n"
    return text[:head] + marker + text[len(text) - tail:]


def redact(value: Any, limit: int | None = None, secrets: list[str] | None = None) -> Any:
    """Return a copy of ``value`` (JSON-like data) with secret values replaced
    and, if ``limit`` is given, every string cut to at most ``limit`` chars."""
    secrets = secret_values() if secrets is None else secrets

    def clean(v: Any) -> Any:
        if isinstance(v, str):
            for s in secrets:
                if s in v:
                    v = v.replace(s, MARKER)
            if limit is not None and len(v) > limit:
                v = f"{v[:limit]}… [truncated {len(v) - limit} chars]"
            return v
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        return v

    return clean(value)
