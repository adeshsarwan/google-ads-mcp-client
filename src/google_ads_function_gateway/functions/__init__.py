"""Read-only Google Ads function catalogue implementations."""

from google_ads_function_gateway.functions.accounts import (
    GetAccountDetailsFunction,
    ListAccountsFunction,
)
from google_ads_function_gateway.functions.campaigns import (
    GetCampaignCostFunction,
    GetCampaignDetailsFunction,
    GetCampaignPerformanceFunction,
    ListCampaignsFunction,
)

__all__ = [
    "GetAccountDetailsFunction",
    "GetCampaignCostFunction",
    "GetCampaignDetailsFunction",
    "GetCampaignPerformanceFunction",
    "ListAccountsFunction",
    "ListCampaignsFunction",
]
