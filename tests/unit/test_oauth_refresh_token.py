from __future__ import annotations

import unittest
from types import SimpleNamespace

from google_ads_function_gateway.auth.oauth import (
    GOOGLE_ADS_OAUTH_SCOPE,
    LOOPBACK_REDIRECT_HOST,
    generate_google_ads_refresh_token,
)
from google_ads_function_gateway.config import GoogleAdsSettings
from google_ads_function_gateway.exceptions import ConfigurationError


class OAuthRefreshTokenTests(unittest.TestCase):
    def test_generate_refresh_token_uses_loopback_offline_access_and_consent(self) -> None:
        fake_flow = _FakeFlow(refresh_token="refresh-token-value")
        captured: dict[str, object] = {}

        def fake_flow_factory(client_config: dict[str, object], scopes: list[str]) -> _FakeFlow:
            captured["client_config"] = client_config
            captured["scopes"] = scopes
            return fake_flow

        token = generate_google_ads_refresh_token(
            settings=GoogleAdsSettings(
                client_id="client-id-value",
                client_secret="client-secret-value",
            ),
            port=9090,
            open_browser=False,
            flow_factory=fake_flow_factory,
        )

        self.assertEqual(token, "refresh-token-value")
        self.assertEqual(captured["scopes"], [GOOGLE_ADS_OAUTH_SCOPE])
        self.assertEqual(fake_flow.kwargs["host"], LOOPBACK_REDIRECT_HOST)
        self.assertEqual(fake_flow.kwargs["bind_addr"], LOOPBACK_REDIRECT_HOST)
        self.assertEqual(fake_flow.kwargs["port"], 9090)
        self.assertFalse(fake_flow.kwargs["open_browser"])
        self.assertEqual(fake_flow.kwargs["access_type"], "offline")
        self.assertEqual(fake_flow.kwargs["prompt"], "consent")
        self.assertNotIn("include_granted_scopes", fake_flow.kwargs)

        web_config = captured["client_config"]["web"]  # type: ignore[index]
        self.assertEqual(web_config["client_id"], "client-id-value")
        self.assertEqual(web_config["client_secret"], "client-secret-value")
        self.assertEqual(web_config["redirect_uris"], ["http://127.0.0.1:9090/"])

    def test_generate_refresh_token_requires_oauth_client_configuration(self) -> None:
        with self.assertRaises(ConfigurationError) as context:
            generate_google_ads_refresh_token(
                settings=GoogleAdsSettings(client_id="client-id-value"),
                flow_factory=lambda client_config, scopes: _FakeFlow("unused"),
            )

        self.assertEqual(context.exception.code, "missing_oauth_client_configuration")
        self.assertEqual(context.exception.context["missing"], ["GOOGLE_ADS_CLIENT_SECRET"])

    def test_generate_refresh_token_errors_when_google_returns_no_refresh_token(self) -> None:
        with self.assertRaises(ConfigurationError) as context:
            generate_google_ads_refresh_token(
                settings=GoogleAdsSettings(
                    client_id="client-id-value",
                    client_secret="client-secret-value",
                ),
                flow_factory=lambda client_config, scopes: _FakeFlow(None),
            )

        self.assertEqual(context.exception.code, "missing_refresh_token_from_oauth_flow")


class _FakeFlow:
    def __init__(self, refresh_token: str | None) -> None:
        self._refresh_token = refresh_token
        self.kwargs: dict[str, object] = {}

    def run_local_server(self, **kwargs: object) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(refresh_token=self._refresh_token)


if __name__ == "__main__":
    unittest.main()
