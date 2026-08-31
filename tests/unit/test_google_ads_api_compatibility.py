from __future__ import annotations

import unittest
from importlib import import_module

from google_ads_function_gateway.config import resolve_default_api_version


class GoogleAdsApiCompatibilityTests(unittest.TestCase):
    def test_fixed_query_fields_exist_in_resolved_google_ads_api_version(self) -> None:
        api_version = resolve_default_api_version()
        if api_version is None:
            self.skipTest("google-ads package is not installed.")

        classes = {
            "Campaign": _load_type(api_version, "resources.types.campaign", "Campaign"),
            "CampaignBudget": _load_type(
                api_version,
                "resources.types.campaign_budget",
                "CampaignBudget",
            ),
            "Customer": _load_type(api_version, "resources.types.customer", "Customer"),
            "CustomerClient": _load_type(
                api_version,
                "resources.types.customer_client",
                "CustomerClient",
            ),
            "Metrics": _load_type(api_version, "common.types.metrics", "Metrics"),
            "Segments": _load_type(api_version, "common.types.segments", "Segments"),
        }
        expected_fields = {
            "Campaign": {
                "id",
                "name",
                "status",
                "advertising_channel_type",
                "advertising_channel_sub_type",
                "campaign_budget",
                "bidding_strategy",
                "bidding_strategy_type",
                "target_cpa",
                "target_roas",
            },
            "CampaignBudget": {"id", "amount_micros"},
            "Customer": {
                "id",
                "descriptive_name",
                "currency_code",
                "time_zone",
                "status",
                "manager",
            },
            "CustomerClient": {
                "id",
                "client_customer",
                "descriptive_name",
                "currency_code",
                "time_zone",
                "status",
                "manager",
                "level",
                "hidden",
            },
            "Metrics": {
                "impressions",
                "clicks",
                "cost_micros",
                "conversions",
                "conversions_value",
                "ctr",
                "average_cpc",
            },
            "Segments": {"date"},
        }

        for type_name, fields in expected_fields.items():
            with self.subTest(api_version=api_version, type_name=type_name):
                self.assertTrue(fields <= set(classes[type_name].meta.fields))


def _load_type(api_version: str, module_path: str, type_name: str) -> type:
    module = import_module(f"google.ads.googleads.{api_version}.{module_path}")
    return getattr(module, type_name)


if __name__ == "__main__":
    unittest.main()
