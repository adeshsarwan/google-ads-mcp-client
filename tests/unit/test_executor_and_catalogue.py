from __future__ import annotations

import unittest

from google_ads_function_gateway.client.protocols import SearchPage
from tests.unit.fakes import FakeGoogleAdsClient, FakeGoogleAdsException, build_catalogue


class ExecutorAndCatalogueTests(unittest.TestCase):
    def test_transient_google_ads_error_is_retried(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "1112223333": [
                    FakeGoogleAdsException("RESOURCE_EXHAUSTED"),
                    SearchPage(
                        rows=[
                            {
                                "customer": {"id": "1112223333", "currency_code": "USD"},
                                "campaign": {"id": 10, "name": "Brand", "status": "ENABLED"},
                                "metrics": {"cost_micros": 1000000},
                                "segments": {"date": "2026-08-01"},
                            }
                        ]
                    ),
                ]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_cost",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
            },
            request_id="req-x1",
        )

        self.assertTrue(result["success"])
        self.assertEqual(len(fake.search_calls), 2)

    def test_retryable_error_becomes_normalized_failure_after_attempts(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "1112223333": [
                    FakeGoogleAdsException("RESOURCE_EXHAUSTED"),
                    FakeGoogleAdsException("RESOURCE_EXHAUSTED"),
                ]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_cost",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
            },
            request_id="req-x2",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "rate_limit")
        self.assertTrue(result["error"]["retryable"])

    def test_unknown_function_uses_standard_envelope(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke("run_gaql", {"query": "SELECT campaign.id"}, request_id="req-x3")

        self.assertFalse(result["success"])
        self.assertEqual(result["function"], "run_gaql")
        self.assertEqual(result["error"]["code"], "unknown_function")
        self.assertEqual(result["data"], {})


if __name__ == "__main__":
    unittest.main()
