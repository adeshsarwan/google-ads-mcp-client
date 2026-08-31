from __future__ import annotations

import unittest

from google_ads_function_gateway.client.protocols import SearchPage
from tests.unit.fakes import FakeGoogleAdsClient, FakeGoogleAdsException, build_catalogue


class AccountFunctionTests(unittest.TestCase):
    def test_list_accounts_success_filters_unauthorized_and_paginates(self) -> None:
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
                            }
                        }
                    ],
                ]
            }
        )
        catalogue = build_catalogue(
            fake,
            allowed_customer_ids=("9990001111", "1112223333"),
        )

        result = catalogue.invoke("list_accounts", {}, request_id="req-1")

        self.assertTrue(result["success"])
        self.assertEqual(result["meta"]["row_count"], 1)
        self.assertEqual(result["data"][0]["customer_id"], "1112223333")
        self.assertEqual(result["data"][0]["descriptive_name"], "Alpha")
        self.assertEqual(len(fake.search_calls), 2)
        self.assertEqual(fake.search_calls[1]["page_token"], "1")

    def test_list_accounts_without_manager_uses_accessible_customer_service(self) -> None:
        fake = FakeGoogleAdsClient(
            accessible_customers=("customers/1112223333", "customers/4445556666")
        )
        catalogue = build_catalogue(
            fake,
            allowed_customer_ids=("1112223333",),
            manager_customer_id=None,
        )

        result = catalogue.invoke("list_accounts", {}, request_id="req-2")

        self.assertTrue(result["success"])
        self.assertEqual(result["data"], [
            {
                "customer_id": "1112223333",
                "descriptive_name": None,
                "currency_code": None,
                "timezone": None,
                "status": None,
                "manager": None,
            }
        ])
        self.assertEqual(fake.accessible_customer_calls, 1)

    def test_list_accounts_empty_result(self) -> None:
        fake = FakeGoogleAdsClient(search_pages_by_customer={"9990001111": []})
        catalogue = build_catalogue(fake, allowed_customer_ids=("9990001111",))

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

    def test_list_accounts_unauthorized_manager(self) -> None:
        fake = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake, allowed_customer_ids=("1112223333",))

        result = catalogue.invoke("list_accounts", {}, request_id="req-5")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "authorization")
        self.assertEqual(fake.search_calls, [])

    def test_list_accounts_google_ads_error(self) -> None:
        fake = FakeGoogleAdsClient(
            side_effects_by_customer={
                "9990001111": [FakeGoogleAdsException("INVALID_ARGUMENT")]
            }
        )
        catalogue = build_catalogue(fake, allowed_customer_ids=("9990001111",))

        result = catalogue.invoke("list_accounts", {}, request_id="req-6")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["category"], "validation")

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
