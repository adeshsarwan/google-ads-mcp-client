from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from google_ads_function_gateway.catalogue import GoogleAdsFunctionCatalogue
from google_ads_function_gateway.client.protocols import SearchPage
from google_ads_function_gateway.config import GoogleAdsSettings
from google_ads_function_gateway.security.access_policy import AllowListCustomerAccessPolicy


class FakeGoogleAdsClient:
    def __init__(
        self,
        *,
        search_pages_by_customer: dict[str, list[Any]] | None = None,
        side_effects_by_customer: dict[str, list[Any]] | None = None,
        accessible_customers: Sequence[str] = (),
    ) -> None:
        self.search_pages_by_customer = search_pages_by_customer or {}
        self.side_effects_by_customer = side_effects_by_customer or {}
        self.accessible_customers = tuple(accessible_customers)
        self.search_calls: list[dict[str, Any]] = []
        self.accessible_customer_calls = 0

    def search(
        self,
        *,
        customer_id: str,
        query: str,
        page_token: str | None,
        page_size: int,
        request_id: str,
    ) -> SearchPage:
        self.search_calls.append(
            {
                "customer_id": customer_id,
                "query": query,
                "page_token": page_token,
                "page_size": page_size,
                "request_id": request_id,
            }
        )
        side_effects = self.side_effects_by_customer.get(customer_id)
        if side_effects:
            effect = side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return _as_page(effect)

        pages = self.search_pages_by_customer.get(customer_id, [])
        if not pages:
            return SearchPage(rows=())
        page_index = int(page_token or "0")
        page = _as_page(pages[page_index])
        if page.next_page_token is not None:
            return page
        next_token = str(page_index + 1) if page_index + 1 < len(pages) else None
        return SearchPage(rows=page.rows, next_page_token=next_token)

    def list_accessible_customers(self, *, request_id: str) -> Sequence[str]:
        self.accessible_customer_calls += 1
        return self.accessible_customers


class FakeGoogleAdsException(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.error_code = code


def build_catalogue(
    fake_client: FakeGoogleAdsClient,
    *,
    allowed_customer_ids: Sequence[str],
    manager_customer_id: str | None = "9990001111",
) -> GoogleAdsFunctionCatalogue:
    settings = GoogleAdsSettings(
        login_customer_id=manager_customer_id,
        allowed_customer_ids=tuple(allowed_customer_ids),
        page_size=2,
        retry_attempts=2,
    )
    return GoogleAdsFunctionCatalogue.from_settings(
        settings,
        client=fake_client,
        access_policy=AllowListCustomerAccessPolicy(allowed_customer_ids),
    )


def _as_page(value: Any) -> SearchPage:
    if isinstance(value, SearchPage):
        return value
    return SearchPage(rows=tuple(value))
