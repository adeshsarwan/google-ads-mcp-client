"""Official google-ads-python client adapter."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from google_ads_function_gateway.client.protocols import SearchPage
from google_ads_function_gateway.config import GoogleAdsSettings
from google_ads_function_gateway.exceptions import ConfigurationError


class OfficialGoogleAdsClientWrapper:
    """Lazy adapter around google.ads.googleads.client.GoogleAdsClient."""

    def __init__(self, settings: GoogleAdsSettings) -> None:
        self._settings = settings
        self._client: Any | None = None

    def search(
        self,
        *,
        customer_id: str,
        query: str,
        page_token: str | None,
        page_size: int,
        request_id: str,
    ) -> SearchPage:
        client = self._get_client()
        service = self._get_service(client, "GoogleAdsService")
        request = self._get_type(client, "SearchGoogleAdsRequest")
        request.customer_id = customer_id
        request.query = query
        request.page_size = page_size
        if page_token:
            request.page_token = page_token

        response = service.search(request=request)
        rows = list(getattr(response, "results", response))
        next_page_token = getattr(response, "next_page_token", None) or None
        return SearchPage(rows=rows, next_page_token=next_page_token)

    def list_accessible_customers(self, *, request_id: str) -> Sequence[str]:
        client = self._get_client()
        service = self._get_service(client, "CustomerService")
        response = service.list_accessible_customers()
        return tuple(getattr(response, "resource_names", response))

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        try:
            from google.ads.googleads.client import GoogleAdsClient
        except ImportError as exc:
            raise ConfigurationError(
                "The google-ads package is required for live API execution.",
                code="missing_google_ads_dependency",
            ) from exc

        config = self._settings.to_google_ads_config()
        kwargs = {}
        if self._settings.api_version:
            kwargs["version"] = self._settings.api_version
        self._client = GoogleAdsClient.load_from_dict(config, **kwargs)
        return self._client

    def _get_service(self, client: Any, service_name: str) -> Any:
        if self._settings.api_version:
            return client.get_service(service_name, version=self._settings.api_version)
        return client.get_service(service_name)

    def _get_type(self, client: Any, type_name: str) -> Any:
        if self._settings.api_version:
            return client.get_type(type_name, version=self._settings.api_version)
        return client.get_type(type_name)
