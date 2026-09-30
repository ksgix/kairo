"""Keep secrets and oversized data out of what Kairo persists and shows cognition.

Actions run with the runtime's environment, so their output can contain
credential values (``env`` alone would print them). Values of environment
variables whose names look secret are replaced with a marker, and long strings
are truncated, before anything is written to memory or sent to a provider.
"""

from __future__ import annotations

import os
import re
from typing import Any

SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)
MIN_SECRET_LENGTH = 8  # shorter values are too likely to match ordinary text
MARKER = "[redacted]"


def secret_values(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    values = {v for k, v in env.items() if SECRET_NAME.search(k) and len(v) >= MIN_SECRET_LENGTH}
    return sorted(values, key=len, reverse=True)  # longest first: no partial leftovers


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
