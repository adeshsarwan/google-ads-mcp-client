"""Function catalogue registry and default wiring."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from google_ads_function_gateway.client.google_ads import OfficialGoogleAdsClientWrapper
from google_ads_function_gateway.client.protocols import GoogleAdsClientWrapper
from google_ads_function_gateway.config import GoogleAdsSettings
from google_ads_function_gateway.dto.response import failure_envelope, new_request_id, response_meta
from google_ads_function_gateway.exceptions import InputValidationError, normalize_exception
from google_ads_function_gateway.functions import (
    GetAccountDetailsFunction,
    GetCampaignCostFunction,
    GetCampaignDetailsFunction,
    GetCampaignPerformanceFunction,
    ListAccountsFunction,
    ListCampaignsFunction,
)
from google_ads_function_gateway.functions.base import GoogleAdsCatalogueFunction
from google_ads_function_gateway.log import StructuredLogger
from google_ads_function_gateway.query.discovery import CustomerDiscoveryExecutor
from google_ads_function_gateway.query.executor import FixedGaqlExecutor, RetryPolicy
from google_ads_function_gateway.security.access_policy import (
    AllowListCustomerAccessPolicy,
    CustomerAccessPolicy,
)


class GoogleAdsFunctionCatalogue:
    """Registry for approved deterministic Google Ads functions."""

    def __init__(self, functions: Mapping[str, GoogleAdsCatalogueFunction[Any]]) -> None:
        self._functions = dict(functions)

    @classmethod
    def from_settings(
        cls,
        settings: GoogleAdsSettings | None = None,
        *,
        client: GoogleAdsClientWrapper | None = None,
        access_policy: CustomerAccessPolicy | None = None,
        logger: StructuredLogger | None = None,
    ) -> GoogleAdsFunctionCatalogue:
        settings = settings or GoogleAdsSettings.from_env()
        logger = logger or StructuredLogger()
        client = client or OfficialGoogleAdsClientWrapper(settings)
        access_policy = access_policy or AllowListCustomerAccessPolicy(
            settings.allowed_customer_ids
        )
        retry_policy = RetryPolicy(max_attempts=settings.retry_attempts)
        executor = FixedGaqlExecutor(
            client=client,
            access_policy=access_policy,
            retry_policy=retry_policy,
            logger=logger,
        )
        discovery_executor = CustomerDiscoveryExecutor(
            client=client,
            retry_policy=retry_policy,
            logger=logger,
        )
        functions = [
            ListAccountsFunction(
                executor=executor,
                discovery_executor=discovery_executor,
                logger=logger,
                manager_customer_id=settings.login_customer_id,
            ),
            GetAccountDetailsFunction(executor=executor, logger=logger),
            ListCampaignsFunction(executor=executor, logger=logger),
            GetCampaignDetailsFunction(executor=executor, logger=logger),
            GetCampaignCostFunction(executor=executor, logger=logger),
            GetCampaignPerformanceFunction(executor=executor, logger=logger),
        ]
        return cls({function.function_name: function for function in functions})

    def function_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._functions))

    def invoke(
        self,
        function_name: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        current_request_id = request_id or new_request_id()
        function = self._functions.get(function_name)
        if function is None:
            error = normalize_exception(
                InputValidationError(
                    f"Unknown Google Ads function: {function_name}",
                    code="unknown_function",
                )
            )
            return failure_envelope(
                function=function_name,
                request_id=current_request_id,
                error=error,
                meta=response_meta(),
            )
        return function.execute(params or {}, request_id=current_request_id)
