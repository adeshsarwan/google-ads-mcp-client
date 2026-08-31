"""Customer access-policy abstractions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from google_ads_function_gateway.dto.validation import normalize_customer_id
from google_ads_function_gateway.exceptions import AuthorizationError


class CustomerAccessPolicy(Protocol):
    def can_access_customer(self, customer_id: str) -> bool:
        """Return whether the caller may access the normalized customer ID."""

    def ensure_can_access_customer(self, customer_id: str) -> None:
        """Raise when the caller may not access the normalized customer ID."""


class AllowListCustomerAccessPolicy:
    """Default production policy: only configured customer IDs are permitted."""

    def __init__(self, allowed_customer_ids: Iterable[str]) -> None:
        self._allowed = frozenset(
            normalize_customer_id(customer_id) for customer_id in allowed_customer_ids
        )

    def can_access_customer(self, customer_id: str) -> bool:
        return normalize_customer_id(customer_id) in self._allowed

    def ensure_can_access_customer(self, customer_id: str) -> None:
        normalized = normalize_customer_id(customer_id)
        if normalized not in self._allowed:
            raise AuthorizationError(context={"customer_id": normalized})


class PermissiveCustomerAccessPolicy:
    """Useful for local tests and explicitly trusted internal callers."""

    def can_access_customer(self, customer_id: str) -> bool:
        normalize_customer_id(customer_id)
        return True

    def ensure_can_access_customer(self, customer_id: str) -> None:
        normalize_customer_id(customer_id)
