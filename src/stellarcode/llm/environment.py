"""Shared environment lookup for provider-neutral model configuration."""

from __future__ import annotations

import os


def first_env(*names: str, default: str | None = None) -> str | None:
    """Return the first non-empty environment value in precedence order.

    Provider-neutral names are passed first by callers. Provider-specific names
    remain as compatibility fallbacks for existing development and user `.env`
    files.
    """

    for name in names:
        if not name:
            continue
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default
