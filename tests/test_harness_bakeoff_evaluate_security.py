from __future__ import annotations

import os
from datetime import date
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments.harness_bakeoff import evaluate


class EvidenceFetchTests(unittest.TestCase):
    def test_default_user_agent_recovers_a_named_crawler_rejection(self) -> None:
        observed: list[str | None] = []

        class Client:
            def __init__(self, *, headers, **_kwargs):
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def get(self, _url):
                user_agent = self.headers.get("User-Agent")
                observed.append(user_agent)
                if user_agent:
                    return SimpleNamespace(status_code=403, headers={}, text="blocked")
                return SimpleNamespace(
                    status_code=200,
                    headers={},
                    text="<html><title>Evidence</title><body>Supported fact</body></html>",
                )

        with patch.object(evaluate, "_assert_public_url", side_effect=lambda url: url):
            with patch.object(evaluate.httpx, "Client", Client):
                result = evaluate._fetch_evidence(
                    "https://evidence.example/event",
                    timeout=1.0,
                    max_chars=2_000,
                )

        self.assertEqual(observed, ["LeadpoetBlindAudit/1.0", None])
        self.assertTrue(result["opened"])
        self.assertEqual(result["status_code"], 200)
        self.assertIn("Supported fact", result["text"])


class EvaluateOutputSecurityTests(unittest.TestCase):
    def test_private_json_is_mode_0600_before_first_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bakeoff-evaluate-test-") as raw:
            output = Path(raw).resolve() / "audit" / "report.json"
            observed_modes: list[int] = []
            original_write = os.write

            def observe_write(descriptor: int, data: bytes) -> int:
                observed_modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
                return original_write(descriptor, data)

            previous_umask = os.umask(0)
            try:
                with patch.object(os, "write", observe_write):
                    evaluate._write_new(output, {"private": "value"})
            finally:
                os.umask(previous_umask)

            self.assertEqual(observed_modes, [0o600])
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_private_json_refuses_existing_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bakeoff-evaluate-test-") as raw:
            output = Path(raw).resolve() / "audit" / "report.json"
            evaluate._write_new(output, {"first": True})

            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
                evaluate._write_new(output, {"second": True})

            self.assertIn('"first": true', output.read_text(encoding="utf-8"))

    def test_private_json_removes_partial_file_after_write_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bakeoff-evaluate-test-") as raw:
            output = Path(raw).resolve() / "audit" / "report.json"

            with patch.object(os, "write", side_effect=OSError("synthetic failure")):
                with self.assertRaisesRegex(OSError, "synthetic failure"):
                    evaluate._write_new(output, {"private": True})

            self.assertFalse(output.exists())

    def test_private_json_rejects_symlink_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bakeoff-evaluate-test-") as raw:
            root = Path(raw).resolve()
            output = root / "audit" / "report.json"
            target = root / "target.json"
            output.parent.mkdir()
            output.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "symbolic links"):
                evaluate._write_new(output, {"private": True})

            self.assertFalse(target.exists())

    def test_private_json_rejects_symlink_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bakeoff-evaluate-test-") as raw:
            root = Path(raw).resolve()
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symbolic links"):
                evaluate._write_new(linked / "report.json", {"private": True})

            self.assertEqual(list(real.iterdir()), [])

    def test_case_alias_of_repository_is_rejected_when_supported(self) -> None:
        repository = Path(evaluate.__file__).resolve().parents[2]
        alias = repository.with_name(repository.name.swapcase())
        try:
            same_repository = alias.samefile(repository)
        except FileNotFoundError:
            self.skipTest("filesystem is case-sensitive")
        if not same_repository:
            self.skipTest("filesystem is case-sensitive")

        with self.assertRaisesRegex(ValueError, "outside the repository"):
            evaluate._outside_repository(alias / "private-audit.json")


class BlindKeyValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.packet = {
            "schema": "leadpoet_blind_sales_audit_v1",
            "matrix": {"arms": ["pydantic_ai", "pi"]},
            "runs": [
                {"blind_run": "run_0001", "blind_arm": "bundle_01", "icp_id": "icp_a"},
                {"blind_run": "run_0002", "blind_arm": "bundle_02", "icp_id": "icp_a"},
            ],
        }
        self.key = {
            "schema": "leadpoet_blind_sales_audit_key_v1",
            "arm_by_blind": {"bundle_01": "pydantic_ai", "bundle_02": "pi"},
            "runs": {
                "run_0001": {
                    "arm": "pydantic_ai",
                    "blind_arm": "bundle_01",
                    "icp_id": "icp_a",
                },
                "run_0002": {
                    "arm": "pi",
                    "blind_arm": "bundle_02",
                    "icp_id": "icp_a",
                },
            },
        }

    def test_matching_key_is_accepted(self) -> None:
        evaluate._validate_blind_key(self.packet, self.key)

    def test_arm_mapping_must_be_a_bijection(self) -> None:
        self.key["arm_by_blind"]["bundle_02"] = "pydantic_ai"
        with self.assertRaisesRegex(ValueError, "bijection"):
            evaluate._validate_blind_key(self.packet, self.key)

    def test_key_run_must_match_packet_relationships(self) -> None:
        self.key["runs"]["run_0002"]["icp_id"] = "icp_b"
        with self.assertRaisesRegex(ValueError, "ICP disagrees"):
            evaluate._validate_blind_key(self.packet, self.key)


class MatrixValidationTests(unittest.TestCase):
    def test_company_domain_collapses_www_and_subdomain_aliases(self) -> None:
        aliases = (
            "https://example.co.uk/",
            "https://www.example.co.uk/about",
            "https://news.example.co.uk/launch",
        )

        with patch.object(evaluate, "_get_sld", None):
            self.assertEqual(
                {
                    evaluate._company_domain({"company_website": value})
                    for value in aliases
                },
                {"example.co.uk"},
            )
        self.assertNotEqual(
            evaluate._company_domain(
                {"company_website": "https://different.example.co.uk"}
            ),
            evaluate._company_domain(
                {"company_website": "https://different-company.co.uk"}
            ),
        )

    def test_truthy_non_boolean_ok_is_rejected(self) -> None:
        row = {
            "phase": "scored",
            "evaluation_date": "2026-09-02",
            "model": "model",
            "arm": "pi",
            "icp_id": "icp_a",
            "repetition": 1,
            "order": 1,
            "block_id": "icp_a:r1",
            "input": {"icp_id": "icp_a"},
            "ok": 1,
            "eligible_for_scoring": True,
            "companies": [],
            "company_count": 0,
            "max_companies": 5,
        }
        with self.assertRaisesRegex(ValueError, "invalid completion or eligibility"):
            evaluate._validate_matrix(
                [row],
                expected_arms=("pi",),
                expected_icp_ids=("icp_a",),
                repetitions=1,
                evaluation_date=date(2026, 9, 2),
            )


class CliContractTests(unittest.TestCase):
    def test_packet_requires_explicit_expected_icp_ids(self) -> None:
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit) as raised:
                evaluate.main(["packet", "--results", "unused.jsonl"])

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
