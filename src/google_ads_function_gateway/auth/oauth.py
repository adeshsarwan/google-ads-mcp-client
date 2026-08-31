"""OAuth configuration bridge for the official Google Ads client."""

from __future__ import annotations

from google_ads_function_gateway.config import GoogleAdsSettings


def load_oauth_settings_from_env() -> GoogleAdsSettings:
    """Return centralized settings for OAuth-backed live Google Ads calls."""

    return GoogleAdsSettings.from_env()
