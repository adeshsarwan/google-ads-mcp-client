from __future__ import annotations

import unittest

from tests.unit.fakes import FakeGoogleAdsClient, FakeGoogleAdsException, build_catalogue


class CampaignFunctionTests(unittest.TestCase):
    def test_list_campaigns_success_with_filters_pagination_and_null_sub_type(self) -> None:
        fake = FakeGoogleAdsClient(
            search_pages_by_customer={
                "1112223333": [
                    [
                        {
                            "campaign": {
                                "id": 10,
                                "name": "Brand Search",
                                "status": "ENABLED",
                                "advertising_channel_type": "SEARCH",
                                "advertising_channel_sub_type": None,
                            }
                        }
                    ],
                    [],
                ]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "list_campaigns",
            {
                "customer_id": "1112223333",
                "status": "enabled",
                "campaign_ids": [10],
                "campaign_name_contains": "Brand",
                "channel_type": "search",
            },
            request_id="req-c1",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["meta"]["row_count"], 1)
        self.assertEqual(result["data"][0]["campaign_id"], 10)
        self.assertIsNone(result["data"][0]["channel_sub_type"])
        query = fake.search_calls[0]["query"]
        self.assertIn("campaign.status = ENABLED", query)
        self.assertIn("campaign.id IN (10)", query)
        self.assertIn("campaign.name LIKE '%Brand%'", query)
        self.assertNotIn("raw_gaql", query)
        self.assertEqual(fake.search_calls[1]["page_token"], "1")

    def test_list_campaigns_empty_result(self) -> None:
        fake = FakeGoogleAdsClient(search_pages_by_customer={"1112223333": []})
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "list_campaigns",
            {"customer_id": "1112223333"},
            request_id="req-c2",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"], [])

    def test_list_campaigns_invalid_input(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "list_campaigns",
            {"customer_id": "1112223333", "campaign_ids": "10"},
            request_id="req-c3",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "validation")

    def test_list_campaigns_unauthorized_customer(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("9990001111",))

        result = catalogue.invoke(
            "list_campaigns",
            {"customer_id": "1112223333"},
            request_id="req-c4",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "authorization")

    def test_list_campaigns_google_ads_error(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "1112223333": [FakeGoogleAdsException("INVALID_ARGUMENT")]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "list_campaigns",
            {"customer_id": "1112223333"},
            request_id="req-c5",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "validation")

    def test_get_campaign_details_success_with_non_applicable_bidding_fields(self) -> None:
        fake = FakeGoogleAdsClient(
            search_pages_by_customer={
                "1112223333": [
                    [
                        {
                            "customer": {"id": 0, "currency_code": "USD"},
                            "campaign": {
                                "id": 10,
                                "name": "Brand Search",
                                "status": "ENABLED",
                                "advertising_channel_type": "SEARCH",
                                "advertising_channel_sub_type": None,
                                "campaign_budget": "customers/1112223333/campaignBudgets/20",
                                "bidding_strategy": None,
                                "bidding_strategy_type": "MANUAL_CPC",
                                "target_cpa": {"target_cpa_micros": None},
                                "target_roas": {"target_roas": None},
                            },
                            "campaign_budget": {"id": 20, "amount_micros": 50000000},
                        }
                    ]
                ]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_details",
            {"customer_id": "1112223333", "campaign_id": 10},
            request_id="req-c6",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["customer_id"], "1112223333")
        self.assertEqual(result["data"]["daily_budget"], 50.0)
        self.assertEqual(result["data"]["daily_budget_micros"], 50000000)
        self.assertIsNone(result["data"]["target_cpa"])
        self.assertIsNone(result["data"]["target_roas"])
        self.assertIn("customer.id", fake.search_calls[0]["query"])

    def test_get_campaign_details_nulls_non_applicable_target_roas_default(self) -> None:
        fake = FakeGoogleAdsClient(
            search_pages_by_customer={
                "1112223333": [
                    [
                        {
                            "customer": {"id": "1112223333", "currency_code": "USD"},
                            "campaign": {
                                "id": 10,
                                "name": "Brand Search",
                                "status": "ENABLED",
                                "advertising_channel_type": "DISPLAY",
                                "advertising_channel_sub_type": "UNSPECIFIED",
                                "campaign_budget": "customers/1112223333/campaignBudgets/20",
                                "bidding_strategy": "",
                                "bidding_strategy_type": "TARGET_CPA",
                                "target_cpa": {"target_cpa_micros": 12000000},
                                "target_roas": {"target_roas": 0.0},
                            },
                            "campaign_budget": {"id": 20, "amount_micros": 50000000},
                        }
                    ]
                ]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_details",
            {"customer_id": "1112223333", "campaign_id": 10},
            request_id="req-c6-target-cpa",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["target_cpa"], 12.0)
        self.assertEqual(result["data"]["target_cpa_micros"], 12000000)
        self.assertIsNone(result["data"]["target_roas"])

    def test_get_campaign_details_empty_result(self) -> None:
        fake = FakeGoogleAdsClient(search_pages_by_customer={"1112223333": []})
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_details",
            {"customer_id": "1112223333", "campaign_id": 10},
            request_id="req-c7",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"], {})

    def test_get_campaign_details_invalid_input(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_details",
            {"customer_id": "1112223333", "campaign_id": 0},
            request_id="req-c8",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "invalid_campaign_id")

    def test_get_campaign_details_unauthorized_customer(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("9990001111",))

        result = catalogue.invoke(
            "get_campaign_details",
            {"customer_id": "1112223333", "campaign_id": 10},
            request_id="req-c9",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "authorization")

    def test_get_campaign_details_google_ads_error(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "1112223333": [FakeGoogleAdsException("UNKNOWN_ERROR")]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_details",
            {"customer_id": "1112223333", "campaign_id": 10},
            request_id="req-c10",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "google_ads_api")

    def test_get_campaign_details_uses_predefined_fallback_for_bidding_field_error(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "1112223333": [
                    FakeGoogleAdsException("INVALID_ARGUMENT"),
                    [
                        {
                            "customer": {"id": "1112223333", "currency_code": "USD"},
                            "campaign": {
                                "id": 10,
                                "name": "Brand Search",
                                "status": "ENABLED",
                                "advertising_channel_type": "SEARCH",
                                "advertising_channel_sub_type": None,
                                "campaign_budget": "customers/1112223333/campaignBudgets/20",
                                "bidding_strategy": None,
                                "bidding_strategy_type": "MANUAL_CPC",
                            },
                            "campaign_budget": {"id": 20, "amount_micros": 50000000},
                        }
                    ],
                ]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_details",
            {"customer_id": "1112223333", "campaign_id": 10},
            request_id="req-c10-fallback",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["campaign_id"], 10)
        self.assertEqual(result["data"]["daily_budget"], 50.0)
        self.assertIsNone(result["data"]["target_cpa"])
        self.assertIsNone(result["data"]["target_roas"])

    def test_get_campaign_cost_success_conversion_and_pagination(self) -> None:
        fake = FakeGoogleAdsClient(
            search_pages_by_customer={
                "1112223333": [
                    [
                        {
                            "customer": {"id": "1112223333", "currency_code": "USD"},
                            "campaign": {"id": 10, "name": "Brand", "status": "ENABLED"},
                            "segments": {"date": "2026-08-01"},
                            "metrics": {"cost_micros": 1234567},
                        }
                    ],
                    [
                        {
                            "customer": {"id": "1112223333", "currency_code": "USD"},
                            "campaign": {"id": 11, "name": "Generic", "status": "PAUSED"},
                            "segments": {"date": "2026-08-02"},
                            "metrics": {"cost_micros": 0},
                        }
                    ],
                ]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_cost",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
                "campaign_ids": [10, 11],
            },
            request_id="req-c11",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["meta"]["row_count"], 2)
        self.assertEqual(result["data"][0]["cost"], 1.234567)
        self.assertEqual(result["data"][0]["cost_micros"], 1234567)
        self.assertEqual(result["data"][1]["cost"], 0.0)
        self.assertNotIn("page_size", fake.search_calls[0])
        self.assertNotIn("page_size", fake.search_calls[1])
        self.assertIsNone(fake.search_calls[0]["page_token"])
        self.assertEqual(fake.search_calls[1]["page_token"], "1")

    def test_get_campaign_cost_empty_result(self) -> None:
        fake = FakeGoogleAdsClient(search_pages_by_customer={"1112223333": []})
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_cost",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
            request_id="req-c12",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"], [])

    def test_get_campaign_cost_invalid_date_range(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_cost",
            {
                "customer_id": "1112223333",
                "start_date": "2026-09-01",
                "end_date": "2026-08-31",
            },
            request_id="req-c13",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "invalid_date_range")

    def test_get_campaign_cost_unauthorized_customer(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("9990001111",))

        result = catalogue.invoke(
            "get_campaign_cost",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
            request_id="req-c14",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "authorization")

    def test_get_campaign_cost_google_ads_error(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "1112223333": [FakeGoogleAdsException("INVALID_ARGUMENT")]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_cost",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
            request_id="req-c15",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "validation")

    def test_get_campaign_performance_multi_customer_success_and_zero_conversion_cpa(self) -> None:
        fake = FakeGoogleAdsClient(
            search_pages_by_customer={
                "1112223333": [
                    [
                        {
                            "customer": {"id": "1112223333", "currency_code": "USD"},
                            "campaign": {"id": 10, "name": "Brand", "status": "ENABLED"},
                            "metrics": {
                                "impressions": 100,
                                "clicks": 10,
                                "cost_micros": 2500000,
                                "conversions": 2.0,
                                "conversions_value": 30.5,
                                "ctr": 0.1,
                                "average_cpc": 250000,
                            },
                        }
                    ]
                ],
                "4445556666": [
                    [
                        {
                            "customer": {"id": "4445556666", "currency_code": "EUR"},
                            "campaign": {"id": 20, "name": "Shopping", "status": "PAUSED"},
                            "metrics": {
                                "impressions": 20,
                                "clicks": 0,
                                "cost_micros": 1000000,
                                "conversions": 0.0,
                                "conversions_value": 0.0,
                                "ctr": 0.0,
                                "average_cpc": None,
                            },
                        }
                    ]
                ],
            }
        )
        catalogue = build_catalogue(
            fake,
            allowed_customer_ids=("1112223333", "4445556666"),
        )

        result = catalogue.invoke(
            "get_campaign_performance",
            {
                "customer_ids": ["1112223333", "4445556666"],
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
            request_id="req-c16",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["meta"]["row_count"], 2)
        self.assertEqual(result["data"][0]["cost"], 2.5)
        self.assertEqual(result["data"][0]["average_cpc"], 0.25)
        self.assertEqual(result["data"][0]["cpa"], 1.25)
        self.assertIsNone(result["data"][1]["cpa"])
        self.assertEqual(result["meta"]["currency_codes"], ["EUR", "USD"])

    def test_get_campaign_performance_empty_result(self) -> None:
        fake = FakeGoogleAdsClient(search_pages_by_customer={"1112223333": []})
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_performance",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
            request_id="req-c17",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"], [])

    def test_get_campaign_performance_invalid_ambiguous_customer_input(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_performance",
            {
                "customer_id": "1112223333",
                "customer_ids": ["1112223333"],
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
            request_id="req-c18",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "ambiguous_customer_ids")

    def test_get_campaign_performance_unauthorized_customer(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_performance",
            {
                "customer_ids": ["1112223333", "4445556666"],
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
            request_id="req-c19",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "authorization")

    def test_get_campaign_performance_google_ads_error(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "1112223333": [FakeGoogleAdsException("INVALID_ARGUMENT")]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_campaign_performance",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
            request_id="req-c20",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "validation")


if __name__ == "__main__":
    unittest.main()
