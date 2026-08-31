from __future__ import annotations

import os
import unittest
from datetime import date, timedelta

from google_ads_function_gateway.catalogue import GoogleAdsFunctionCatalogue
from google_ads_function_gateway.config import GoogleAdsSettings
from google_ads_function_gateway.diagnostics import build_doctor_report
from google_ads_function_gateway.env import load_local_env

load_local_env()


@unittest.skipUnless(
    os.getenv("GOOGLE_ADS_RUN_LIVE_TESTS") == "1",
    "Set GOOGLE_ADS_RUN_LIVE_TESTS=1 and provide Google Ads credentials to run live tests.",
)
class LiveGoogleAdsSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = GoogleAdsSettings.from_env()
        if not cls.settings.allowed_customer_ids:
            raise unittest.SkipTest("Set GOOGLE_ADS_ALLOWED_CUSTOMER_IDS for live reporting tests.")
        cls.catalogue = GoogleAdsFunctionCatalogue.from_settings(cls.settings)
        cls.customer_id = cls.settings.allowed_customer_ids[0]
        smoke_date = date.today() - timedelta(days=1)
        cls.date_params = {
            "start_date": smoke_date.isoformat(),
            "end_date": smoke_date.isoformat(),
        }

    def test_doctor_reports_live_readiness(self) -> None:
        report = build_doctor_report(self.settings)
        self.assertEqual(report["google_ads_library"], "ok")
        self.assertEqual(report["developer_token"], "configured")
        self.assertEqual(report["oauth_client"], "configured")
        self.assertEqual(report["refresh_token"], "configured")
        self.assertGreater(report["allowed_customer_ids"], 0)

    def test_live_account_discovery(self) -> None:
        result = self.catalogue.invoke("list_accounts", {})
        self.assertTrue(result["success"], result)
        self.assertIn("data", result)

    def test_live_authorized_account_details(self) -> None:
        result = self.catalogue.invoke("get_account_details", {"customer_id": self.customer_id})
        self.assertTrue(result["success"], result)
        self.assertIn("data", result)

    def test_live_campaign_listing(self) -> None:
        result = self.catalogue.invoke("list_campaigns", {"customer_id": self.customer_id})
        self.assertTrue(result["success"], result)
        self.assertIsInstance(result["data"], list)

    def test_live_campaign_details_when_campaign_exists(self) -> None:
        campaigns = self.catalogue.invoke("list_campaigns", {"customer_id": self.customer_id})
        self.assertTrue(campaigns["success"], campaigns)
        if not campaigns["data"]:
            self.skipTest("No campaigns found in authorized account.")
        result = self.catalogue.invoke(
            "get_campaign_details",
            {
                "customer_id": self.customer_id,
                "campaign_id": campaigns["data"][0]["campaign_id"],
            },
        )
        self.assertTrue(result["success"], result)

    def test_live_campaign_cost(self) -> None:
        result = self.catalogue.invoke(
            "get_campaign_cost",
            {"customer_id": self.customer_id, **self.date_params},
        )
        self.assertTrue(result["success"], result)
        self.assertIsInstance(result["data"], list)

    def test_live_campaign_performance(self) -> None:
        result = self.catalogue.invoke(
            "get_campaign_performance",
            {"customer_id": self.customer_id, **self.date_params},
        )
        self.assertTrue(result["success"], result)
        self.assertIsInstance(result["data"], list)


if __name__ == "__main__":
    unittest.main()
