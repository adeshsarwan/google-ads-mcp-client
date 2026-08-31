"""Configuration diagnostics that never reveal secret values."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from google_ads_function_gateway.config import GoogleAdsSettings, supported_api_versions


def build_doctor_report(settings: GoogleAdsSettings | None = None) -> dict[str, Any]:
    settings = settings or GoogleAdsSettings.from_env()
    library = _google_ads_library_report()
    developer_token_configured = bool(settings.developer_token)
    oauth_client_configured = bool(settings.client_id and settings.client_secret)
    refresh_token_configured = bool(settings.refresh_token)
    login_customer_id_configured = bool(settings.login_customer_id)
    allowed_customer_count = len(settings.allowed_customer_ids)

    credentials_ready = (
        developer_token_configured and oauth_client_configured and refresh_token_configured
    )
    discovery_ready = library["google_ads_library"] == "ok" and credentials_ready
    mcc_discovery_ready = discovery_ready and login_customer_id_configured
    reporting_ready = discovery_ready and allowed_customer_count > 0

    return {
        **library,
        "developer_token": _configured_status(developer_token_configured),
        "oauth_client": _configured_status(oauth_client_configured),
        "refresh_token": _configured_status(refresh_token_configured),
        "login_customer_id": _configured_status(login_customer_id_configured),
        "allowed_customer_ids": allowed_customer_count,
        "api_version": settings.api_version or "unavailable",
        "discovery_ready": discovery_ready,
        "mcc_discovery_ready": mcc_discovery_ready,
        "reporting_ready": reporting_ready,
        "ready": mcc_discovery_ready and reporting_ready,
    }


def _google_ads_library_report() -> dict[str, Any]:
    try:
        package_version = version("google-ads")
    except PackageNotFoundError:
        return {
            "google_ads_library": "missing",
            "google_ads_library_version": None,
            "supported_api_versions": [],
        }

    return {
        "google_ads_library": "ok",
        "google_ads_library_version": package_version,
        "supported_api_versions": list(supported_api_versions()),
    }


def _configured_status(configured: bool) -> str:
    return "configured" if configured else "missing"
