"""Account catalogue functions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google_ads_function_gateway.dto.response import response_meta
from google_ads_function_gateway.dto.validation import (
    get_path,
    normalize_customer_id,
    normalize_enum,
    require_customer_id,
)
from google_ads_function_gateway.exceptions import InputValidationError
from google_ads_function_gateway.functions.base import GoogleAdsCatalogueFunction


LIST_ACCOUNTS_GAQL = """
SELECT
  customer_client.id,
  customer_client.client_customer,
  customer_client.descriptive_name,
  customer_client.currency_code,
  customer_client.time_zone,
  customer_client.status,
  customer_client.manager,
  customer_client.level
FROM customer_client
WHERE customer_client.hidden = false
ORDER BY customer_client.id
""".strip()

GET_ACCOUNT_DETAILS_GAQL = """
SELECT
  customer.id,
  customer.descriptive_name,
  customer.currency_code,
  customer.time_zone,
  customer.status,
  customer.manager
FROM customer
LIMIT 1
""".strip()


@dataclass(frozen=True)
class ListAccountsRequest:
    manager_customer_id: str | None


@dataclass(frozen=True)
class GetAccountDetailsRequest:
    customer_id: str


class ListAccountsFunction(GoogleAdsCatalogueFunction[ListAccountsRequest]):
    function_name = "list_accounts"

    def __init__(self, *, manager_customer_id: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._manager_customer_id = (
            normalize_customer_id(manager_customer_id) if manager_customer_id else None
        )

    def validate(self, params: dict[str, Any]) -> ListAccountsRequest:
        if params:
            raise InputValidationError(
                "list_accounts does not accept caller parameters.",
                code="unsupported_list_accounts_parameter",
                context={"parameters": sorted(params)},
            )
        return ListAccountsRequest(manager_customer_id=self._manager_customer_id)

    def run(
        self,
        request: ListAccountsRequest,
        *,
        request_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if request.manager_customer_id:
            rows = self._executor.search(
                customer_id=request.manager_customer_id,
                query=LIST_ACCOUNTS_GAQL,
                request_id=request_id,
                function=self.function_name,
            )
            accounts = [
                account
                for account in (_normalize_customer_client(row) for row in rows)
                if account["customer_id"] and self._executor.can_access_customer(account["customer_id"])
            ]
        else:
            customer_ids = self._executor.list_accessible_customers(
                request_id=request_id,
                function=self.function_name,
            )
            accounts = [
                {
                    "customer_id": customer_id,
                    "descriptive_name": None,
                    "currency_code": None,
                    "timezone": None,
                    "status": None,
                    "manager": None,
                }
                for customer_id in customer_ids
                if self._executor.can_access_customer(customer_id)
            ]

        return accounts, response_meta(
            customer_ids=[account["customer_id"] for account in accounts],
            currency_codes=[account.get("currency_code") for account in accounts],
            row_count=len(accounts),
        )


class GetAccountDetailsFunction(GoogleAdsCatalogueFunction[GetAccountDetailsRequest]):
    function_name = "get_account_details"

    def validate(self, params: dict[str, Any]) -> GetAccountDetailsRequest:
        return GetAccountDetailsRequest(customer_id=require_customer_id(params))

    def run(
        self,
        request: GetAccountDetailsRequest,
        *,
        request_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        rows = self._executor.search(
            customer_id=request.customer_id,
            query=GET_ACCOUNT_DETAILS_GAQL,
            request_id=request_id,
            function=self.function_name,
        )
        if not rows:
            return {}, response_meta(customer_ids=[request.customer_id], row_count=0)

        account = _normalize_customer(rows[0], fallback_customer_id=request.customer_id)
        return account, response_meta(
            customer_ids=[account["customer_id"]],
            currency_codes=[account.get("currency_code")],
            row_count=1,
        )


def _normalize_customer_client(row: Any) -> dict[str, Any]:
    customer_id = get_path(row, "customer_client.id")
    if customer_id is None:
        resource_name = get_path(row, "customer_client.client_customer")
        customer_id = str(resource_name).rsplit("/", maxsplit=1)[-1] if resource_name else None
    normalized_id = normalize_customer_id(customer_id) if customer_id is not None else None
    manager = get_path(row, "customer_client.manager")
    return {
        "customer_id": normalized_id,
        "descriptive_name": get_path(row, "customer_client.descriptive_name"),
        "currency_code": get_path(row, "customer_client.currency_code"),
        "timezone": get_path(row, "customer_client.time_zone"),
        "status": normalize_enum(get_path(row, "customer_client.status")),
        "manager": bool(manager) if manager is not None else None,
    }


def _normalize_customer(row: Any, *, fallback_customer_id: str) -> dict[str, Any]:
    manager = get_path(row, "customer.manager")
    return {
        "customer_id": normalize_customer_id(get_path(row, "customer.id", fallback_customer_id)),
        "descriptive_name": get_path(row, "customer.descriptive_name"),
        "currency_code": get_path(row, "customer.currency_code"),
        "timezone": get_path(row, "customer.time_zone"),
        "status": normalize_enum(get_path(row, "customer.status")),
        "manager": bool(manager) if manager is not None else None,
    }
