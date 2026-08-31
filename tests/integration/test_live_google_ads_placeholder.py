from __future__ import annotations

import os
import unittest

from google_ads_function_gateway.catalogue import GoogleAdsFunctionCatalogue


class LiveGoogleAdsIntegrationPlaceholder(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv("GOOGLE_ADS_RUN_LIVE_TESTS") == "1",
        "Set GOOGLE_ADS_RUN_LIVE_TESTS=1 and provide Google Ads credentials to run live tests.",
    )
    def test_live_list_accounts_smoke(self) -> None:
        catalogue = GoogleAdsFunctionCatalogue.from_settings()
        result = catalogue.invoke("list_accounts", {})
        self.assertIn("success", result)


if __name__ == "__main__":
    unittest.main()
