from __future__ import annotations

import contextlib
import json
import unittest
from io import StringIO
from unittest.mock import patch

from google_ads_function_gateway.cli import main
from google_ads_function_gateway.config import GoogleAdsSettings
from google_ads_function_gateway.diagnostics import build_doctor_report


class CliAndDiagnosticsTests(unittest.TestCase):
    def test_doctor_reports_readiness_without_secret_values(self) -> None:
        settings = GoogleAdsSettings(
            developer_token="developer-token-value",
            client_id="client-id-value",
            client_secret="client-secret-value",
            refresh_token="refresh-token-value",
            login_customer_id="9990001111",
            api_version="v21",
            allowed_customer_ids=("1112223333", "4445556666"),
        )

        report = build_doctor_report(settings)
        encoded = json.dumps(report)

        self.assertIn(report["google_ads_library"], {"ok", "missing"})
        self.assertEqual(report["developer_token"], "configured")
        self.assertEqual(report["oauth_client"], "configured")
        self.assertEqual(report["refresh_token"], "configured")
        self.assertEqual(report["login_customer_id"], "configured")
        self.assertEqual(report["allowed_customer_ids"], 2)
        self.assertEqual(report["api_version"], "v21")
        self.assertNotIn("developer-token-value", encoded)
        self.assertNotIn("client-secret-value", encoded)
        self.assertNotIn("refresh-token-value", encoded)

    def test_cli_doctor_prints_json(self) -> None:
        stdout = StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = main(["doctor"])

        self.assertEqual(exit_code, 0)
        self.assertIn("google_ads_library", json.loads(stdout.getvalue()))

    def test_cli_list_accounts_uses_catalogue_envelope(self) -> None:
        fake_catalogue = _FakeCatalogue(
            {
                "success": True,
                "function": "list_accounts",
                "request_id": "req",
                "data": [],
                "meta": {"customer_ids": [], "currency_codes": [], "row_count": 0},
                "error": None,
            }
        )
        stdout = StringIO()

        with patch(
            "google_ads_function_gateway.cli.GoogleAdsFunctionCatalogue.from_settings",
            return_value=fake_catalogue,
        ), contextlib.redirect_stdout(stdout):
            exit_code = main(["list-accounts"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_catalogue.calls, [("list_accounts", {})])
        self.assertEqual(json.loads(stdout.getvalue())["function"], "list_accounts")

    def test_cli_get_campaign_cost_forwards_standard_params(self) -> None:
        fake_catalogue = _FakeCatalogue(
            {
                "success": True,
                "function": "get_campaign_cost",
                "request_id": "req",
                "data": [],
                "meta": {"customer_ids": [], "currency_codes": [], "row_count": 0},
                "error": None,
            }
        )

        stdout = StringIO()
        with patch(
            "google_ads_function_gateway.cli.GoogleAdsFunctionCatalogue.from_settings",
            return_value=fake_catalogue,
        ), contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "get-campaign-cost",
                    "--customer-id",
                    "1112223333",
                    "--start-date",
                    "2026-08-30",
                    "--end-date",
                    "2026-08-30",
                    "--campaign-ids",
                    "10",
                    "11",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            fake_catalogue.calls,
            [
                (
                    "get_campaign_cost",
                    {
                        "customer_id": "1112223333",
                        "start_date": "2026-08-30",
                        "end_date": "2026-08-30",
                        "campaign_ids": [10, 11],
                    },
                )
            ],
        )

    def test_cli_oauth_generate_refresh_token_prints_token_once(self) -> None:
        stdout = StringIO()
        with patch(
            "google_ads_function_gateway.cli.generate_google_ads_refresh_token",
            return_value="refresh-token-value",
        ) as generate, contextlib.redirect_stdout(stdout):
            exit_code = main(["oauth-generate-refresh-token", "--no-browser"])

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("GOOGLE_ADS_REFRESH_TOKEN=refresh-token-value", output)
        self.assertNotIn("GOOGLE_ADS_CLIENT_SECRET", output)
        generate.assert_called_once()

    def test_cli_oauth_generate_refresh_token_reports_missing_setup(self) -> None:
        stderr = StringIO()
        with patch(
            "google_ads_function_gateway.cli.GoogleAdsSettings.from_env",
            return_value=GoogleAdsSettings(),
        ), contextlib.redirect_stderr(stderr):
            exit_code = main(["oauth-generate-refresh-token"])

        self.assertEqual(exit_code, 2)
        self.assertIn("OAuth setup error", stderr.getvalue())
        self.assertIn("GOOGLE_ADS_CLIENT_ID", stderr.getvalue())


class _FakeCatalogue:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def invoke(self, function_name: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((function_name, params))
        return self._response


if __name__ == "__main__":
    unittest.main()
