"""Fixed GAQL report execution with access checks, pagination, and retry."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, TypeVar

from google_ads_function_gateway.client.protocols import GoogleAdsClientWrapper, SearchPage
from google_ads_function_gateway.exceptions import is_retryable_exception, normalize_exception
from google_ads_function_gateway.log import StructuredLogger
from google_ads_function_gateway.security.access_policy import CustomerAccessPolicy


T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    multiplier: float = 2.0


class FixedGaqlExecutor:
    """Execute catalogue-owned GAQL only; callers never pass user-authored GAQL."""

    def __init__(
        self,
        *,
        client: GoogleAdsClientWrapper,
        access_policy: CustomerAccessPolicy,
        page_size: int = 1000,
        retry_policy: RetryPolicy | None = None,
        logger: StructuredLogger | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._access_policy = access_policy
        self._page_size = page_size
        self._retry_policy = retry_policy or RetryPolicy()
        self._logger = logger or StructuredLogger()
        self._sleeper = sleeper

    def search(self, *, customer_id: str, query: str, request_id: str, function: str) -> list[object]:
        self._access_policy.ensure_can_access_customer(customer_id)
        rows: list[object] = []
        page_token: str | None = None

        while True:
            page = self._with_retry(
                lambda: self._client.search(
                    customer_id=customer_id,
                    query=query,
                    page_token=page_token,
                    page_size=self._page_size,
                    request_id=request_id,
                ),
                request_id=request_id,
                function=function,
                customer_id=customer_id,
            )
            rows.extend(page.rows)
            page_token = page.next_page_token
            if not page_token:
                return rows

    def list_accessible_customers(self, *, request_id: str, function: str) -> list[str]:
        resource_names = self._with_retry(
            lambda: self._client.list_accessible_customers(request_id=request_id),
            request_id=request_id,
            function=function,
            customer_id=None,
        )
        customer_ids = []
        for resource_name in resource_names:
            text = str(resource_name)
            customer_ids.append(text.rsplit("/", maxsplit=1)[-1].replace("-", ""))
        return customer_ids

    def can_access_customer(self, customer_id: str) -> bool:
        return self._access_policy.can_access_customer(customer_id)

    def _with_retry(
        self,
        operation: Callable[[], T],
        *,
        request_id: str,
        function: str,
        customer_id: str | None,
    ) -> T:
        delay = self._retry_policy.initial_delay_seconds
        attempts = max(1, self._retry_policy.max_attempts)

        for attempt in range(1, attempts + 1):
            try:
                return operation()
            except Exception as exc:
                error = normalize_exception(exc)
                should_retry = attempt < attempts and is_retryable_exception(exc)
                self._logger.warning(
                    "google_ads_request_failed",
                    request_id=request_id,
                    function=function,
                    customer_id=customer_id,
                    attempt=attempt,
                    retrying=should_retry,
                    error=error.to_dict(),
                )
                if not should_retry:
                    raise
                self._sleeper(delay)
                delay = min(self._retry_policy.max_delay_seconds, delay * self._retry_policy.multiplier)

        raise AssertionError("retry loop exited unexpectedly")
