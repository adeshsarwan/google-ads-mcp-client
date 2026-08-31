"""Gateway exception and error-normalization types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedError:
    category: str
    code: str
    message: str
    retryable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class GatewayError(Exception):
    """Base exception with public, credential-safe error details."""

    category = "internal"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str,
        category: str | None = None,
        retryable: bool | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.public_message = message
        self.code = code
        if category is not None:
            self.category = category
        if retryable is not None:
            self.retryable = retryable
        self.context = context or {}

    def normalized(self) -> NormalizedError:
        return NormalizedError(
            category=self.category,
            code=self.code,
            message=self.public_message,
            retryable=self.retryable,
        )


class InputValidationError(GatewayError):
    category = "validation"

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_input",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, context=context)


class AuthorizationError(GatewayError):
    category = "authorization"

    def __init__(
        self,
        message: str = "Customer access is not authorized.",
        *,
        code: str = "unauthorized_customer",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, context=context)


class ConfigurationError(GatewayError):
    category = "configuration"

    def __init__(
        self,
        message: str,
        *,
        code: str = "configuration_error",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, context=context)


class GoogleAdsApiError(GatewayError):
    category = "google_ads_api"

    def __init__(
        self,
        message: str = "Google Ads API request failed.",
        *,
        code: str = "google_ads_api_error",
        category: str = "google_ads_api",
        retryable: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            category=category,
            retryable=retryable,
            context=context,
        )


_RETRYABLE_TOKENS = (
    "RESOURCE_EXHAUSTED",
    "UNAVAILABLE",
    "DEADLINE_EXCEEDED",
    "INTERNAL",
    "TRANSIENT",
    "TEMPORARY",
    "TIMEOUT",
    "RATE",
    "QUOTA",
)

_AUTH_TOKENS = (
    "AUTHENTICATION",
    "AUTHORIZATION",
    "PERMISSION",
    "ACCESS",
    "USER_PERMISSION_DENIED",
)

_VALIDATION_TOKENS = ("INVALID", "MALFORMED", "POLICY_FINDING", "REQUIRED")


def normalize_exception(exc: Exception) -> NormalizedError:
    """Convert arbitrary exceptions into public error details."""

    if isinstance(exc, GatewayError):
        return exc.normalized()

    code = _extract_google_ads_error_code(exc) or exc.__class__.__name__
    upper_code = code.upper()
    message = "Google Ads API request failed."
    category = "google_ads_api"
    retryable = any(token in upper_code for token in _RETRYABLE_TOKENS)

    if any(token in upper_code for token in _AUTH_TOKENS):
        category = "authorization"
        message = "Google Ads API authorization failed."
    elif any(token in upper_code for token in _VALIDATION_TOKENS):
        category = "validation"
        message = "Google Ads API rejected the request."
    elif retryable:
        category = (
            "rate_limit"
            if any(t in upper_code for t in ("RATE", "QUOTA", "RESOURCE_EXHAUSTED"))
            else "transient"
        )
        message = "Google Ads API request failed with a retryable error."

    return NormalizedError(category=category, code=code, message=message, retryable=retryable)


def is_retryable_exception(exc: Exception) -> bool:
    return normalize_exception(exc).retryable


def _extract_google_ads_error_code(exc: Exception) -> str | None:
    """Best-effort extraction without importing google-ads at module import time."""

    failure = getattr(exc, "failure", None)
    errors = getattr(failure, "errors", None)
    if errors:
        first = errors[0]
        error_code = getattr(first, "error_code", None)
        if error_code is not None:
            code_name = _enum_code_name(error_code)
            if code_name:
                return code_name

    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if status_code:
            return f"http_{status_code}"

    name = getattr(exc, "error_code", None) or getattr(exc, "code", None)
    if name is not None:
        return str(name)
    return None


def _enum_code_name(error_code: object) -> str | None:
    for attr in dir(error_code):
        if attr.startswith("_"):
            continue
        value = getattr(error_code, attr, None)
        name = getattr(value, "name", None)
        if name:
            return str(name)
        if value:
            return attr
    return None
