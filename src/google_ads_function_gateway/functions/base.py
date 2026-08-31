"""Base class for normalized Google Ads catalogue functions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from google_ads_function_gateway.dto.response import (
    failure_envelope,
    new_request_id,
    response_meta,
    success_envelope,
)
from google_ads_function_gateway.exceptions import normalize_exception
from google_ads_function_gateway.log import StructuredLogger
from google_ads_function_gateway.query.executor import FixedGaqlExecutor


RequestT = TypeVar("RequestT")


class GoogleAdsCatalogueFunction(ABC, Generic[RequestT]):
    function_name: str

    def __init__(self, *, executor: FixedGaqlExecutor, logger: StructuredLogger | None = None) -> None:
        self._executor = executor
        self._logger = logger or StructuredLogger()

    def execute(self, params: dict[str, Any] | None, *, request_id: str | None = None) -> dict[str, Any]:
        current_request_id = request_id or new_request_id()
        try:
            request = self.validate(params or {})
            data, meta = self.run(request, request_id=current_request_id)
            return success_envelope(
                function=self.function_name,
                request_id=current_request_id,
                data=data,
                meta=meta,
            )
        except Exception as exc:
            error = normalize_exception(exc)
            self._logger.error(
                "catalogue_function_failed",
                request_id=current_request_id,
                function=self.function_name,
                error=error.to_dict(),
            )
            return failure_envelope(
                function=self.function_name,
                request_id=current_request_id,
                error=error,
                meta=response_meta(),
            )

    @abstractmethod
    def validate(self, params: dict[str, Any]) -> RequestT:
        """Validate raw caller parameters into a typed request."""

    @abstractmethod
    def run(self, request: RequestT, *, request_id: str) -> tuple[Any, dict[str, Any]]:
        """Execute the deterministic function and return data plus response metadata."""
