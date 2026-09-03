"""Load bakeoff provider credentials from the process environment."""

from __future__ import annotations

import os
from collections.abc import Mapping


REQUIRED_PROVIDER_KEYS = (
    "OPENROUTER_API_KEY",
    "DEEPLINE_API_KEY",
    "SCRAPINGDOG_API_KEY",
)
OPTIONAL_PROVIDER_KEYS = ("EXA_API_KEY",)


def load_provider_secrets(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return required credentials and any configured optional credentials."""
    source = os.environ if environment is None else environment
    missing = [
        name for name in REQUIRED_PROVIDER_KEYS if not source.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError(
            "required provider environment variables are unavailable: "
            + ", ".join(missing)
        )
    return {
        name: source[name].strip()
        for name in (*REQUIRED_PROVIDER_KEYS, *OPTIONAL_PROVIDER_KEYS)
        if source.get(name, "").strip()
    }
