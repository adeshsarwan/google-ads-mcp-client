"""Centralized runtime configuration for the Google Ads gateway."""

from __future__ import annotations

import os
import pkgutil
import re
from collections.abc import Iterable
from dataclasses import dataclass

from google_ads_function_gateway.dto.validation import normalize_customer_id


@dataclass(frozen=True)
class GoogleAdsSettings:
    """Settings required by the official Google Ads API client wrapper."""

    developer_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    login_customer_id: str | None = None
    api_version: str | None = None
    retry_attempts: int = 3
    allowed_customer_ids: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> GoogleAdsSettings:
        """Load configuration from environment variables."""

        login_customer_id = _optional_customer_id(os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"))
        allowed_customer_ids = _customer_ids_from_csv(
            os.getenv("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS", "")
        )
        return cls(
            developer_token=os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
            client_id=os.getenv("GOOGLE_ADS_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
            refresh_token=os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
            login_customer_id=login_customer_id,
            api_version=os.getenv("GOOGLE_ADS_API_VERSION") or resolve_default_api_version(),
            retry_attempts=_int_from_env("GOOGLE_ADS_RETRY_ATTEMPTS", 3),
            allowed_customer_ids=allowed_customer_ids,
        )

    def require_official_client_credentials(self) -> None:
        """Raise a clear configuration error if OAuth credentials are incomplete."""

        from google_ads_function_gateway.exceptions import ConfigurationError

        missing = [
            name
            for name, value in (
                ("GOOGLE_ADS_DEVELOPER_TOKEN", self.developer_token),
                ("GOOGLE_ADS_CLIENT_ID", self.client_id),
                ("GOOGLE_ADS_CLIENT_SECRET", self.client_secret),
                ("GOOGLE_ADS_REFRESH_TOKEN", self.refresh_token),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Google Ads OAuth configuration is incomplete.",
                code="missing_google_ads_credentials",
                context={"missing": missing},
            )

    def to_google_ads_config(self) -> dict[str, object]:
        """Return the config dictionary expected by google-ads-python."""

        self.require_official_client_credentials()
        config: dict[str, object] = {
            "developer_token": self.developer_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "use_proto_plus": True,
        }
        if self.login_customer_id:
            config["login_customer_id"] = self.login_customer_id
        return config


def _int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _optional_customer_id(raw: str | None) -> str | None:
    if raw is None or raw.strip() == "":
        return None
    return normalize_customer_id(raw)


def _customer_ids_from_csv(raw: str | Iterable[str]) -> tuple[str, ...]:
    values = raw.split(",") if isinstance(raw, str) else raw
    return tuple(normalize_customer_id(value) for value in values if str(value).strip())


def resolve_default_api_version() -> str | None:
    """Resolve the Google Ads API version the gateway will use by default."""

    versions = supported_api_versions()
    if not versions:
        return None
    return versions[-1]


def supported_api_versions() -> tuple[str, ...]:
    """Return API versions packaged by the installed google-ads distribution."""

    try:
        import google.ads.googleads as googleads
    except ImportError:
        return ()

    versions = [
        module.name
        for module in pkgutil.iter_modules(googleads.__path__)
        if re.fullmatch(r"v\d+", module.name)
    ]
    return tuple(sorted(versions, key=lambda value: int(value.removeprefix("v"))))
