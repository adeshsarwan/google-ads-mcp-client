"""Normalized response envelope builders."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from google_ads_function_gateway.exceptions import NormalizedError


def new_request_id() -> str:
    return str(uuid4())


def success_envelope(
    *,
    function: str,
    request_id: str,
    data: Any,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": True,
        "function": function,
        "request_id": request_id,
        "data": data,
        "meta": _default_meta(meta),
        "error": None,
    }


def failure_envelope(
    *,
    function: str,
    request_id: str,
    error: NormalizedError,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "function": function,
        "request_id": request_id,
        "data": {},
        "meta": _default_meta(meta),
        "error": error.to_dict(),
    }


def response_meta(
    *,
    customer_ids: list[str] | tuple[str, ...] | None = None,
    currency_codes: list[str] | tuple[str, ...] | None = None,
    row_count: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "customer_ids": sorted(set(customer_ids or [])),
        "currency_codes": sorted(set(code for code in (currency_codes or []) if code)),
        "row_count": row_count,
    }
    meta.update(extra)
    return meta


def _default_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    base = {"customer_ids": [], "currency_codes": [], "row_count": 0}
    if meta:
        base.update(meta)
    return base
