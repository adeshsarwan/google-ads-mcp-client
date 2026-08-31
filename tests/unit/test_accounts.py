from __future__ import annotations

import unittest

from tests.unit.fakes import FakeGoogleAdsClient, FakeGoogleAdsException, build_catalogue


class AccountFunctionTests(unittest.TestCase):
    def test_list_accounts_configured_mcc_discovery_returns_unallowlisted_children(self) -> None:
        fake = FakeGoogleAdsClient(
            search_pages_by_customer={
                "9990001111": [
                    [
                        {
                            "customer_client": {
                                "id": "1112223333",
                                "descriptive_name": "Alpha",
                                "currency_code": "USD",
                                "time_zone": "America/New_York",
                                "status": "ENABLED",
                                "manager": False,
                                "level": 1,
                            }
                        }
                    ],
                    [
                        {
                            "customer_client": {
                                "id": "4445556666",
                                "descriptive_name": "Blocked",
                                "currency_code": "EUR",
                                "time_zone": "Europe/Berlin",
                                "status": "ENABLED",
                                "manager": False,
                                "level": 1,
                            }
                        }
                    ],
                ]
            }
        )
        catalogue = build_catalogue(
            fake,
            allowed_customer_ids=(),
        )

        result = catalogue.invoke("list_accounts", {}, request_id="req-1")

        self.assertTrue(result["success"])
        self.assertEqual(result["meta"]["row_count"], 2)
        self.assertEqual(result["meta"]["discovery_mode"], "configured_mcc")
        self.assertEqual(result["meta"]["authorization"], "discovery_only")
        self.assertEqual(result["data"][0]["customer_id"], "1112223333")
        self.assertEqual(result["data"][0]["descriptive_name"], "Alpha")
        self.assertEqual(result["data"][0]["account_relationship"], "child")
        self.assertEqual(result["data"][1]["customer_id"], "4445556666")
        self.assertEqual(len(fake.search_calls), 2)
        self.assertNotIn("page_size", fake.search_calls[0])
        self.assertNotIn("page_size", fake.search_calls[1])
        self.assertIsNone(fake.search_calls[0]["page_token"])
        self.assertEqual(fake.search_calls[1]["page_token"], "1")

    def test_list_accounts_without_manager_uses_accessible_customer_service(self) -> None:
        fake = FakeGoogleAdsClient(
            accessible_customers=("customers/1112223333", "customers/4445556666")
        )
        catalogue = build_catalogue(
            fake,
            allowed_customer_ids=(),
            manager_customer_id=None,
        )

        result = catalogue.invoke("list_accounts", {}, request_id="req-2")

        self.assertTrue(result["success"])
        self.assertEqual([row["customer_id"] for row in result["data"]], [
            "1112223333",
            "4445556666",
        ])
        self.assertEqual(result["meta"]["discovery_mode"], "direct_accessible_customers")
        self.assertEqual(fake.accessible_customer_calls, 1)

    def test_list_accounts_empty_result(self) -> None:
        fake = FakeGoogleAdsClient(search_pages_by_customer={"9990001111": []})
        catalogue = build_catalogue(fake, allowed_customer_ids=())

        result = catalogue.invoke("list_accounts", {}, request_id="req-3")

        self.assertTrue(result["success"])
        self.assertEqual(result["data"], [])
        self.assertEqual(result["meta"]["row_count"], 0)

    def test_list_accounts_invalid_input(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("9990001111",))

        result = catalogue.invoke("list_accounts", {"raw_gaql": "SELECT *"}, request_id="req-4")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "validation")

    def test_list_accounts_google_ads_error(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "9990001111": [FakeGoogleAdsException("INVALID_ARGUMENT")]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=())

        result = catalogue.invoke("list_accounts", {}, request_id="req-6")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "validation")

    def test_discovered_child_does_not_grant_reporting_authorization(self) -> None:
        fake = FakeGoogleAdsClient(
            search_pages_by_customer={
                "9990001111": [
                    [
                        {
                            "customer_client": {
                                "id": "1112223333",
                                "descriptive_name": "Alpha",
                                "currency_code": "USD",
                                "time_zone": "America/New_York",
                                "status": "ENABLED",
                                "manager": False,
                            }
                        }
                    ]
                ],
                "1112223333": [
                    [
                        {
                            "customer": {"id": "1112223333", "currency_code": "USD"},
                            "campaign": {"id": 10, "name": "Brand", "status": "ENABLED"},
                            "segments": {"date": "2026-08-30"},
                            "metrics": {"cost_micros": 1000000},
                        }
                    ]
                ],
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=())

        discovery = catalogue.invoke("list_accounts", {}, request_id="req-discovery")
        denied_campaigns = catalogue.invoke(
            "list_campaigns",
            {"customer_id": "1112223333"},
            request_id="req-denied-campaigns",
        )
        denied_cost = catalogue.invoke(
            "get_campaign_cost",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-30",
                "end_date": "2026-08-30",
            },
            request_id="req-denied-cost",
        )

        self.assertTrue(discovery["success"])
        self.assertEqual(discovery["data"][0]["customer_id"], "1112223333")
        self.assertFalse(denied_campaigns["success"])
        self.assertEqual(denied_campaigns["error"]["category"], "authorization")
        self.assertFalse(denied_cost["success"])
        self.assertEqual(denied_cost["error"]["category"], "authorization")

    def test_allowlisted_discovered_child_can_run_reporting(self) -> None:
        fake = FakeGoogleAdsClient(
            search_pages_by_customer={
                "9990001111": [
                    [
                        {
                            "customer_client": {
                                "id": "1112223333",
                                "descriptive_name": "Alpha",
                                "currency_code": "USD",
                                "time_zone": "America/New_York",
                                "status": "ENABLED",
                                "manager": False,
                            }
                        }
                    ]
                ],
                "1112223333": [
                    [
                        {
                            "customer": {"id": "1112223333", "currency_code": "USD"},
                            "campaign": {"id": 10, "name": "Brand", "status": "ENABLED"},
                            "segments": {"date": "2026-08-30"},
                            "metrics": {"cost_micros": 1000000},
                        }
                    ]
                ],
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        discovery = catalogue.invoke("list_accounts", {}, request_id="req-discovery-allowed")
        cost = catalogue.invoke(
            "get_campaign_cost",
            {
                "customer_id": "1112223333",
                "start_date": "2026-08-30",
                "end_date": "2026-08-30",
            },
            request_id="req-cost-allowed",
        )

        self.assertTrue(discovery["success"])
        self.assertTrue(cost["success"])
        self.assertEqual(cost["data"][0]["cost"], 1.0)

    def test_get_account_details_success_with_null_manager_flag(self) -> None:
        fake = FakeGoogleAdsClient(
            search_pages_by_customer={
                "1112223333": [
                    [
                        {
                            "customer": {
                                "id": "1112223333",
                                "descriptive_name": "Alpha",
                                "currency_code": "USD",
                                "time_zone": "America/New_York",
                                "status": "ENABLED",
                                "manager": None,
                            }
                        }
                    ]
                ]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_account_details",
            {"customer_id": "111-222-3333"},
            request_id="req-7",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["customer_id"], "1112223333")
        self.assertIsNone(result["data"]["manager"])
        self.assertEqual(result["meta"]["currency_codes"], ["USD"])

    def test_get_account_details_empty_result(self) -> None:
        fake = FakeGoogleAdsClient(search_pages_by_customer={"1112223333": []})
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_account_details",
            {"customer_id": "1112223333"},
            request_id="req-8",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"], {})
        self.assertEqual(result["meta"]["row_count"], 0)

    def test_get_account_details_invalid_input(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke("get_account_details", {}, request_id="req-9")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "missing_customer_id")

    def test_get_account_details_unauthorized_customer(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("9990001111",))

        result = catalogue.invoke(
            "get_account_details",
            {"customer_id": "1112223333"},
            request_id="req-10",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "authorization")
        self.assertEqual(fake.search_calls, [])

    def test_get_account_details_google_ads_error(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "1112223333": [FakeGoogleAdsException("INVALID_ARGUMENT")]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke(
            "get_account_details",
            {"customer_id": "1112223333"},
            request_id="req-11",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "validation")


if __name__ == "__main__":
    unittest.main()
