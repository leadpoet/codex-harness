from __future__ import annotations

from datetime import date
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from experiments.harness_bakeoff import providers


class ProviderFreshnessTests(unittest.TestCase):
    def _tools(self, **kwargs) -> providers.LiveProviderTools:
        return providers.LiveProviderTools(
            deepline_api_key="deepline-test",
            scrapingdog_api_key="scrapingdog-test",
            **kwargs,
        )

    def test_environment_date_anchors_provider_recency(self) -> None:
        with patch.dict(
            os.environ,
            {"BAKEOFF_EVALUATION_DATE": "2026-09-02"},
            clear=False,
        ):
            tools = self._tools()

        with patch.object(
            tools, "_scrapingdog_search", return_value=[]
        ) as scrapingdog_search:
            with patch.object(tools, "_exa_search", return_value=[]) as exa_search:
                tools.search_web(
                    {
                        "query": "product launch",
                        "mode": "news",
                        "limit": 5,
                        "recency_days": 30,
                    }
                )

        scrapingdog_search.assert_called_once_with(
            "product launch after:2026-08-03", 5, "news"
        )
        exa_search.assert_called_once_with("product launch", 5, "news", "2026-08-03")

    def test_explicit_run_date_overrides_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"BAKEOFF_EVALUATION_DATE": "2025-01-01"},
            clear=False,
        ):
            tools = self._tools(evaluation_date="2026-09-02")

        self.assertEqual(tools.evaluation_date, date(2026, 9, 2))

    def test_news_default_window_uses_the_evaluation_date(self) -> None:
        tools = self._tools(evaluation_date="2026-09-02")
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"news_results": []},
        )

        with patch.object(providers.httpx, "get", return_value=response) as request:
            tools._scrapingdog_search("product launch", 1, "news")

        self.assertEqual(
            request.call_args.kwargs["params"]["query"],
            "product launch after:2025-09-02",
        )

    def test_invalid_environment_date_falls_back_to_utc(self) -> None:
        fixed_utc_day = date(2027, 4, 9)

        class FixedDateTime:
            @staticmethod
            def now(_timezone):
                class Current:
                    @staticmethod
                    def date():
                        return fixed_utc_day

                return Current()

        with patch.dict(
            os.environ,
            {"BAKEOFF_EVALUATION_DATE": "not-a-date"},
            clear=False,
        ):
            with patch.object(providers, "datetime", FixedDateTime):
                self.assertEqual(providers._evaluation_day(), fixed_utc_day)


if __name__ == "__main__":
    unittest.main()
