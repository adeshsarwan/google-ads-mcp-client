"""Input validation and Google Ads row normalization helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from google_ads_function_gateway.exceptions import InputValidationError


def normalize_customer_id(value: Any) -> str:
    text = str(value).replace("-", "").replace(" ", "").strip()
    if not text or not text.isdigit():
        raise InputValidationError(
            "customer_id must contain digits only.",
            code="invalid_customer_id",
        )
    return text


def require_customer_id(params: dict[str, Any]) -> str:
    if "customer_id" not in params:
        raise InputValidationError("customer_id is required.", code="missing_customer_id")
    return normalize_customer_id(params["customer_id"])


def require_campaign_id(params: dict[str, Any]) -> int:
    if "campaign_id" not in params:
        raise InputValidationError("campaign_id is required.", code="missing_campaign_id")
    return normalize_campaign_id(params["campaign_id"])


def normalize_campaign_id(value: Any) -> int:
    try:
        campaign_id = int(value)
    except (TypeError, ValueError) as exc:
        raise InputValidationError(
            "campaign_id must be an integer.",
            code="invalid_campaign_id",
        ) from exc
    if campaign_id <= 0:
        raise InputValidationError("campaign_id must be positive.", code="invalid_campaign_id")
    return campaign_id


def normalize_campaign_ids(value: Any | None) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, Iterable) or isinstance(value, str | bytes):
        raise InputValidationError(
            "campaign_ids must be a list of positive integers.",
            code="invalid_campaign_ids",
        )
    ids = tuple(normalize_campaign_id(item) for item in value)
    return tuple(dict.fromkeys(ids))


def optional_enum(value: Any | None, *, field_name: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    normalized = str(value).strip().upper()
    if not normalized.replace("_", "").isalnum():
        raise InputValidationError(f"{field_name} is invalid.", code=f"invalid_{field_name}")
    return normalized


def optional_text(value: Any | None, *, field_name: str, max_length: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_length:
        raise InputValidationError(f"{field_name} is too long.", code=f"invalid_{field_name}")
    return text


def require_date_range(params: dict[str, Any]) -> DateRange:
    missing = [name for name in ("start_date", "end_date") if name not in params]
    if missing:
        raise InputValidationError(
            "start_date and end_date are required.",
            code="missing_date_range",
        )
    start = parse_iso_date(params["start_date"], field_name="start_date")
    end = parse_iso_date(params["end_date"], field_name="end_date")
    if start > end:
        raise InputValidationError(
            "start_date must be on or before end_date.",
            code="invalid_date_range",
        )
    return DateRange(start_date=start, end_date=end)


def parse_iso_date(value: Any, *, field_name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise InputValidationError(
            f"{field_name} must use YYYY-MM-DD format.",
            code=f"invalid_{field_name}",
        ) from exc


@dataclass(frozen=True)
class DateRange:
    start_date: date
    end_date: date

    @property
    def start(self) -> str:
        return self.start_date.isoformat()

    @property
    def end(self) -> str:
        return self.end_date.isoformat()


def get_path(source: Any, path: str, default: Any = None) -> Any:
    current = source
    for part in path.split("."):
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(part, default)
        else:
            current = getattr(current, part, default)
    return current


def normalize_enum(value: Any) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(value, str):
        return value
    return str(value)


def micros_to_units(value: Any) -> float | None:
    if value is None:
        return None
    micros = Decimal(int(value))
    return float(micros / Decimal("1000000"))


def number_or_none(value: Any) -> int | float | None:
    if value is None:
        return None
    try:
        if isinstance(value, float):
            return value
        if isinstance(value, int):
            return value
        text = str(value)
        return float(text) if "." in text else int(text)
    except (TypeError, ValueError):
        return None
