"""Build blind live-evidence audits and score sourcing bakeoff results."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import stat
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .providers import _assert_public_url, _extract_text
from .models import normalize_icp, validate_companies
from .runner import ARMS

try:
    from publicsuffix2 import get_sld as _get_sld
except ImportError:  # pragma: no cover - optional dependency
    _get_sld = None


DEFAULT_ROOT = (
    Path.home() / "Downloads" / "deepline" / "data" / "codex-harness-bakeoff" / "audit"
)
EXPECTED_COMPANIES = 5
SCORE_NAMES = ("icp_fit", "intent_validity", "why_now_quality", "evidence_quality")
CLAIM_CHECKS = ("correct_company", "supports_claim", "fresh", "commercially_meaningful")
CHALLENGER_ARMS = ARMS
REPORT_SCHEMA = "leadpoet_harness_bakeoff_report_v3"
COMBINED_REPORT_SCHEMA = "leadpoet_harness_bakeoff_combined_report_v2"
NEAR_TIE_RATE = 0.05
_HOSTNAME_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")
_COMPOUND_SUFFIXES = frozenset(
    {
        "ac.uk",
        "co.in",
        "co.jp",
        "co.nz",
        "co.za",
        "co.uk",
        "co.kr",
        "co.id",
        "co.il",
        "co.ke",
        "co.ma",
        "co.th",
        "com.au",
        "com.ar",
        "com.br",
        "com.cn",
        "com.co",
        "com.hk",
        "com.my",
        "com.mx",
        "com.ph",
        "com.pk",
        "com.sa",
        "com.sg",
        "com.tr",
        "com.tw",
        "com.ua",
        "com.vn",
        "go.jp",
        "gov.uk",
        "ltd.uk",
        "me.uk",
        "ne.jp",
        "net.au",
        "or.jp",
        "org.au",
        "org.br",
        "org.uk",
        "plc.uk",
    }
)
RUBRIC = {
    "scores": {
        "icp_fit": "0=no fit, 1=partial/uncertain fit, 2=clear fit to all material ICP constraints",
        "intent_validity": "0=invalid, 1=plausible/weak, 2=correct, current, and commercially meaningful",
        "why_now_quality": "0=not useful, 1=understandable, 2=specific and immediately useful to a salesperson",
        "evidence_quality": "0=unsupported, 1=indirect/partial, 2=direct public evidence for the claim",
    },
    "sales_ready": "icp_fit=2, intent_validity=2, why_now_quality>=1, evidence_quality>=1, with no disqualifier",
    "review_rule": "Open and inspect every linked page. Do not score from the supplied snippet alone.",
}


def _outside_repository(path: Path) -> Path:
    expanded = Path(os.path.abspath(path.expanduser()))
    _reject_symlink_components(expanded)
    try:
        resolved = expanded.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "private audit output path could not be resolved safely"
        ) from exc
    _reject_symlink_components(resolved)
    repository = Path(__file__).resolve().parents[2]
    for candidate in (resolved, *resolved.parents):
        if candidate == repository:
            raise ValueError("private audit output must be outside the repository")
        try:
            same_repository = candidate.samefile(repository)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(
                "private audit output path could not be verified safely"
            ) from exc
        if same_repository:
            raise ValueError("private audit output must be outside the repository")
    return resolved


def _reject_symlink_components(path: Path) -> None:
    """Reject existing symbolic links in one absolute output path."""
    if not path.is_absolute():
        raise ValueError("private audit output path must be absolute")
    for candidate in reversed((path, *path.parents)):
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(
                "private audit output path could not be verified safely"
            ) from exc
        if stat.S_ISLNK(mode):
            raise ValueError("private audit output path cannot contain symbolic links")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on {path}:{number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"row {number} must be a JSON object")
        rows.append(row)
    if not rows:
        raise ValueError("result file is empty")
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _validate_matrix(
    rows: list[dict[str, Any]],
    *,
    expected_arms: tuple[str, ...],
    expected_icp_ids: tuple[str, ...],
    repetitions: int,
    evaluation_date: date,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Refuse to score a partial, duplicate, smoke, or mixed-model matrix."""
    if not expected_arms or len(set(expected_arms)) != len(expected_arms):
        raise ValueError("expected arms must be a nonempty unique list")
    if not expected_icp_ids or len(set(expected_icp_ids)) != len(expected_icp_ids):
        raise ValueError("expected ICP IDs must be a nonempty unique list")
    expected_count = len(expected_arms) * len(expected_icp_ids) * repetitions
    if len(rows) != expected_count:
        raise ValueError(
            f"expected exactly {expected_count} scored attempts, found {len(rows)}"
        )
    expected_arm_set = set(expected_arms)
    expected_icp_set = set(expected_icp_ids)
    models = {str(row.get("model") or "") for row in rows}
    if len(models) != 1 or "" in models:
        raise ValueError("all scored attempts must use one explicit model")
    seen: set[tuple[str, int, str]] = set()
    block_orders: dict[tuple[str, int], set[int]] = {}
    normalized_inputs: dict[str, str] = {}
    normalized_icps: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        if row.get("phase") != "scored":
            raise ValueError(f"attempt {index} is not from the scored phase")
        if row.get("evaluation_date") != evaluation_date.isoformat():
            raise ValueError(f"attempt {index} used a different evaluation date")
        if (
            type(row.get("ok")) is not bool
            or type(row.get("eligible_for_scoring")) is not bool
        ):
            raise ValueError(
                f"attempt {index} has invalid completion or eligibility flags"
            )
        if row["ok"] != row["eligible_for_scoring"]:
            raise ValueError(
                f"attempt {index} has inconsistent completion and eligibility flags"
            )
        companies = row.get("companies")
        if not isinstance(companies, list):
            raise ValueError(f"attempt {index} has an invalid company result")
        try:
            validated_companies = validate_companies(
                companies,
                max_companies=int(row.get("max_companies", EXPECTED_COMPANIES)),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"attempt {index} has an invalid company result") from exc
        if validated_companies != companies or row.get("company_count") != len(
            companies
        ):
            raise ValueError(f"attempt {index} has a noncanonical company result")
        arm = str(row.get("arm") or "")
        icp_id = str(row.get("icp_id") or "")
        repetition = row.get("repetition")
        order = row.get("order")
        if arm not in expected_arm_set or icp_id not in expected_icp_set:
            raise ValueError(f"attempt {index} has an unexpected arm or ICP")
        if (
            isinstance(repetition, bool)
            or not isinstance(repetition, int)
            or not 1 <= repetition <= repetitions
        ):
            raise ValueError(f"attempt {index} has an invalid repetition")
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or not 1 <= order <= len(expected_arms)
        ):
            raise ValueError(f"attempt {index} has an invalid randomized order")
        if row.get("block_id") != f"{icp_id}:r{repetition}":
            raise ValueError(f"attempt {index} has an invalid block_id")
        value = row.get("input")
        if not isinstance(value, dict) or str(value.get("icp_id") or "") != icp_id:
            raise ValueError(f"attempt {index} does not contain its matching ICP input")
        try:
            normalized_value = normalize_icp(value)
            normalized_input = json.dumps(
                normalized_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"attempt {index} has an invalid normalized ICP input"
            ) from exc
        prior_input = normalized_inputs.setdefault(icp_id, normalized_input)
        if normalized_input != prior_input:
            raise ValueError(
                f"attempt {index} uses a different normalized ICP payload for {icp_id}"
            )
        normalized_icps.setdefault(icp_id, normalized_value)
        key = (icp_id, repetition, arm)
        if key in seen:
            raise ValueError(f"duplicate scored attempt: {key}")
        seen.add(key)
        block_orders.setdefault((icp_id, repetition), set()).add(order)
    expected_orders = set(range(1, len(expected_arms) + 1))
    if any(orders != expected_orders for orders in block_orders.values()):
        raise ValueError(
            "every ICP/repetition block must contain one complete randomized order"
        )
    return normalized_icps, next(iter(models))


def _write_new(path: Path, value: Any) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    path = _outside_repository(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _outside_repository(path)
    if not path.parent.is_dir():
        raise ValueError("private audit output parent must be a directory")

    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        parent_descriptor = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise ValueError(
            "private audit output directory could not be opened safely"
        ) from exc

    descriptor: int | None = None
    created = False
    try:
        parent_status = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(parent_status.st_mode):
            raise ValueError("private audit output parent must be a directory")
        os.fchmod(parent_descriptor, 0o700)

        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(
                path.name,
                file_flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError as exc:
            raise RuntimeError(
                f"refusing to overwrite private audit data: {path}"
            ) from exc
        created = True
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("private audit output write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                os.unlink(path.name, dir_fd=parent_descriptor)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _normalized_url(value: Any) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")
    )


def _homepage(value: str) -> bool:
    parsed = urlsplit(value)
    return (parsed.path or "/") == "/"


def _fetch_evidence(url: str, *, timeout: float, max_chars: int) -> dict[str, Any]:
    requested = _assert_public_url(url)
    accept = (
        "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.5"
    )
    status_code: int | None = None
    current = requested
    last_error = ""
    # Some public sites reject a named audit crawler while others reject the
    # default client. Try both ordinary identities before recording a miss.
    for user_agent in ("LeadpoetBlindAudit/1.0", None):
        current = requested
        headers = {"Accept": accept}
        if user_agent is not None:
            headers["User-Agent"] = user_agent
        try:
            with httpx.Client(
                headers=headers,
                follow_redirects=False,
                timeout=timeout,
                trust_env=False,
            ) as client:
                for _ in range(5):
                    response = client.get(current)
                    status_code = response.status_code
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError("redirect did not include a location")
                        current = _assert_public_url(urljoin(current, location))
                        continue
                    raw = response.text[:1_500_000]
                    title, text = _extract_text(raw, current)
                    ok = 200 <= response.status_code < 300 and bool(text.strip())
                    if ok:
                        return {
                            "requested_url": url,
                            "final_url": current,
                            "opened": True,
                            "status_code": response.status_code,
                            "title": title,
                            "text": text[:max_chars],
                            "error": "",
                        }
                    last_error = f"HTTP {response.status_code} or empty page text"
                    break
                else:
                    last_error = "too many redirects"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    return {
        "requested_url": url,
        "final_url": current,
        "opened": False,
        "status_code": status_code,
        "title": "",
        "text": "",
        "error": last_error,
    }


def _all_urls(rows: Iterable[dict[str, Any]]) -> list[str]:
    values: set[str] = set()
    for row in rows:
        for company in row.get("companies", []):
            if not isinstance(company, dict):
                continue
            candidates: list[Any] = [
                company.get("company_website"),
                company.get("company_linkedin"),
            ]
            candidates.extend(company.get("fit_evidence_urls") or [])
            for signal in company.get("intent_signals") or []:
                if isinstance(signal, dict):
                    candidates.append(signal.get("url"))
            for candidate in candidates:
                normalized = _normalized_url(candidate)
                if normalized:
                    values.add(normalized)
    return sorted(values)


def _fetch_all(
    urls: list[str], *, workers: int, timeout: float, max_chars: int
) -> dict[str, dict[str, Any]]:
    fetched: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="evidence"
    ) as executor:
        futures = {
            executor.submit(
                _fetch_evidence, url, timeout=timeout, max_chars=max_chars
            ): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                fetched[url] = future.result()
            except Exception as exc:
                fetched[url] = {
                    "requested_url": url,
                    "final_url": url,
                    "opened": False,
                    "status_code": None,
                    "title": "",
                    "text": "",
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                }
    return fetched


def _parse_day(value: Any) -> date | None:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _registrable_domain(value: str) -> str:
    """Return a stable registrable domain, with a safe local fallback."""

    try:
        raw = (value or "").strip()
        host = (
            urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
        ).lower()
        host = host.strip("+-_.")
        if host and not host.isascii():
            host = host.encode("idna").decode("ascii")
        if not host or not _HOSTNAME_RE.fullmatch(host):
            return ""
        if _get_sld is not None:
            domain = _get_sld(host)
            if domain and "." in domain:
                return domain.lower()
        if host.startswith("www."):
            host = host[4:]
        labels = host.split(".")
        if len(labels) >= 3 and ".".join(labels[-2:]) in _COMPOUND_SUFFIXES:
            return ".".join(labels[-3:])
        return ".".join(labels[-2:])
    except (UnicodeError, ValueError):
        return ""


def _company_domain(company: dict[str, Any]) -> str:
    return _registrable_domain(str(company.get("company_website") or ""))


def _roles(company: dict[str, Any], url_ids: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(value: Any, role: str, claim_index: int | None = None) -> None:
        normalized = _normalized_url(value)
        if not normalized or normalized not in url_ids:
            return
        row: dict[str, Any] = {"evidence_id": url_ids[normalized], "role": role}
        if claim_index is not None:
            row["claim_index"] = claim_index
        rows.append(row)

    add(company.get("company_website"), "company_website")
    add(company.get("company_linkedin"), "company_linkedin")
    for value in company.get("fit_evidence_urls") or []:
        add(value, "fit")
    for index, signal in enumerate(company.get("intent_signals") or []):
        if isinstance(signal, dict):
            add(signal.get("url"), "intent", index)
    return rows


def _claim_flags(
    signal: dict[str, Any],
    icp: dict[str, Any],
    fetched: dict[str, dict[str, Any]],
    evaluation_date: date,
) -> list[str]:
    flags: list[str] = []
    url = _normalized_url(signal.get("url"))
    if not url or not fetched.get(url, {}).get("opened"):
        flags.append("evidence_url_unavailable")
    elif _homepage(url) or _homepage(str(fetched[url].get("final_url") or "")):
        flags.append("homepage_only_evidence")
    required = icp.get("required_intents") or []
    index = signal.get("matched_icp_signal")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index >= len(required)
    ):
        flags.append("matched_icp_signal_out_of_range")
        intent = None
    else:
        intent = required[index] if isinstance(required[index], dict) else None
    event_date = _parse_day(signal.get("date"))
    if event_date is None:
        flags.append("intent_date_missing_or_invalid")
    elif event_date > evaluation_date:
        flags.append("intent_date_is_in_the_future")
    elif intent is not None and intent.get("max_age_days") not in (None, ""):
        try:
            oldest = evaluation_date - timedelta(
                days=max(0, int(intent["max_age_days"]))
            )
            if event_date < oldest:
                flags.append("intent_is_stale")
        except (TypeError, ValueError):
            flags.append("icp_freshness_limit_invalid")
    return flags


def _review_template(item: dict[str, Any]) -> dict[str, Any]:
    fit_evidence_ids = sorted(
        {
            ref["evidence_id"]
            for ref in item.get("evidence_refs", [])
            if ref.get("role") == "fit"
        }
    )
    return {
        "review_id": item["review_id"],
        "scores": {name: None for name in SCORE_NAMES},
        "fit_evidence_checks": [
            {"evidence_id": evidence_id, "supports_fit": None, "notes": ""}
            for evidence_id in fit_evidence_ids
        ],
        "intent_checks": [
            {
                "claim_index": claim["claim_index"],
                **{name: None for name in CLAIM_CHECKS},
                "notes": "",
            }
            for claim in item["intent_claims"]
        ],
        "severe_false_positive": None,
        "reviewer_borderline": None,
        "notes": "",
    }


def build_packet(
    *,
    results_path: Path,
    output: Path,
    evaluation_date: date,
    seed: int,
    workers: int,
    timeout: float,
    max_chars: int,
    expected_icp_ids: tuple[str, ...],
    expected_arms: tuple[str, ...] = ARMS,
    repetitions: int = 2,
) -> dict[str, Path]:
    output = _outside_repository(output)
    rows = _read_jsonl(results_path)
    normalized_icps, model = _validate_matrix(
        rows,
        expected_arms=expected_arms,
        expected_icp_ids=expected_icp_ids,
        repetitions=repetitions,
        evaluation_date=evaluation_date,
    )
    arms = sorted({str(row.get("arm") or "") for row in rows})
    if not arms or "" in arms:
        raise ValueError("every attempt must contain an arm")

    rng = random.Random(seed)
    shuffled_arms = list(arms)
    rng.shuffle(shuffled_arms)
    arm_to_blind = {
        arm: f"bundle_{index:02d}" for index, arm in enumerate(shuffled_arms, start=1)
    }
    order = list(range(len(rows)))
    rng.shuffle(order)

    eligible_rows = [
        row
        for row in rows
        if row.get("ok") is True and row.get("eligible_for_scoring") is True
    ]
    urls = _all_urls(eligible_rows)
    fetched = _fetch_all(urls, workers=workers, timeout=timeout, max_chars=max_chars)
    url_ids = {url: f"evidence_{index:05d}" for index, url in enumerate(urls, start=1)}
    evidence = {url_ids[url]: fetched[url] for url in urls}

    packet_runs: list[dict[str, Any]] = []
    key_runs: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    review_number = 0
    for run_number, source_index in enumerate(order, start=1):
        row = rows[source_index]
        arm = str(row["arm"])
        blind_run = f"run_{run_number:04d}"
        blind_arm = arm_to_blind[arm]
        icp = normalized_icps[str(row["icp_id"])]
        companies = (
            row.get("companies")
            if row.get("ok") is True and row.get("eligible_for_scoring") is True
            else []
        )
        domain_counts: dict[str, int] = {}
        for company in companies:
            if isinstance(company, dict):
                domain = _company_domain(company)
                domain_counts[domain] = domain_counts.get(domain, 0) + 1

        run_review_ids: list[str] = []
        for rank, company in enumerate(companies, start=1):
            if not isinstance(company, dict):
                continue
            review_number += 1
            review_id = f"company_{review_number:05d}"
            run_review_ids.append(review_id)
            refs = _roles(company, url_ids)
            mechanical_flags: list[str] = []
            domain = _company_domain(company)
            if not domain or domain_counts.get(domain, 0) > 1:
                mechanical_flags.append("duplicate_or_missing_company_domain")
            fit_refs = [ref for ref in refs if ref["role"] == "fit"]
            if not fit_refs:
                mechanical_flags.append("missing_fit_evidence_url")
            elif not any(evidence[ref["evidence_id"]]["opened"] for ref in fit_refs):
                mechanical_flags.append("no_live_fit_evidence")

            claims: list[dict[str, Any]] = []
            for claim_index, signal in enumerate(company.get("intent_signals") or []):
                if not isinstance(signal, dict):
                    continue
                flags = _claim_flags(signal, icp, fetched, evaluation_date)
                claims.append(
                    {
                        "claim_index": claim_index,
                        "signal": signal,
                        "mechanical_flags": flags,
                    }
                )
            if not claims:
                mechanical_flags.append("missing_intent_claim")
            elif all(claim["mechanical_flags"] for claim in claims):
                mechanical_flags.append("no_mechanically_valid_intent_claim")

            item = {
                "review_id": review_id,
                "blind_arm": blind_arm,
                "blind_run": blind_run,
                "icp_id": str(row.get("icp_id") or icp.get("icp_id") or ""),
                "icp": icp,
                "rank": rank,
                "company": company,
                "evidence_refs": refs,
                "intent_claims": claims,
                "mechanical_flags": sorted(set(mechanical_flags)),
            }
            items.append(item)

        packet_runs.append(
            {
                "blind_run": blind_run,
                "blind_arm": blind_arm,
                "icp_id": str(row.get("icp_id") or icp.get("icp_id") or ""),
                "completed": bool(row.get("ok")),
                "returned_companies": len(run_review_ids),
                "expected_companies": EXPECTED_COMPANIES,
                "review_ids": run_review_ids,
            }
        )
        key_runs[blind_run] = {
            "arm": arm,
            "blind_arm": blind_arm,
            "icp_id": str(row.get("icp_id") or icp.get("icp_id") or ""),
            "estimated_combined_cost_usd": row.get("estimated_combined_cost_usd"),
            "latency_seconds": row.get("latency_seconds"),
            "provider_call_count": row.get("provider_call_count"),
            "error": str(row.get("error") or "")[:1000],
        }

    packet = {
        "schema": "leadpoet_blind_sales_audit_v1",
        "evaluation_date": evaluation_date.isoformat(),
        "matrix": {
            "arms": list(expected_arms),
            "icp_ids": list(expected_icp_ids),
            "repetitions": repetitions,
            "attempts": len(rows),
            "model": model,
        },
        "rubric": RUBRIC,
        "runs": packet_runs,
        "items": items,
        "evidence": evidence,
        "evidence_summary": {
            "distinct_urls": len(urls),
            "opened_urls": sum(bool(row["opened"]) for row in evidence.values()),
        },
    }
    key = {
        "schema": "leadpoet_blind_sales_audit_key_v1",
        "arm_by_blind": {blind: arm for arm, blind in arm_to_blind.items()},
        "runs": key_runs,
    }
    primary = {
        "schema": "leadpoet_primary_sales_review_v1",
        "reviews": [_review_template(item) for item in items],
    }
    paths = {
        "packet": output / "audit_packet.json",
        "key": output / "blind_key.json",
        "primary": output / "primary_review.json",
    }
    _write_new(paths["packet"], packet)
    _write_new(paths["key"], key)
    _write_new(paths["primary"], primary)
    return paths


def _score(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1, 2}:
        raise ValueError(f"{field} must be 0, 1, or 2")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be true or false")
    return value


def _validated_reviews(
    review_file: dict[str, Any],
    items: Iterable[dict[str, Any]],
    *,
    require_all: bool,
) -> dict[str, dict[str, Any]]:
    expected = {str(item["review_id"]): item for item in items}
    rows = review_file.get("reviews")
    if not isinstance(rows, list):
        raise ValueError("review file must contain a reviews list")
    parsed: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("each review must be an object")
        review_id = str(raw.get("review_id") or "")
        if review_id not in expected or review_id in parsed:
            raise ValueError(f"unknown or duplicate review_id: {review_id}")
        scores = raw.get("scores")
        if not isinstance(scores, dict):
            raise ValueError(f"{review_id}.scores must be an object")
        clean_scores = {
            name: _score(scores.get(name), f"{review_id}.scores.{name}")
            for name in SCORE_NAMES
        }
        fit_checks = raw.get("fit_evidence_checks")
        if not isinstance(fit_checks, list):
            raise ValueError(f"{review_id}.fit_evidence_checks must be a list")
        expected_fit_ids = {
            ref["evidence_id"]
            for ref in expected[review_id].get("evidence_refs", [])
            if ref.get("role") == "fit"
        }
        seen_fit_ids: set[str] = set()
        clean_fit_checks: list[dict[str, Any]] = []
        for check in fit_checks:
            if not isinstance(check, dict):
                raise ValueError(f"{review_id} has an invalid fit evidence check")
            evidence_id = str(check.get("evidence_id") or "")
            if evidence_id not in expected_fit_ids or evidence_id in seen_fit_ids:
                raise ValueError(
                    f"{review_id} has an unknown or duplicate fit evidence_id"
                )
            seen_fit_ids.add(evidence_id)
            clean_fit_checks.append(
                {
                    "evidence_id": evidence_id,
                    "supports_fit": _boolean(
                        check.get("supports_fit"),
                        f"{review_id}.fit_evidence_checks.{evidence_id}.supports_fit",
                    ),
                    "notes": str(check.get("notes") or ""),
                }
            )
        if seen_fit_ids != expected_fit_ids:
            raise ValueError(f"{review_id} must review every fit evidence URL")
        checks = raw.get("intent_checks")
        if not isinstance(checks, list):
            raise ValueError(f"{review_id}.intent_checks must be a list")
        expected_claims = {
            claim["claim_index"] for claim in expected[review_id]["intent_claims"]
        }
        clean_checks: list[dict[str, Any]] = []
        seen_claims: set[int] = set()
        for check in checks:
            if not isinstance(check, dict) or isinstance(
                check.get("claim_index"), bool
            ):
                raise ValueError(f"{review_id} has an invalid intent check")
            claim_index = check.get("claim_index")
            if (
                not isinstance(claim_index, int)
                or claim_index not in expected_claims
                or claim_index in seen_claims
            ):
                raise ValueError(f"{review_id} has an unknown or duplicate claim_index")
            seen_claims.add(claim_index)
            clean_checks.append(
                {
                    "claim_index": claim_index,
                    **{
                        name: _boolean(
                            check.get(name),
                            f"{review_id}.intent_checks.{claim_index}.{name}",
                        )
                        for name in CLAIM_CHECKS
                    },
                    "notes": str(check.get("notes") or ""),
                }
            )
        if seen_claims != expected_claims:
            raise ValueError(f"{review_id} must review every intent claim")
        parsed[review_id] = {
            "review_id": review_id,
            "scores": clean_scores,
            "fit_evidence_checks": clean_fit_checks,
            "intent_checks": clean_checks,
            "severe_false_positive": _boolean(
                raw.get("severe_false_positive"), f"{review_id}.severe_false_positive"
            ),
            "reviewer_borderline": _boolean(
                raw.get("reviewer_borderline"), f"{review_id}.reviewer_borderline"
            ),
            "notes": str(raw.get("notes") or ""),
        }
    if require_all and set(parsed) != set(expected):
        missing = sorted(set(expected) - set(parsed))
        raise ValueError(
            f"reviews are missing {len(missing)} items: {', '.join(missing[:10])}"
        )
    return parsed


def _borderline(review: dict[str, Any]) -> bool:
    return bool(review["reviewer_borderline"]) or any(
        value == 1 for value in review["scores"].values()
    )


def second_review_packet(
    *, packet_path: Path, primary_path: Path, output: Path, seed: int
) -> dict[str, Path]:
    output = _outside_repository(output)
    packet = _read_json(packet_path)
    items = packet.get("items")
    if not isinstance(items, list):
        raise ValueError("audit packet is missing items")
    primary = _validated_reviews(_read_json(primary_path), items, require_all=True)
    borderline = {
        review_id for review_id, review in primary.items() if _borderline(review)
    }
    pool = sorted(set(primary) - borderline)
    sample_size = min(len(pool), math.ceil(len(primary) * 0.20))
    sampled = (
        set(random.Random(seed).sample(pool, sample_size)) if sample_size else set()
    )
    selected = sorted(borderline | sampled)
    by_id = {item["review_id"]: item for item in items}
    selected_items = [by_id[review_id] for review_id in selected]
    evidence_ids = {
        ref["evidence_id"]
        for item in selected_items
        for ref in item.get("evidence_refs", [])
    }
    packet_value = {
        "schema": "leadpoet_second_sales_audit_v1",
        "rubric": packet.get("rubric"),
        "selection": {
            "borderline_count": len(borderline),
            "random_sample_count": len(sampled),
            "sample_fraction": 0.20,
        },
        "items": selected_items,
        "evidence": {
            evidence_id: packet["evidence"][evidence_id]
            for evidence_id in sorted(evidence_ids)
        },
    }
    template = {
        "schema": "leadpoet_second_sales_review_v1",
        "reviews": [_review_template(item) for item in selected_items],
    }
    paths = {
        "packet": output / "second_review_packet.json",
        "review": output / "second_review.json",
    }
    _write_new(paths["packet"], packet_value)
    _write_new(paths["review"], template)
    return paths


def _effective_review(item: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    scores = dict(review["scores"])
    flags = set(item.get("mechanical_flags") or [])
    claim_by_index = {
        claim["claim_index"]: claim for claim in item.get("intent_claims", [])
    }
    valid_claims = 0
    for check in review["intent_checks"]:
        claim = claim_by_index[check["claim_index"]]
        if not claim.get("mechanical_flags") and all(
            check[name] for name in CLAIM_CHECKS
        ):
            valid_claims += 1
    valid_fit_evidence = sum(
        bool(check["supports_fit"]) for check in review["fit_evidence_checks"]
    )
    if "duplicate_or_missing_company_domain" in flags:
        scores = {name: 0 for name in SCORE_NAMES}
    else:
        if flags & {"missing_fit_evidence_url", "no_live_fit_evidence"}:
            scores["icp_fit"] = 0
            scores["evidence_quality"] = 0
        elif valid_fit_evidence == 0:
            scores["icp_fit"] = 0
            scores["evidence_quality"] = 0
        if valid_claims == 0:
            scores["intent_validity"] = 0
            scores["evidence_quality"] = 0
        if review["severe_false_positive"]:
            scores["icp_fit"] = 0
            scores["intent_validity"] = 0
    sales_ready = (
        scores["icp_fit"] == 2
        and scores["intent_validity"] == 2
        and scores["why_now_quality"] >= 1
        and scores["evidence_quality"] >= 1
        and not review["severe_false_positive"]
    )
    return {
        **review,
        "effective_scores": scores,
        "valid_intent_claims": valid_claims,
        "valid_fit_evidence_urls": valid_fit_evidence,
        "sales_ready": sales_ready,
        "borderline": _borderline(review),
    }


def _major_disagreements(
    primary: dict[str, dict[str, Any]], secondary: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for review_id in sorted(secondary):
        differences = {
            name: abs(
                primary[review_id]["scores"][name]
                - secondary[review_id]["scores"][name]
            )
            for name in SCORE_NAMES
        }
        major = [name for name, difference in differences.items() if difference > 1]
        if major:
            rows.append(
                {
                    "review_id": review_id,
                    "dimensions": major,
                    "differences": differences,
                }
            )
    return rows


def _combined_notes(primary: Any, secondary: Any) -> str:
    parts = []
    if str(primary or "").strip():
        parts.append(f"Primary: {str(primary).strip()}")
    if str(secondary or "").strip():
        parts.append(f"Secondary: {str(secondary).strip()}")
    return "\n".join(parts)


def _conservative_review(
    primary: dict[str, Any], secondary: dict[str, Any]
) -> dict[str, Any]:
    """Merge two validated reviews without increasing either reviewer's credit."""

    if primary["review_id"] != secondary["review_id"]:
        raise ValueError("cannot merge reviews with different review IDs")
    secondary_fit = {
        row["evidence_id"]: row for row in secondary["fit_evidence_checks"]
    }
    secondary_intent = {row["claim_index"]: row for row in secondary["intent_checks"]}
    fit_checks = []
    for row in primary["fit_evidence_checks"]:
        other = secondary_fit.get(row["evidence_id"])
        if other is None:
            raise ValueError("second review has different fit evidence checks")
        fit_checks.append(
            {
                "evidence_id": row["evidence_id"],
                "supports_fit": bool(row["supports_fit"] and other["supports_fit"]),
                "notes": _combined_notes(row.get("notes"), other.get("notes")),
            }
        )
    if len(secondary_fit) != len(fit_checks):
        raise ValueError("second review has different fit evidence checks")

    intent_checks = []
    for row in primary["intent_checks"]:
        other = secondary_intent.get(row["claim_index"])
        if other is None:
            raise ValueError("second review has different intent checks")
        intent_checks.append(
            {
                "claim_index": row["claim_index"],
                **{name: bool(row[name] and other[name]) for name in CLAIM_CHECKS},
                "notes": _combined_notes(row.get("notes"), other.get("notes")),
            }
        )
    if len(secondary_intent) != len(intent_checks):
        raise ValueError("second review has different intent checks")

    return {
        "review_id": primary["review_id"],
        "scores": {
            name: min(primary["scores"][name], secondary["scores"][name])
            for name in SCORE_NAMES
        },
        "fit_evidence_checks": fit_checks,
        "intent_checks": intent_checks,
        "severe_false_positive": bool(
            primary["severe_false_positive"] or secondary["severe_false_positive"]
        ),
        "reviewer_borderline": bool(
            primary["reviewer_borderline"] or secondary["reviewer_borderline"]
        ),
        "notes": _combined_notes(primary.get("notes"), secondary.get("notes")),
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _validate_blind_key(packet: dict[str, Any], key: dict[str, Any]) -> None:
    """Require an ordinary one-to-one key for exactly this audit packet."""

    if packet.get("schema") != "leadpoet_blind_sales_audit_v1":
        raise ValueError("audit packet has an unsupported schema")
    if key.get("schema") != "leadpoet_blind_sales_audit_key_v1":
        raise ValueError("blind key has an unsupported schema")
    matrix = packet.get("matrix")
    packet_runs = packet.get("runs")
    arm_by_blind = key.get("arm_by_blind")
    key_runs = key.get("runs")
    if not isinstance(matrix, dict) or not isinstance(packet_runs, list):
        raise ValueError("audit packet is missing its matrix or runs")
    if not isinstance(arm_by_blind, dict) or not isinstance(key_runs, dict):
        raise ValueError("blind key is missing its arm or run mappings")

    expected_arms = matrix.get("arms")
    if (
        not isinstance(expected_arms, list)
        or not expected_arms
        or any(not isinstance(arm, str) or not arm for arm in expected_arms)
        or len(set(expected_arms)) != len(expected_arms)
    ):
        raise ValueError("audit packet matrix has invalid arms")
    blind_arms = {
        str(row.get("blind_arm") or "") for row in packet_runs if isinstance(row, dict)
    }
    if len(blind_arms) != len(expected_arms) or "" in blind_arms:
        raise ValueError("audit packet runs do not cover its matrix arms")
    if set(arm_by_blind) != blind_arms:
        raise ValueError("blind key arm names do not exactly match the audit packet")
    mapped_arms = list(arm_by_blind.values())
    if (
        any(not isinstance(arm, str) for arm in mapped_arms)
        or len(set(mapped_arms)) != len(mapped_arms)
        or set(mapped_arms) != set(expected_arms)
    ):
        raise ValueError("blind key is not a bijection over the matrix arms")

    packet_run_by_id: dict[str, dict[str, Any]] = {}
    for row in packet_runs:
        if not isinstance(row, dict):
            raise ValueError("audit packet contains an invalid run")
        run_id = str(row.get("blind_run") or "")
        if not run_id or run_id in packet_run_by_id:
            raise ValueError("audit packet contains a missing or duplicate run ID")
        packet_run_by_id[run_id] = row
    if set(key_runs) != set(packet_run_by_id):
        raise ValueError("blind key run IDs do not exactly match the audit packet")
    for run_id, packet_run in packet_run_by_id.items():
        key_run = key_runs[run_id]
        if not isinstance(key_run, dict):
            raise ValueError("blind key contains an invalid run mapping")
        blind_arm = str(packet_run.get("blind_arm") or "")
        expected_arm = arm_by_blind[blind_arm]
        if key_run.get("arm") != expected_arm:
            raise ValueError("blind key run arm disagrees with its blind arm")
        if key_run.get("blind_arm") != blind_arm:
            raise ValueError("blind key run blind arm disagrees with the packet")
        if key_run.get("icp_id") != packet_run.get("icp_id"):
            raise ValueError("blind key run ICP disagrees with the packet")


def _aggregate(
    packet: dict[str, Any], key: dict[str, Any], reviews: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _validate_blind_key(packet, key)
    item_by_id = {item["review_id"]: item for item in packet["items"]}
    effective = {
        review_id: _effective_review(item_by_id[review_id], review)
        for review_id, review in reviews.items()
    }
    arm_by_blind = key.get("arm_by_blind") or {}
    arm_names = sorted(set(arm_by_blind.values()))
    metrics: list[dict[str, Any]] = []
    scored_items: list[dict[str, Any]] = []
    for arm in arm_names:
        blind = next(name for name, value in arm_by_blind.items() if value == arm)
        runs = [row for row in packet["runs"] if row["blind_arm"] == blind]
        review_ids = [review_id for run in runs for review_id in run["review_ids"]]
        arm_reviews = [effective[review_id] for review_id in review_ids]
        ready_ids = [
            review_id for review_id in review_ids if effective[review_id]["sales_ready"]
        ]
        for review_id in review_ids:
            scored_items.append(
                {
                    "review_id": review_id,
                    "arm": arm,
                    "icp_id": item_by_id[review_id]["icp_id"],
                    "company_name": item_by_id[review_id]["company"].get(
                        "company_name"
                    ),
                    "effective_scores": effective[review_id]["effective_scores"],
                    "sales_ready": effective[review_id]["sales_ready"],
                    "borderline": effective[review_id]["borderline"],
                    "mechanical_flags": item_by_id[review_id].get("mechanical_flags")
                    or [],
                }
            )

        by_icp: dict[str, set[str]] = {}
        for review_id in ready_ids:
            item = item_by_id[review_id]
            by_icp.setdefault(item["icp_id"], set()).add(
                _company_domain(item["company"])
            )
        supporting_count = 0
        valid_supporting_count = 0
        for review_id in review_ids:
            item = item_by_id[review_id]
            review = effective[review_id]
            checks = {row["claim_index"]: row for row in review["intent_checks"]}
            fit_checks = {
                row["evidence_id"]: row for row in review["fit_evidence_checks"]
            }
            for ref in item.get("evidence_refs", []):
                if ref["role"] not in {"fit", "intent"}:
                    continue
                supporting_count += 1
                opened = bool(packet["evidence"][ref["evidence_id"]]["opened"])
                if ref["role"] == "fit":
                    valid = opened and bool(
                        fit_checks.get(ref["evidence_id"], {}).get("supports_fit")
                    )
                else:
                    check = checks.get(ref.get("claim_index"), {})
                    valid = opened and all(
                        bool(check.get(name))
                        for name in ("correct_company", "supports_claim", "fresh")
                    )
                valid_supporting_count += int(valid)
        run_keys = [key["runs"][run["blind_run"]] for run in runs]
        costs = [_number(row.get("estimated_combined_cost_usd")) for row in run_keys]
        latencies = [_number(row.get("latency_seconds")) for row in run_keys]
        provider_calls = [_number(row.get("provider_call_count")) for row in run_keys]
        costs = [value for value in costs if value is not None]
        latencies = [value for value in latencies if value is not None]
        provider_calls = [value for value in provider_calls if value is not None]
        cost_complete = len(costs) == len(runs)
        total_cost = sum(costs) if cost_complete else None
        denominator = EXPECTED_COMPANIES * len(runs)
        completed_attempts = sum(bool(run["completed"]) for run in runs)
        severe_false_positives = sum(
            bool(review["severe_false_positive"]) for review in arm_reviews
        )
        metrics.append(
            {
                "arm": arm,
                "selection_eligible": arm in CHALLENGER_ARMS,
                "attempts": len(runs),
                "completed_attempts": completed_attempts,
                "run_completion_rate": (
                    round(completed_attempts / len(runs), 4) if runs else 0
                ),
                "returned_companies": len(review_ids),
                "sales_ready_companies": len(ready_ids),
                "sales_ready_at_5_average": (
                    round(len(ready_ids) / len(runs), 4) if runs else 0
                ),
                "sales_ready_at_5_rate": (
                    round(len(ready_ids) / denominator, 4) if denominator else 0
                ),
                "unique_sales_ready_by_icp": {
                    name: len(domains) for name, domains in sorted(by_icp.items())
                },
                "valid_supporting_url_rate": (
                    round(valid_supporting_count / supporting_count, 4)
                    if supporting_count
                    else 0
                ),
                "severe_false_positive_rate": (
                    round(severe_false_positives / len(arm_reviews), 4)
                    if arm_reviews
                    else 0
                ),
                "cost_complete": cost_complete,
                "total_cost_usd": (
                    round(total_cost, 6) if total_cost is not None else None
                ),
                "cost_per_sales_ready_company_usd": (
                    round(total_cost / len(ready_ids), 6)
                    if total_cost is not None and ready_ids
                    else None
                ),
                "median_latency_seconds": (
                    round(statistics.median(latencies), 3) if latencies else None
                ),
                "median_provider_calls": (
                    round(statistics.median(provider_calls), 2)
                    if provider_calls
                    else None
                ),
                "components": {
                    "attempts": len(runs),
                    "completed_attempts": completed_attempts,
                    "returned_companies": len(review_ids),
                    "reviewed_companies": len(arm_reviews),
                    "sales_ready_companies": len(ready_ids),
                    "supporting_url_count": supporting_count,
                    "valid_supporting_url_count": valid_supporting_count,
                    "severe_false_positive_count": severe_false_positives,
                    "unique_sales_ready_by_icp": {
                        name: len(domains) for name, domains in sorted(by_icp.items())
                    },
                    "total_cost_usd": round(sum(costs), 12),
                    "missing_cost_attempts": len(runs) - len(costs),
                    "latency_seconds": latencies,
                    "provider_calls": provider_calls,
                },
            }
        )
    metrics.sort(
        key=lambda row: (
            -row["sales_ready_at_5_rate"],
            row["severe_false_positive_rate"],
            (
                row["cost_per_sales_ready_company_usd"]
                if row["cost_per_sales_ready_company_usd"] is not None
                else float("inf")
            ),
            (
                row["median_latency_seconds"]
                if row["median_latency_seconds"] is not None
                else float("inf")
            ),
            row["arm"],
        )
    )
    return metrics, scored_items


def _selection_order(metrics: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank only eligible challenger bundles for winner selection."""

    challengers = [
        row
        for row in metrics
        if row.get("arm") in CHALLENGER_ARMS and row.get("selection_eligible", True)
    ]
    challengers.sort(
        key=lambda row: (
            -float(row["sales_ready_at_5_rate"]),
            (
                row["cost_per_sales_ready_company_usd"]
                if row.get("cost_per_sales_ready_company_usd") is not None
                else float("inf")
            ),
            (
                row["median_latency_seconds"]
                if row.get("median_latency_seconds") is not None
                else float("inf")
            ),
            str(row["arm"]),
        )
    )
    return challengers


def build_report(
    *,
    packet_path: Path,
    key_path: Path,
    primary_path: Path,
    secondary_path: Path | None,
    adjudication_path: Path | None,
    output: Path,
    seed: int,
) -> Path:
    output = _outside_repository(output)
    packet = _read_json(packet_path)
    key = _read_json(key_path)
    items = packet.get("items")
    if not isinstance(items, list):
        raise ValueError("audit packet is missing items")
    primary = _validated_reviews(_read_json(primary_path), items, require_all=True)

    borderline = {
        review_id for review_id, review in primary.items() if _borderline(review)
    }
    pool = sorted(set(primary) - borderline)
    sample_size = min(len(pool), math.ceil(len(primary) * 0.20))
    sampled = (
        set(random.Random(seed).sample(pool, sample_size)) if sample_size else set()
    )
    expected_secondary = borderline | sampled
    secondary: dict[str, dict[str, Any]] = {}
    if secondary_path is not None:
        selected_items = [
            item for item in items if item["review_id"] in expected_secondary
        ]
        secondary = _validated_reviews(
            _read_json(secondary_path), selected_items, require_all=True
        )
        if set(secondary) != expected_secondary:
            raise ValueError(
                "second review does not match the deterministic review selection"
            )
    major = _major_disagreements(primary, secondary)
    unresolved = {row["review_id"] for row in major}
    final_reviews = dict(primary)
    for review_id, secondary_review in secondary.items():
        final_reviews[review_id] = _conservative_review(
            primary[review_id], secondary_review
        )
    if adjudication_path is not None:
        adjudication_items = [item for item in items if item["review_id"] in unresolved]
        adjudicated = _validated_reviews(
            _read_json(adjudication_path), adjudication_items, require_all=True
        )
        final_reviews.update(adjudicated)
        unresolved -= set(adjudicated)

    metrics, scored_items = _aggregate(packet, key, final_reviews)
    challenger_metrics = _selection_order(metrics)
    if not challenger_metrics:
        raise ValueError("report does not contain a recognized challenger arm")
    top_two = challenger_metrics[:2]
    complete_second_review = (
        not expected_secondary or set(secondary) == expected_secondary
    )
    review_complete = complete_second_review and not unresolved
    tiebreak_required = (
        review_complete
        and len(top_two) == 2
        and abs(
            top_two[0]["sales_ready_at_5_rate"] - top_two[1]["sales_ready_at_5_rate"]
        )
        <= NEAR_TIE_RATE
    )
    final = review_complete and not tiebreak_required
    matrix = packet.get("matrix")
    if not isinstance(matrix, dict):
        raise ValueError("audit packet is missing its matrix")
    report = {
        "schema": REPORT_SCHEMA,
        "evaluation_date": packet.get("evaluation_date"),
        "matrix": matrix,
        "review_complete": review_complete,
        "final": final,
        "primary_metric": "sales_ready_at_5_average",
        "winner": challenger_metrics[0]["arm"] if final else None,
        "tiebreak_required": tiebreak_required,
        "tiebreak_arms": [row["arm"] for row in top_two] if tiebreak_required else [],
        "second_review": {
            "required_count": len(expected_secondary),
            "completed_count": len(secondary),
            "borderline_count": len(borderline),
            "random_sample_count": len(sampled),
            "major_disagreements": major,
            "unresolved_adjudications": sorted(unresolved),
        },
        "leaderboard": metrics,
        "challenger_leaderboard": challenger_metrics,
        "scored_items": scored_items,
    }
    _write_new(output, report)
    return output


def _matrix_spec(
    report: dict[str, Any], label: str
) -> tuple[tuple[str, ...], tuple[str, ...], int]:
    if report.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"{label} report has an unsupported schema")
    if report.get("review_complete") is not True:
        raise ValueError(f"{label} report has incomplete independent review")
    matrix = report.get("matrix")
    if not isinstance(matrix, dict):
        raise ValueError(f"{label} report is missing its matrix")
    arms = matrix.get("arms")
    icp_ids = matrix.get("icp_ids")
    repetitions = matrix.get("repetitions")
    attempts = matrix.get("attempts")
    if (
        not isinstance(arms, list)
        or not arms
        or any(not isinstance(arm, str) or not arm for arm in arms)
        or len(set(arms)) != len(arms)
    ):
        raise ValueError(f"{label} report has invalid matrix arms")
    if (
        not isinstance(icp_ids, list)
        or not icp_ids
        or any(not isinstance(icp_id, str) or not icp_id for icp_id in icp_ids)
        or len(set(icp_ids)) != len(icp_ids)
    ):
        raise ValueError(f"{label} report has invalid matrix ICP IDs")
    if (
        isinstance(repetitions, bool)
        or not isinstance(repetitions, int)
        or repetitions < 1
    ):
        raise ValueError(f"{label} report has invalid repetitions")
    expected_attempts = len(arms) * len(icp_ids) * repetitions
    if attempts != expected_attempts:
        raise ValueError(
            f"{label} report matrix must contain exactly {expected_attempts} attempts"
        )
    if not isinstance(matrix.get("model"), str) or not matrix["model"]:
        raise ValueError(f"{label} report is missing its model")
    if (
        not isinstance(report.get("evaluation_date"), str)
        or not report["evaluation_date"]
    ):
        raise ValueError(f"{label} report is missing its evaluation date")
    return tuple(arms), tuple(icp_ids), repetitions


def _component_integer(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} exceeds its maximum")
    return value


def _report_components(report: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    arms, icp_ids, repetitions = _matrix_spec(report, label)
    leaderboard = report.get("leaderboard")
    if not isinstance(leaderboard, list):
        raise ValueError(f"{label} report is missing its leaderboard")
    expected_attempts = len(icp_ids) * repetitions
    parsed: dict[str, dict[str, Any]] = {}
    for metric in leaderboard:
        if not isinstance(metric, dict):
            raise ValueError(f"{label} report contains an invalid leaderboard row")
        arm = str(metric.get("arm") or "")
        if arm not in arms or arm in parsed:
            raise ValueError(f"{label} report contains an unknown or duplicate arm")
        component = metric.get("components")
        if not isinstance(component, dict):
            raise ValueError(
                f"{label} report is missing aggregation components for {arm}"
            )
        attempts = _component_integer(
            component.get("attempts"), f"{label}.{arm}.attempts"
        )
        if attempts != expected_attempts:
            raise ValueError(
                f"{label} report must contain {expected_attempts} attempts for {arm}"
            )
        completed = _component_integer(
            component.get("completed_attempts"),
            f"{label}.{arm}.completed_attempts",
            maximum=attempts,
        )
        returned = _component_integer(
            component.get("returned_companies"),
            f"{label}.{arm}.returned_companies",
            maximum=EXPECTED_COMPANIES * attempts,
        )
        reviewed = _component_integer(
            component.get("reviewed_companies"),
            f"{label}.{arm}.reviewed_companies",
            maximum=returned,
        )
        if reviewed != returned:
            raise ValueError(
                f"{label} report did not review every returned company for {arm}"
            )
        sales_ready = _component_integer(
            component.get("sales_ready_companies"),
            f"{label}.{arm}.sales_ready_companies",
            maximum=reviewed,
        )
        supporting = _component_integer(
            component.get("supporting_url_count"),
            f"{label}.{arm}.supporting_url_count",
        )
        valid_supporting = _component_integer(
            component.get("valid_supporting_url_count"),
            f"{label}.{arm}.valid_supporting_url_count",
            maximum=supporting,
        )
        severe = _component_integer(
            component.get("severe_false_positive_count"),
            f"{label}.{arm}.severe_false_positive_count",
            maximum=reviewed,
        )
        missing_costs = _component_integer(
            component.get("missing_cost_attempts"),
            f"{label}.{arm}.missing_cost_attempts",
            maximum=attempts,
        )
        total_cost = _number(component.get("total_cost_usd"))
        if total_cost is None or total_cost < 0:
            raise ValueError(f"{label}.{arm}.total_cost_usd must be nonnegative")
        latencies = component.get("latency_seconds")
        if not isinstance(latencies, list) or len(latencies) != attempts:
            raise ValueError(f"{label} report needs every latency sample for {arm}")
        parsed_latencies = [_number(value) for value in latencies]
        if any(value is None or value < 0 for value in parsed_latencies):
            raise ValueError(f"{label} report has an invalid latency sample for {arm}")
        provider_calls = component.get("provider_calls")
        if not isinstance(provider_calls, list):
            raise ValueError(
                f"{label} report has invalid provider-call samples for {arm}"
            )
        parsed_provider_calls = [_number(value) for value in provider_calls]
        if any(value is None or value < 0 for value in parsed_provider_calls):
            raise ValueError(
                f"{label} report has an invalid provider-call sample for {arm}"
            )
        unique = component.get("unique_sales_ready_by_icp")
        if not isinstance(unique, dict) or not set(unique).issubset(set(icp_ids)):
            raise ValueError(
                f"{label} report has invalid unique-company ICP counts for {arm}"
            )
        parsed_unique = {
            str(icp_id): _component_integer(
                count, f"{label}.{arm}.unique_sales_ready_by_icp.{icp_id}"
            )
            for icp_id, count in unique.items()
        }
        parsed[arm] = {
            "attempts": attempts,
            "completed_attempts": completed,
            "returned_companies": returned,
            "reviewed_companies": reviewed,
            "sales_ready_companies": sales_ready,
            "supporting_url_count": supporting,
            "valid_supporting_url_count": valid_supporting,
            "severe_false_positive_count": severe,
            "unique_sales_ready_by_icp": parsed_unique,
            "total_cost_usd": total_cost,
            "missing_cost_attempts": missing_costs,
            "latency_seconds": [float(value) for value in parsed_latencies],
            "provider_calls": [float(value) for value in parsed_provider_calls],
        }
    if set(parsed) != set(arms):
        raise ValueError(f"{label} report leaderboard does not match its matrix arms")
    return parsed


def _combined_metric(
    arm: str,
    initial: dict[str, Any],
    extension: dict[str, Any],
) -> dict[str, Any]:
    attempts = initial["attempts"] + extension["attempts"]
    completed = initial["completed_attempts"] + extension["completed_attempts"]
    returned = initial["returned_companies"] + extension["returned_companies"]
    reviewed = initial["reviewed_companies"] + extension["reviewed_companies"]
    ready = initial["sales_ready_companies"] + extension["sales_ready_companies"]
    supporting = initial["supporting_url_count"] + extension["supporting_url_count"]
    valid_supporting = (
        initial["valid_supporting_url_count"] + extension["valid_supporting_url_count"]
    )
    severe = (
        initial["severe_false_positive_count"]
        + extension["severe_false_positive_count"]
    )
    missing_costs = (
        initial["missing_cost_attempts"] + extension["missing_cost_attempts"]
    )
    total_cost_value = initial["total_cost_usd"] + extension["total_cost_usd"]
    total_cost = total_cost_value if missing_costs == 0 else None
    latencies = initial["latency_seconds"] + extension["latency_seconds"]
    provider_calls = initial["provider_calls"] + extension["provider_calls"]
    unique = {
        **initial["unique_sales_ready_by_icp"],
        **extension["unique_sales_ready_by_icp"],
    }
    denominator = EXPECTED_COMPANIES * attempts
    return {
        "arm": arm,
        "selection_eligible": True,
        "attempts": attempts,
        "initial_attempts": initial["attempts"],
        "extension_attempts": extension["attempts"],
        "completed_attempts": completed,
        "run_completion_rate": round(completed / attempts, 4) if attempts else 0,
        "returned_companies": returned,
        "sales_ready_companies": ready,
        "sales_ready_at_5_average": round(ready / attempts, 4) if attempts else 0,
        "sales_ready_at_5_rate": round(ready / denominator, 4) if denominator else 0,
        "unique_sales_ready_by_icp": dict(sorted(unique.items())),
        "valid_supporting_url_rate": (
            round(valid_supporting / supporting, 4) if supporting else 0
        ),
        "severe_false_positive_rate": round(severe / reviewed, 4) if reviewed else 0,
        "cost_complete": missing_costs == 0,
        "total_cost_usd": round(total_cost, 6) if total_cost is not None else None,
        "cost_per_sales_ready_company_usd": (
            round(total_cost / ready, 6) if total_cost is not None and ready else None
        ),
        "median_latency_seconds": (
            round(statistics.median(latencies), 3) if latencies else None
        ),
        "median_provider_calls": (
            round(statistics.median(provider_calls), 2) if provider_calls else None
        ),
        "components": {
            "attempts": attempts,
            "completed_attempts": completed,
            "returned_companies": returned,
            "reviewed_companies": reviewed,
            "sales_ready_companies": ready,
            "supporting_url_count": supporting,
            "valid_supporting_url_count": valid_supporting,
            "severe_false_positive_count": severe,
            "unique_sales_ready_by_icp": dict(sorted(unique.items())),
            "total_cost_usd": round(total_cost_value, 12),
            "missing_cost_attempts": missing_costs,
            "latency_seconds": latencies,
            "provider_calls": provider_calls,
        },
    }


def _selection_row_from_components(
    arm: str, component: dict[str, Any]
) -> dict[str, Any]:
    """Build unrounded selection values from validated aggregation components."""

    attempts = component["attempts"]
    ready = component["sales_ready_companies"]
    denominator = EXPECTED_COMPANIES * attempts
    cost: float | None = None
    if component["missing_cost_attempts"] == 0:
        cost = component["total_cost_usd"] / ready if ready else float("inf")
    latencies = component["latency_seconds"]
    return {
        "arm": arm,
        "selection_eligible": arm in CHALLENGER_ARMS,
        "sales_ready_at_5_rate": ready / denominator if denominator else 0.0,
        "cost_per_sales_ready_company_usd": cost,
        "median_latency_seconds": statistics.median(latencies) if latencies else None,
    }


def _combined_winner(metrics: list[dict[str, Any]]) -> tuple[str | None, str, float]:
    if len(metrics) != 2:
        raise ValueError("combined selection requires exactly two challenger metrics")
    exact = [
        _selection_row_from_components(row["arm"], row["components"]) for row in metrics
    ]
    quality_order = sorted(
        exact, key=lambda row: (-row["sales_ready_at_5_rate"], row["arm"])
    )
    quality_gap = abs(
        quality_order[0]["sales_ready_at_5_rate"]
        - quality_order[1]["sales_ready_at_5_rate"]
    )
    if quality_gap > NEAR_TIE_RATE:
        return quality_order[0]["arm"], "combined_quality", quality_gap

    cost_order = sorted(
        exact,
        key=lambda row: (
            (
                row["cost_per_sales_ready_company_usd"]
                if row["cost_per_sales_ready_company_usd"] is not None
                else float("inf")
            ),
            row["arm"],
        ),
    )
    first_cost = cost_order[0]["cost_per_sales_ready_company_usd"]
    second_cost = cost_order[1]["cost_per_sales_ready_company_usd"]
    if (
        first_cost is not None
        and second_cost is not None
        and not math.isclose(first_cost, second_cost, rel_tol=1e-12, abs_tol=1e-12)
    ):
        return (
            cost_order[0]["arm"],
            "combined_cost_per_sales_ready_company",
            quality_gap,
        )

    latency_order = sorted(
        exact,
        key=lambda row: (
            (
                row["median_latency_seconds"]
                if row["median_latency_seconds"] is not None
                else float("inf")
            ),
            row["arm"],
        ),
    )
    first_latency = latency_order[0]["median_latency_seconds"]
    second_latency = latency_order[1]["median_latency_seconds"]
    if (
        first_latency is not None
        and second_latency is not None
        and not math.isclose(
            first_latency, second_latency, rel_tol=1e-12, abs_tol=1e-12
        )
    ):
        return latency_order[0]["arm"], "combined_median_latency", quality_gap
    return None, "unresolved_after_quality_cost_latency", quality_gap


def combine_reports(*, initial_path: Path, extension_path: Path, output: Path) -> Path:
    """Combine one reviewed 50-attempt report with its reviewed 12-attempt extension."""

    output = _outside_repository(output)
    initial = _read_json(initial_path)
    extension = _read_json(extension_path)
    initial_arms, initial_icps, initial_repetitions = _matrix_spec(initial, "initial")
    extension_arms, extension_icps, extension_repetitions = _matrix_spec(
        extension, "extension"
    )
    if (
        set(initial_arms) != set(ARMS)
        or len(initial_arms) != 5
        or len(initial_icps) != 5
        or initial_repetitions != 2
    ):
        raise ValueError(
            "initial report must be the complete five-arm, five-ICP, two-repetition matrix"
        )
    if initial.get("tiebreak_required") is not True:
        raise ValueError("initial report does not require a tiebreak extension")
    finalists = initial.get("tiebreak_arms")
    if (
        not isinstance(finalists, list)
        or len(finalists) != 2
        or len(set(finalists)) != 2
        or any(arm not in CHALLENGER_ARMS for arm in finalists)
    ):
        raise ValueError("initial report must name exactly two challenger finalists")
    if (
        set(extension_arms) != set(finalists)
        or len(extension_icps) != 3
        or extension_repetitions != 2
    ):
        raise ValueError(
            "extension report must contain the two finalists, three ICPs, and two repetitions"
        )
    if set(initial_icps) & set(extension_icps):
        raise ValueError("extension ICPs must be unused by the initial report")
    if initial.get("evaluation_date") != extension.get("evaluation_date"):
        raise ValueError("initial and extension reports use different evaluation dates")
    if initial["matrix"].get("model") != extension["matrix"].get("model"):
        raise ValueError("initial and extension reports use different models")

    initial_components = _report_components(initial, "initial")
    extension_components = _report_components(extension, "extension")
    initial_order = _selection_order(
        _selection_row_from_components(arm, component)
        for arm, component in initial_components.items()
    )
    if [row["arm"] for row in initial_order[:2]] != finalists:
        raise ValueError(
            "initial report finalists do not match its challenger leaderboard"
        )
    initial_gap = abs(
        initial_order[0]["sales_ready_at_5_rate"]
        - initial_order[1]["sales_ready_at_5_rate"]
    )
    if initial_gap > NEAR_TIE_RATE:
        raise ValueError(
            "initial report finalists are not within five percentage points"
        )
    combined = [
        _combined_metric(
            arm,
            initial_components[arm],
            extension_components[arm],
        )
        for arm in finalists
    ]
    combined.sort(key=lambda row: (-row["sales_ready_at_5_rate"], row["arm"]))
    winner, selection_basis, quality_gap = _combined_winner(combined)

    scored_items = []
    for stage, report in (("initial", initial), ("extension", extension)):
        rows = report.get("scored_items")
        if not isinstance(rows, list):
            raise ValueError(f"{stage} report is missing scored items")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{stage} report has an invalid scored item")
            source_id = str(row.get("review_id") or "")
            scored_items.append(
                {
                    **row,
                    "review_id": f"{stage}:{source_id}",
                    "source_review_id": source_id,
                    "stage": stage,
                }
            )

    report = {
        "schema": COMBINED_REPORT_SCHEMA,
        "evaluation_date": initial["evaluation_date"],
        "model": initial["matrix"]["model"],
        "final": winner is not None,
        "winner": winner,
        "selection_basis": selection_basis,
        "primary_metric": "combined_sales_ready_at_5_average",
        "quality_near_tie_threshold": NEAR_TIE_RATE,
        "combined_quality_rate_gap": round(quality_gap, 4),
        "tiebreak_arms": finalists,
        "matrix": {
            "initial_attempts": len(initial_arms)
            * len(initial_icps)
            * initial_repetitions,
            "extension_attempts": len(extension_arms)
            * len(extension_icps)
            * extension_repetitions,
            "combined_attempts_per_finalist": sum(row["attempts"] for row in combined)
            // len(combined),
            "initial_icp_ids": list(initial_icps),
            "extension_icp_ids": list(extension_icps),
        },
        "leaderboard": combined,
        "initial_leaderboard": initial.get("leaderboard"),
        "extension_leaderboard": extension.get("leaderboard"),
        "scored_items": scored_items,
    }
    _write_new(output, report)
    return output


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    packet = commands.add_parser(
        "packet", help="blind results and open each distinct evidence URL live"
    )
    packet.add_argument("--results", type=Path, required=True)
    packet.add_argument("--output", type=Path, default=DEFAULT_ROOT)
    packet.add_argument("--evaluation-date", type=_day, default=date.today())
    packet.add_argument("--seed", type=int, default=20260902)
    packet.add_argument("--workers", type=int, default=8)
    packet.add_argument("--url-timeout", type=float, default=20.0)
    packet.add_argument("--max-evidence-chars", type=int, default=12000)
    packet.add_argument("--expected-arm", action="append", dest="expected_arms")
    packet.add_argument(
        "--expected-icp-id",
        action="append",
        dest="expected_icp_ids",
        required=True,
        help="expected ICP ID; repeat once for each complete matrix ICP",
    )
    packet.add_argument("--repetitions", type=int, default=2)

    second = commands.add_parser(
        "second-review", help="select all borderlines plus a deterministic 20%% sample"
    )
    second.add_argument("--packet", type=Path, required=True)
    second.add_argument("--primary", type=Path, required=True)
    second.add_argument("--output", type=Path, default=DEFAULT_ROOT / "second")
    second.add_argument("--seed", type=int, default=20260902)

    report = commands.add_parser(
        "report", help="validate reviews and aggregate the final sales-quality report"
    )
    report.add_argument("--packet", type=Path, required=True)
    report.add_argument("--key", type=Path, required=True)
    report.add_argument("--primary", type=Path, required=True)
    report.add_argument("--secondary", type=Path)
    report.add_argument("--adjudication", type=Path)
    report.add_argument("--output", type=Path, default=DEFAULT_ROOT / "report.json")
    report.add_argument("--seed", type=int, default=20260902)

    combine = commands.add_parser(
        "combine",
        help="combine the reviewed initial report with its reviewed tiebreak extension",
    )
    combine.add_argument("--initial-report", type=Path, required=True)
    combine.add_argument("--extension-report", type=Path, required=True)
    combine.add_argument(
        "--output", type=Path, default=DEFAULT_ROOT / "combined_report.json"
    )

    args = parser.parse_args(argv)
    if args.command == "packet":
        if args.workers < 1 or args.workers > 32:
            parser.error("--workers must be from 1 through 32")
        if args.url_timeout <= 0 or args.max_evidence_chars < 1000:
            parser.error(
                "URL timeout must be positive and evidence text must be at least 1000 characters"
            )
        if args.repetitions < 1:
            parser.error("--repetitions must be at least 1")
        paths = build_packet(
            results_path=args.results,
            output=args.output,
            evaluation_date=args.evaluation_date,
            seed=args.seed,
            workers=args.workers,
            timeout=args.url_timeout,
            max_chars=args.max_evidence_chars,
            expected_arms=tuple(args.expected_arms or ARMS),
            expected_icp_ids=tuple(args.expected_icp_ids),
            repetitions=args.repetitions,
        )
    elif args.command == "second-review":
        paths = second_review_packet(
            packet_path=args.packet,
            primary_path=args.primary,
            output=args.output,
            seed=args.seed,
        )
    elif args.command == "report":
        path = build_report(
            packet_path=args.packet,
            key_path=args.key,
            primary_path=args.primary,
            secondary_path=args.secondary,
            adjudication_path=args.adjudication,
            output=args.output,
            seed=args.seed,
        )
        paths = {"report": path}
    else:
        path = combine_reports(
            initial_path=args.initial_report,
            extension_path=args.extension_report,
            output=args.output,
        )
        paths = {"combined_report": path}
    print(json.dumps({name: str(path) for name, path in paths.items()}, sort_keys=True))
    if args.command in {"report", "combine"} and not bool(
        _read_json(next(iter(paths.values()))).get("final")
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
