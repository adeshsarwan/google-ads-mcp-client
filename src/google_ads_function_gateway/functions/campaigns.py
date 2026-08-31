"""Campaign catalogue functions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google_ads_function_gateway.dto.response import response_meta
from google_ads_function_gateway.dto.validation import (
    DateRange,
    get_path,
    micros_to_units,
    normalize_campaign_ids,
    normalize_customer_id,
    normalize_enum,
    number_or_none,
    optional_enum,
    optional_text,
    require_campaign_id,
    require_customer_id,
    require_date_range,
)
from google_ads_function_gateway.exceptions import InputValidationError, normalize_exception
from google_ads_function_gateway.functions.base import GoogleAdsCatalogueFunction
from google_ads_function_gateway.query.gaql import (
    and_where,
    in_int_list,
    quote_gaql_string,
    status_filter,
)


@dataclass(frozen=True)
class CampaignFilterRequest:
    customer_id: str
    status: str | None = None
    campaign_ids: tuple[int, ...] = ()
    campaign_name_contains: str | None = None
    channel_type: str | None = None


@dataclass(frozen=True)
class CampaignDetailsRequest:
    customer_id: str
    campaign_id: int


@dataclass(frozen=True)
class CampaignMetricsRequest:
    customer_ids: tuple[str, ...]
    date_range: DateRange
    status: str | None = None
    campaign_ids: tuple[int, ...] = ()


class ListCampaignsFunction(GoogleAdsCatalogueFunction[CampaignFilterRequest]):
    function_name = "list_campaigns"

    def validate(self, params: dict[str, Any]) -> CampaignFilterRequest:
        return CampaignFilterRequest(
            customer_id=require_customer_id(params),
            status=optional_enum(params.get("status"), field_name="status"),
            campaign_ids=normalize_campaign_ids(params.get("campaign_ids")),
            campaign_name_contains=optional_text(
                params.get("campaign_name_contains"),
                field_name="campaign_name_contains",
            ),
            channel_type=optional_enum(params.get("channel_type"), field_name="channel_type"),
        )

    def run(
        self,
        request: CampaignFilterRequest,
        *,
        request_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows = self._executor.search(
            customer_id=request.customer_id,
            query=_list_campaigns_query(request),
            request_id=request_id,
            function=self.function_name,
        )
        campaigns = [_normalize_campaign_summary(row) for row in rows]
        return campaigns, response_meta(
            customer_ids=[request.customer_id],
            row_count=len(campaigns),
        )


class GetCampaignDetailsFunction(GoogleAdsCatalogueFunction[CampaignDetailsRequest]):
    function_name = "get_campaign_details"

    def validate(self, params: dict[str, Any]) -> CampaignDetailsRequest:
        return CampaignDetailsRequest(
            customer_id=require_customer_id(params),
            campaign_id=require_campaign_id(params),
        )

    def run(
        self,
        request: CampaignDetailsRequest,
        *,
        request_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            rows = self._executor.search(
                customer_id=request.customer_id,
                query=_campaign_details_query(request.campaign_id),
                request_id=request_id,
                function=self.function_name,
            )
        except Exception as exc:
            error = normalize_exception(exc)
            if error.category != "validation":
                raise
            rows = self._executor.search(
                customer_id=request.customer_id,
                query=_campaign_details_fallback_query(request.campaign_id),
                request_id=request_id,
                function=self.function_name,
            )
        if not rows:
            return {}, response_meta(customer_ids=[request.customer_id], row_count=0)

        details = _normalize_campaign_details(rows[0], fallback_customer_id=request.customer_id)
        return details, response_meta(
            customer_ids=[request.customer_id],
            currency_codes=[details.get("currency")],
            row_count=1,
        )


class GetCampaignCostFunction(GoogleAdsCatalogueFunction[CampaignMetricsRequest]):
    function_name = "get_campaign_cost"

    def validate(self, params: dict[str, Any]) -> CampaignMetricsRequest:
        return CampaignMetricsRequest(
            customer_ids=(require_customer_id(params),),
            date_range=require_date_range(params),
            status=optional_enum(params.get("status"), field_name="status"),
            campaign_ids=normalize_campaign_ids(params.get("campaign_ids")),
        )

    def run(
        self,
        request: CampaignMetricsRequest,
        *,
        request_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        customer_id = request.customer_ids[0]
        rows = self._executor.search(
            customer_id=customer_id,
            query=_campaign_cost_query(request),
            request_id=request_id,
            function=self.function_name,
        )
        costs = [_normalize_campaign_cost(row, fallback_customer_id=customer_id) for row in rows]
        return costs, response_meta(
            customer_ids=[customer_id],
            currency_codes=[row.get("currency_code") for row in costs],
            row_count=len(costs),
        )


class GetCampaignPerformanceFunction(GoogleAdsCatalogueFunction[CampaignMetricsRequest]):
    function_name = "get_campaign_performance"

    def validate(self, params: dict[str, Any]) -> CampaignMetricsRequest:
        customer_ids = _customer_ids_for_performance(params)
        return CampaignMetricsRequest(
            customer_ids=customer_ids,
            date_range=require_date_range(params),
            status=optional_enum(params.get("status"), field_name="status"),
            campaign_ids=normalize_campaign_ids(params.get("campaign_ids")),
        )

    def run(
        self,
        request: CampaignMetricsRequest,
        *,
        request_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        performance_rows: list[dict[str, Any]] = []
        for customer_id in request.customer_ids:
            rows = self._executor.search(
                customer_id=customer_id,
                query=_campaign_performance_query(request),
                request_id=request_id,
                function=self.function_name,
            )
            performance_rows.extend(
                _normalize_campaign_performance(row, fallback_customer_id=customer_id)
                for row in rows
            )

        return performance_rows, response_meta(
            customer_ids=list(request.customer_ids),
            currency_codes=[row.get("currency_code") for row in performance_rows],
            row_count=len(performance_rows),
        )


def _list_campaigns_query(request: CampaignFilterRequest) -> str:
    filters = [
        status_filter("campaign.status", request.status),
        in_int_list("campaign.id", request.campaign_ids),
        (
            f"campaign.name LIKE {quote_gaql_string('%' + request.campaign_name_contains + '%')}"
            if request.campaign_name_contains
            else None
        ),
        (
            f"campaign.advertising_channel_type = {request.channel_type}"
            if request.channel_type
            else None
        ),
    ]
    return f"""
SELECT
  campaign.id,
  campaign.name,
  campaign.status,
  campaign.advertising_channel_type,
  campaign.advertising_channel_sub_type
FROM campaign
{and_where(filters)}
ORDER BY campaign.id
""".strip()


def _campaign_details_query(campaign_id: int) -> str:
    return f"""
SELECT
  customer.id,
  customer.currency_code,
  campaign.id,
  campaign.name,
  campaign.status,
  campaign.advertising_channel_type,
  campaign.advertising_channel_sub_type,
  campaign.campaign_budget,
  campaign_budget.id,
  campaign_budget.amount_micros,
  campaign.bidding_strategy,
  campaign.bidding_strategy_type,
  campaign.target_cpa.target_cpa_micros,
  campaign.target_roas.target_roas
FROM campaign
WHERE campaign.id = {campaign_id}
LIMIT 1
""".strip()


def _campaign_details_fallback_query(campaign_id: int) -> str:
    return f"""
SELECT
  customer.id,
  customer.currency_code,
  campaign.id,
  campaign.name,
  campaign.status,
  campaign.advertising_channel_type,
  campaign.advertising_channel_sub_type,
  campaign.campaign_budget,
  campaign_budget.id,
  campaign_budget.amount_micros,
  campaign.bidding_strategy,
  campaign.bidding_strategy_type
FROM campaign
WHERE campaign.id = {campaign_id}
LIMIT 1
""".strip()


def _campaign_cost_query(request: CampaignMetricsRequest) -> str:
    filters = [
        f"segments.date BETWEEN {quote_gaql_string(request.date_range.start)} "
        f"AND {quote_gaql_string(request.date_range.end)}",
        status_filter("campaign.status", request.status),
        in_int_list("campaign.id", request.campaign_ids),
    ]
    return f"""
SELECT
  customer.id,
  customer.currency_code,
  campaign.id,
  campaign.name,
  campaign.status,
  segments.date,
  metrics.cost_micros
FROM campaign
{and_where(filters)}
ORDER BY segments.date, campaign.id
""".strip()


def _campaign_performance_query(request: CampaignMetricsRequest) -> str:
    filters = [
        f"segments.date BETWEEN {quote_gaql_string(request.date_range.start)} "
        f"AND {quote_gaql_string(request.date_range.end)}",
        status_filter("campaign.status", request.status),
        in_int_list("campaign.id", request.campaign_ids),
    ]
    return f"""
SELECT
  customer.id,
  customer.currency_code,
  campaign.id,
  campaign.name,
  campaign.status,
  metrics.impressions,
  metrics.clicks,
  metrics.cost_micros,
  metrics.conversions,
  metrics.conversions_value,
  metrics.ctr,
  metrics.average_cpc
FROM campaign
{and_where(filters)}
ORDER BY campaign.id
""".strip()


def _normalize_campaign_summary(row: Any) -> dict[str, Any]:
    return {
        "campaign_id": number_or_none(get_path(row, "campaign.id")),
        "campaign_name": get_path(row, "campaign.name"),
        "status": normalize_enum(get_path(row, "campaign.status")),
        "channel_type": normalize_enum(get_path(row, "campaign.advertising_channel_type")),
        "channel_sub_type": normalize_enum(get_path(row, "campaign.advertising_channel_sub_type")),
    }


def _normalize_campaign_details(row: Any, *, fallback_customer_id: str) -> dict[str, Any]:
    target_cpa_micros = get_path(row, "campaign.target_cpa.target_cpa_micros")
    target_roas = get_path(row, "campaign.target_roas.target_roas")
    daily_budget_micros = get_path(row, "campaign_budget.amount_micros")
    bidding_strategy_type = normalize_enum(get_path(row, "campaign.bidding_strategy_type"))
    return {
        "customer_id": _selected_customer_id(row, fallback_customer_id=fallback_customer_id),
        "campaign_id": number_or_none(get_path(row, "campaign.id")),
        "campaign_name": get_path(row, "campaign.name"),
        "status": normalize_enum(get_path(row, "campaign.status")),
        "channel_type": normalize_enum(get_path(row, "campaign.advertising_channel_type")),
        "channel_sub_type": normalize_enum(get_path(row, "campaign.advertising_channel_sub_type")),
        "budget_resource": get_path(row, "campaign.campaign_budget"),
        "budget_id": number_or_none(get_path(row, "campaign_budget.id")),
        "daily_budget_micros": number_or_none(daily_budget_micros),
        "daily_budget": micros_to_units(daily_budget_micros),
        "currency": get_path(row, "customer.currency_code"),
        "bidding_strategy": get_path(row, "campaign.bidding_strategy"),
        "bidding_strategy_type": bidding_strategy_type,
        "target_cpa_micros": _applicable_bidding_value(
            target_cpa_micros,
            bidding_strategy_type=bidding_strategy_type,
            applicable_strategy_type="TARGET_CPA",
            converter=number_or_none,
        ),
        "target_cpa": _applicable_bidding_value(
            target_cpa_micros,
            bidding_strategy_type=bidding_strategy_type,
            applicable_strategy_type="TARGET_CPA",
            converter=micros_to_units,
        ),
        "target_roas": _applicable_bidding_value(
            target_roas,
            bidding_strategy_type=bidding_strategy_type,
            applicable_strategy_type="TARGET_ROAS",
            converter=number_or_none,
        ),
    }


def _normalize_campaign_cost(row: Any, *, fallback_customer_id: str) -> dict[str, Any]:
    cost_micros = get_path(row, "metrics.cost_micros")
    return {
        "customer_id": _selected_customer_id(row, fallback_customer_id=fallback_customer_id),
        "campaign_id": number_or_none(get_path(row, "campaign.id")),
        "campaign_name": get_path(row, "campaign.name"),
        "status": normalize_enum(get_path(row, "campaign.status")),
        "date": get_path(row, "segments.date"),
        "currency_code": get_path(row, "customer.currency_code"),
        "cost_micros": number_or_none(cost_micros),
        "cost": micros_to_units(cost_micros),
    }


def _normalize_campaign_performance(row: Any, *, fallback_customer_id: str) -> dict[str, Any]:
    cost_micros = get_path(row, "metrics.cost_micros")
    cost = micros_to_units(cost_micros)
    conversions = number_or_none(get_path(row, "metrics.conversions"))
    cpa = cost / conversions if cost is not None and conversions and conversions > 0 else None
    return {
        "customer_id": _selected_customer_id(row, fallback_customer_id=fallback_customer_id),
        "campaign_id": number_or_none(get_path(row, "campaign.id")),
        "campaign_name": get_path(row, "campaign.name"),
        "status": normalize_enum(get_path(row, "campaign.status")),
        "impressions": number_or_none(get_path(row, "metrics.impressions")),
        "clicks": number_or_none(get_path(row, "metrics.clicks")),
        "cost_micros": number_or_none(cost_micros),
        "cost": cost,
        "conversions": conversions,
        "conversion_value": number_or_none(get_path(row, "metrics.conversions_value")),
        "ctr": number_or_none(get_path(row, "metrics.ctr")),
        "average_cpc": micros_to_units(get_path(row, "metrics.average_cpc")),
        "currency_code": get_path(row, "customer.currency_code"),
        "cpa": cpa,
    }


def _customer_ids_for_performance(params: dict[str, Any]) -> tuple[str, ...]:
    has_customer_id = "customer_id" in params
    has_customer_ids = "customer_ids" in params
    if has_customer_id and has_customer_ids:
        raise InputValidationError(
            "Provide either customer_id or customer_ids, not both.",
            code="ambiguous_customer_ids",
        )
    if has_customer_id:
        return (normalize_customer_id(params["customer_id"]),)
    if not has_customer_ids:
        raise InputValidationError(
            "customer_id or customer_ids is required.",
            code="missing_customer_ids",
        )
    raw_customer_ids = params["customer_ids"]
    if not isinstance(raw_customer_ids, list | tuple) or not raw_customer_ids:
        raise InputValidationError(
            "customer_ids must be a non-empty list.",
            code="invalid_customer_ids",
        )
    return tuple(
        dict.fromkeys(normalize_customer_id(customer_id) for customer_id in raw_customer_ids)
    )


def _selected_customer_id(row: Any, *, fallback_customer_id: str) -> str:
    customer_id = get_path(row, "customer.id", fallback_customer_id)
    if customer_id in (None, 0, "0"):
        customer_id = fallback_customer_id
    return normalize_customer_id(customer_id)


def _applicable_bidding_value(
    value: Any,
    *,
    bidding_strategy_type: str | None,
    applicable_strategy_type: str,
    converter: Any,
) -> Any:
    if bidding_strategy_type != applicable_strategy_type:
        return None
    return converter(value)
