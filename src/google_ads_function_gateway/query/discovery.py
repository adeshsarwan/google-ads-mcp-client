"""Dedicated customer-discovery execution paths."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from google_ads_function_gateway.client.protocols import GoogleAdsClientWrapper
from google_ads_function_gateway.dto.validation import normalize_customer_id
from google_ads_function_gateway.exceptions import is_retryable_exception, normalize_exception
from google_ads_function_gateway.log import StructuredLogger
from google_ads_function_gateway.query.executor import RetryPolicy

T = TypeVar("T")


class CustomerDiscoveryExecutor:
    """Run approved discovery operations without granting reporting authorization."""

    def __init__(
        self,
        *,
        client: GoogleAdsClientWrapper,
        retry_policy: RetryPolicy | None = None,
        logger: StructuredLogger | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._retry_policy = retry_policy or RetryPolicy()
        self._logger = logger or StructuredLogger()
        self._sleeper = sleeper

    def search_manager_customer_clients(
        self,
        *,
        manager_customer_id: str,
        query: str,
        request_id: str,
        function: str,
    ) -> list[object]:
        discovery_root = normalize_customer_id(manager_customer_id)
        rows: list[object] = []
        page_token: str | None = None

        while True:
            current_page_token = page_token
            page = self._with_retry(
                lambda current_page_token=current_page_token: self._client.search(
                    customer_id=discovery_root,
                    query=query,
                    page_token=current_page_token,
                    request_id=request_id,
                ),
                request_id=request_id,
                function=function,
                customer_id=discovery_root,
                discovery_operation="manager_customer_discovery",
            )
            rows.extend(page.rows)
            page_token = page.next_page_token
            if not page_token:
                return rows

    def list_direct_accessible_customers(self, *, request_id: str, function: str) -> list[str]:
        resource_names = self._with_retry(
            lambda: self._client.list_accessible_customers(request_id=request_id),
            request_id=request_id,
            function=function,
            customer_id=None,
            discovery_operation="direct_accessible_customer_discovery",
        )
        return [
            normalize_customer_id(str(resource_name).rsplit("/", maxsplit=1)[-1])
            for resource_name in resource_names
        ]

    def _with_retry(
        self,
        run_operation: Callable[[], T],
        *,
        request_id: str,
        function: str,
        customer_id: str | None,
        discovery_operation: str,
    ) -> T:
        delay = self._retry_policy.initial_delay_seconds
        attempts = max(1, self._retry_policy.max_attempts)

        for attempt in range(1, attempts + 1):
            try:
                return run_operation()
            except Exception as exc:
                error = normalize_exception(exc)
                should_retry = attempt < attempts and is_retryable_exception(exc)
                self._logger.warning(
                    "google_ads_discovery_failed",
                    request_id=request_id,
                    function=function,
                    customer_id=customer_id,
                    discovery_operation=discovery_operation,
                    attempt=attempt,
                    retrying=should_retry,
                    error=error.to_dict(),
                )
                if not should_retry:
                    raise
                self._sleeper(delay)
                delay = min(
                    self._retry_policy.max_delay_seconds,
                    delay * self._retry_policy.multiplier,
                )

        raise AssertionError("retry loop exited unexpectedly")
