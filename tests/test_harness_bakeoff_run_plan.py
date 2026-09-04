from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.harness_bakeoff.icp_loader import external_icp_file, load_icps
from experiments.harness_bakeoff.runner import (
    ARMS,
    _planned_attempts,
    _run_plan_icp_ids,
    main,
)
from experiments.harness_bakeoff.secrets import load_provider_secrets
from experiments.harness_bakeoff.worker import MODULES


def _icp(icp_id: str) -> dict[str, str]:
    return {"icp_id": icp_id}


class RunPlanSelectionTests(unittest.TestCase):
    def test_public_runner_has_only_codex(self) -> None:
        expected = ("codex_sdk",)

        self.assertEqual(ARMS, expected)
        self.assertEqual(tuple(MODULES), expected)

    def test_all_loads_smoke_once_but_scores_only_the_requested_icps(self) -> None:
        available = ("icp_a", "icp_b", "icp_c")
        requested = ("icp_c", "icp_b")

        load_ids, smoke_ids, scored_ids = _run_plan_icp_ids(
            "all", available, requested, "icp_a"
        )

        self.assertEqual(load_ids, ("icp_a", *requested))
        self.assertEqual(smoke_ids, ("icp_a",))
        self.assertEqual(scored_ids, requested)

    def test_default_selection_uses_all_file_icps_and_first_for_smoke(self) -> None:
        available = ("icp_a", "icp_b", "icp_c")

        load_ids, smoke_ids, scored_ids = _run_plan_icp_ids("scored", available, None)

        self.assertEqual(load_ids, available)
        self.assertEqual(smoke_ids, ("icp_a",))
        self.assertEqual(scored_ids, available)

    def test_smoke_uses_first_selected_icp_by_default(self) -> None:
        load_ids, smoke_ids, scored_ids = _run_plan_icp_ids(
            "smoke",
            ("icp_a", "icp_b", "icp_c"),
            ("icp_c", "icp_b"),
        )

        self.assertEqual(load_ids, ("icp_c",))
        self.assertEqual(smoke_ids, ("icp_c",))
        self.assertEqual(scored_ids, ())

    def test_selection_rejects_missing_and_duplicate_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing selected IDs"):
            _run_plan_icp_ids("all", ("icp_a",), ("icp_missing",))
        with self.assertRaisesRegex(ValueError, "contains duplicates"):
            _run_plan_icp_ids("all", ("icp_a",), ("icp_a", "icp_a"))

    def test_smoke_plan_has_one_attempt_per_selected_arm(self) -> None:
        plans = _planned_attempts(
            phase="smoke",
            icps=[_icp("icp_smoke")],
            repetitions=9,
            model="openai/test",
            model_pricing={},
            seed=7,
            evaluation_date="2026-09-03",
            arms=("codex_sdk",),
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual({plan["arm"] for plan in plans}, {"codex_sdk"})
        self.assertEqual({plan["icp_id"] for plan in plans}, {"icp_smoke"})
        self.assertEqual({plan["repetition"] for plan in plans}, {1})

    def test_scored_plan_keeps_all_requested_icps_and_repetitions(self) -> None:
        plans = _planned_attempts(
            phase="scored",
            icps=[_icp("icp_a"), _icp("icp_b")],
            repetitions=2,
            model="openai/test",
            model_pricing={},
            seed=7,
            evaluation_date="2026-09-03",
            arms=("codex_sdk",),
        )

        self.assertEqual(len(plans), 4)
        self.assertEqual({plan["icp_id"] for plan in plans}, {"icp_a", "icp_b"})
        self.assertEqual({plan["repetition"] for plan in plans}, {1, 2})

    def test_smoke_plan_rejects_a_scored_icp_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "smoke requires exactly one"):
            _planned_attempts(
                phase="smoke",
                icps=[_icp("icp_a"), _icp("icp_b")],
                repetitions=1,
                model="openai/test",
                model_pricing={},
                seed=7,
                evaluation_date="2026-09-03",
                arms=("pi",),
            )


class StandaloneInputTests(unittest.TestCase):
    def test_provider_credentials_come_from_plain_environment_mapping(self) -> None:
        loaded = load_provider_secrets(
            {
                "OPENROUTER_API_KEY": "openrouter",
                "DEEPLINE_API_KEY": "deepline",
                "SCRAPINGDOG_API_KEY": "scrapingdog",
                "EXA_API_KEY": "exa",
                "UNRELATED_SECRET": "must-not-be-read",
            }
        )

        self.assertEqual(
            loaded,
            {
                "OPENROUTER_API_KEY": "openrouter",
                "DEEPLINE_API_KEY": "deepline",
                "SCRAPINGDOG_API_KEY": "scrapingdog",
                "EXA_API_KEY": "exa",
            },
        )

    def test_missing_required_provider_credential_fails(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "SCRAPINGDOG_API_KEY"):
            load_provider_secrets(
                {
                    "OPENROUTER_API_KEY": "openrouter",
                    "DEEPLINE_API_KEY": "deepline",
                }
            )

    def test_json_file_selection_preserves_order_and_normalizes_icps(self) -> None:
        payload = {
            "icps": [
                {
                    "icp_id": "icp_b",
                    "employee_count": "11-50|51-200",
                    "intent_signals": ["New product launch"],
                    "intent_signal_evidence_types": ["product_launch"],
                    "intent_signal_max_age_days": [60],
                },
                {"icp_id": "icp_a", "employee_count": ["1-10"]},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icps.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = load_icps(icp_file=path, icp_ids=("icp_a", "icp_b"))

        self.assertEqual([row["icp_id"] for row in loaded], ["icp_a", "icp_b"])
        self.assertEqual(loaded[1]["employee_count"], ["11-50", "51-200"])
        self.assertEqual(
            loaded[1]["required_intents"],
            [
                {
                    "signal": "New product launch",
                    "category": "PRODUCT_LAUNCH",
                    "max_age_days": 60,
                }
            ],
        )

    def test_json_file_without_selection_keeps_file_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icps.json"
            path.write_text(
                json.dumps([{"icp_id": "first"}, {"icp_id": "second"}]),
                encoding="utf-8",
            )

            loaded = load_icps(icp_file=path)

        self.assertEqual([row["icp_id"] for row in loaded], ["first", "second"])

    @patch("experiments.harness_bakeoff.runner.load_provider_secrets")
    def test_duplicate_file_ids_fail_before_provider_preflight(
        self, load_secrets
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icps.json"
            path.write_text(
                json.dumps([{"icp_id": "same"}, {"icp_id": "same"}]),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                main(["smoke", "--icp-file", str(path)])

        self.assertEqual(raised.exception.code, 2)
        load_secrets.assert_not_called()

    def test_icp_file_must_be_outside_repository(self) -> None:
        repository = Path(__file__).resolve().parents[1]

        with self.assertRaisesRegex(ValueError, "outside the repository"):
            external_icp_file(Path(__file__), repository)

    @patch("experiments.harness_bakeoff.runner._secure_directory")
    @patch("experiments.harness_bakeoff.runner._safe_output_path")
    @patch("experiments.harness_bakeoff.runner.select_model")
    @patch("experiments.harness_bakeoff.runner.deepline_preflight")
    @patch("experiments.harness_bakeoff.runner.load_provider_secrets")
    def test_preflight_does_not_require_an_icp_file(
        self,
        load_secrets,
        deepline_check,
        model_check,
        safe_output,
        _secure_directory,
    ) -> None:
        load_secrets.return_value = {
            "OPENROUTER_API_KEY": "openrouter",
            "DEEPLINE_API_KEY": "deepline",
            "SCRAPINGDOG_API_KEY": "scrapingdog",
        }
        deepline_check.return_value = {"connected": True}
        model_check.return_value = {
            "selected": "openai/test",
            "pricing": {},
            "probe": {"tool_call": True},
            "errors": [],
        }
        safe_output.return_value = Path("/tmp/codex-harness-test-output")

        self.assertEqual(main(["preflight"]), 0)


if __name__ == "__main__":
    unittest.main()
