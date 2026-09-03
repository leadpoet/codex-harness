"""Load normalized ICPs from a user-supplied JSON file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .models import normalize_icp


def external_icp_file(path: str | os.PathLike[str], repository: Path) -> Path:
    """Resolve one existing ICP file and reject files inside the repository."""
    candidate = Path(path).expanduser().resolve(strict=True)
    if not candidate.is_file():
        raise ValueError("--icp-file must name a JSON file")
    try:
        candidate.relative_to(repository.resolve())
    except ValueError:
        return candidate
    raise ValueError("--icp-file must be outside the repository")


def _payload_icps(payload: Any) -> list[Any]:
    if isinstance(payload, dict) and isinstance(payload.get("icps"), list):
        return payload["icps"]
    if (
        isinstance(payload, list)
        and len(payload) == 1
        and isinstance(payload[0], dict)
        and "icp_id" not in payload[0]
        and isinstance(payload[0].get("icps"), list)
    ):
        return payload[0]["icps"]
    if isinstance(payload, list):
        return payload
    raise ValueError("ICP JSON must be a list or an object with an 'icps' list")


def load_icps(
    *,
    icp_file: str | os.PathLike[str],
    icp_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Load, normalize, select, and order ICPs by their requested IDs."""
    with Path(icp_file).open(encoding="utf-8") as handle:
        raw_icps = _payload_icps(json.load(handle))

    by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_icps):
        if not isinstance(item, dict):
            raise ValueError(f"ICP at index {index} must be an object")
        icp_id = str(item.get("icp_id") or "").strip()
        if not icp_id:
            raise ValueError(f"ICP at index {index} has no icp_id")
        if icp_id in by_id:
            raise ValueError(f"ICP file contains duplicate ID: {icp_id}")
        normalized = normalize_icp(item)
        normalized["icp_id"] = icp_id
        by_id[icp_id] = normalized

    requested = (
        tuple(str(value) for value in icp_ids) if icp_ids is not None else tuple(by_id)
    )
    if not requested:
        raise ValueError("ICP file contains no ICPs")
    if any(not value for value in requested):
        raise ValueError("requested ICP IDs must not be empty")
    if len(set(requested)) != len(requested):
        raise ValueError("requested ICP IDs contain duplicates")
    missing = [value for value in requested if value not in by_id]
    if missing:
        raise ValueError("ICP file is missing requested IDs: " + ", ".join(missing))
    return [by_id[value] for value in requested]


def describe_icps(icps: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a small operational summary that does not include full ICP text."""
    return [
        {
            "icp_id": str(icp.get("icp_id") or ""),
            "industry": str(icp.get("industry") or ""),
            "geography": str(icp.get("geography") or ""),
            "employee_count": list(icp.get("employee_count") or []),
            "intent_categories": [
                row.get("category") for row in icp.get("required_intents", [])
            ],
        }
        for icp in icps
    ]
