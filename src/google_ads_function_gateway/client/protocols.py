"""Protocols for fakeable Google Ads client wrappers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SearchPage:
    rows: Sequence[Any]
    next_page_token: str | None = None


class GoogleAdsClientWrapper(Protocol):
    def search(
        self,
        *,
        customer_id: str,
        query: str,
        page_token: str | None,
        request_id: str,
    ) -> SearchPage:
        """Execute one fixed Google Ads search page."""

    def list_accessible_customers(self, *, request_id: str) -> Sequence[str]:
        """Return accessible customer resource names or IDs."""
