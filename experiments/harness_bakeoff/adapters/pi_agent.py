"""Thin Python entrypoint for the isolated Pi bakeoff worker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from ..models import normalize_icp, validate_companies
from ..prompt import SYSTEM_PROMPT, build_prompt


_WORKER = Path(__file__).resolve().parents[1] / "pi" / "worker.mjs"
_LAST_USAGE: dict[str, Any] = {}


def _timeout_seconds() -> float:
    raw = os.environ.get("BAKEOFF_RUN_TIMEOUT_SECONDS", "720")
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError("BAKEOFF_RUN_TIMEOUT_SECONDS must be numeric") from exc
    if timeout <= 0:
        raise ValueError("BAKEOFF_RUN_TIMEOUT_SECONDS must be positive")
    return timeout + 15


def run_icp(icp: dict[str, Any]) -> list[dict[str, Any]]:
    """Run one ICP through Pi and return its ranked company objects."""

    if not isinstance(icp, dict):
        raise TypeError("icp must be a dict")
    if not _WORKER.is_file():
        raise RuntimeError(f"Pi worker is missing: {_WORKER}")

    node = os.environ.get("BAKEOFF_NODE", "node")
    if not Path(node).is_absolute() and shutil.which(node) is None:
        raise RuntimeError(f"Node executable is unavailable: {node}")

    max_companies = max(1, min(int(os.environ.get("BAKEOFF_MAX_COMPANIES", "5")), 5))
    request = {
        "protocol": "leadpoet.harness_bakeoff.pi.v1",
        "icp": normalize_icp(icp),
        "prompt": build_prompt(icp, max_companies=max_companies),
        "system_prompt": SYSTEM_PROMPT,
    }
    environment = os.environ.copy()
    environment["BAKEOFF_MAX_COMPANIES"] = str(max_companies)

    try:
        completed = subprocess.run(
            [node, str(_WORKER)],
            input=json.dumps(request, separators=(",", ":")),
            text=True,
            capture_output=True,
            check=False,
            timeout=_timeout_seconds(),
            env=environment,
            cwd=_WORKER.parent,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Pi worker exceeded its run deadline") from exc

    payload: Any = None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip() or "worker exited without an error message"
            )
            raise RuntimeError(f"Pi worker failed: {detail}") from exc
        raise RuntimeError("Pi worker returned invalid JSON") from exc
    if isinstance(payload, dict):
        result = payload.get("companies")
        usage = payload.get("usage")
    else:
        result = payload
        usage = None
    _LAST_USAGE.clear()
    if isinstance(usage, dict):
        _LAST_USAGE.update(usage)
    if completed.returncode != 0 or (
        isinstance(payload, dict) and payload.get("ok") is False
    ):
        detail = (
            str(payload.get("error") or "").strip() if isinstance(payload, dict) else ""
        )
        detail = (
            detail
            or completed.stderr.strip()
            or "worker exited without an error message"
        )
        raise RuntimeError(f"Pi worker failed: {detail}")
    return validate_companies(result, max_companies=max_companies)


def get_last_usage() -> dict[str, Any]:
    """Return model and harness usage from the most recent run."""

    return dict(_LAST_USAGE)


__all__ = ["get_last_usage", "run_icp"]
