"""Command-line entrypoint for the standardized Google Ads function catalogue."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from google_ads_function_gateway.auth.oauth import (
    DEFAULT_LOOPBACK_REDIRECT_PORT,
    generate_google_ads_refresh_token,
)
from google_ads_function_gateway.catalogue import GoogleAdsFunctionCatalogue
from google_ads_function_gateway.config import GoogleAdsSettings
from google_ads_function_gateway.diagnostics import build_doctor_report
from google_ads_function_gateway.env import load_local_env
from google_ads_function_gateway.exceptions import ConfigurationError


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        print(json.dumps(build_doctor_report(), indent=2, sort_keys=True))
        return 0

    if args.command == "oauth-generate-refresh-token":
        return _run_oauth_generate_refresh_token(args)

    function_name, params = _function_call_from_args(args)
    catalogue = GoogleAdsFunctionCatalogue.from_settings()
    result = catalogue.invoke(function_name, params)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["success"] else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m google_ads_function_gateway",
        description="Invoke approved Google Ads Function Gateway functions.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Report local Google Ads configuration readiness.")
    oauth = subparsers.add_parser(
        "oauth-generate-refresh-token",
        help="Generate a Google Ads OAuth refresh token for local setup.",
    )
    oauth.add_argument(
        "--port",
        type=int,
        default=DEFAULT_LOOPBACK_REDIRECT_PORT,
        help="Local 127.0.0.1 callback port. Defaults to 8080.",
    )
    oauth.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser automatically.",
    )
    subparsers.add_parser("list-accounts", help="Discover accessible Google Ads accounts.")

    account_details = subparsers.add_parser("get-account-details")
    _add_customer_id(account_details)

    list_campaigns = subparsers.add_parser("list-campaigns")
    _add_customer_id(list_campaigns)
    _add_campaign_filters(list_campaigns, include_name_and_channel=True)

    campaign_details = subparsers.add_parser("get-campaign-details")
    _add_customer_id(campaign_details)
    campaign_details.add_argument("--campaign-id", required=True, type=int)

    campaign_cost = subparsers.add_parser("get-campaign-cost")
    _add_customer_id(campaign_cost)
    _add_date_range(campaign_cost)
    _add_campaign_filters(campaign_cost)

    campaign_performance = subparsers.add_parser("get-campaign-performance")
    customer = campaign_performance.add_mutually_exclusive_group(required=True)
    customer.add_argument("--customer-id")
    customer.add_argument("--customer-ids", nargs="+")
    _add_date_range(campaign_performance)
    _add_campaign_filters(campaign_performance)

    return parser


def _run_oauth_generate_refresh_token(args: argparse.Namespace) -> int:
    try:
        refresh_token = generate_google_ads_refresh_token(
            settings=GoogleAdsSettings.from_env(),
            port=args.port,
            open_browser=not args.no_browser,
        )
    except ConfigurationError as exc:
        missing = exc.context.get("missing")
        suffix = f" Missing: {', '.join(missing)}." if missing else ""
        print(f"OAuth setup error: {exc.public_message}{suffix}", file=sys.stderr)
        return 2

    print("Google Ads OAuth refresh token generated.")
    print()
    print("Paste this into .env:")
    print(f"GOOGLE_ADS_REFRESH_TOKEN={refresh_token}")
    print()
    print("The refresh token was printed once and was not written to disk.")
    return 0


def _function_call_from_args(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if args.command == "list-accounts":
        return "list_accounts", {}
    if args.command == "get-account-details":
        return "get_account_details", {"customer_id": args.customer_id}
    if args.command == "list-campaigns":
        return (
            "list_campaigns",
            {
                "customer_id": args.customer_id,
                **_optional_campaign_filter_params(args, include_name_and_channel=True),
            },
        )
    if args.command == "get-campaign-details":
        return (
            "get_campaign_details",
            {"customer_id": args.customer_id, "campaign_id": args.campaign_id},
        )
    if args.command == "get-campaign-cost":
        return (
            "get_campaign_cost",
            {
                "customer_id": args.customer_id,
                "start_date": args.start_date,
                "end_date": args.end_date,
                **_optional_campaign_filter_params(args),
            },
        )
    if args.command == "get-campaign-performance":
        return (
            "get_campaign_performance",
            {
                **_performance_customer_params(args),
                "start_date": args.start_date,
                "end_date": args.end_date,
                **_optional_campaign_filter_params(args),
            },
        )
    raise ValueError(f"Unsupported command: {args.command}")


def _add_customer_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--customer-id", required=True)


def _add_date_range(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)


def _add_campaign_filters(
    parser: argparse.ArgumentParser,
    *,
    include_name_and_channel: bool = False,
) -> None:
    parser.add_argument("--status")
    parser.add_argument("--campaign-ids", nargs="+", type=int)
    if include_name_and_channel:
        parser.add_argument("--campaign-name-contains")
        parser.add_argument("--channel-type")


def _optional_campaign_filter_params(
    args: argparse.Namespace,
    *,
    include_name_and_channel: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if getattr(args, "status", None):
        params["status"] = args.status
    if getattr(args, "campaign_ids", None):
        params["campaign_ids"] = args.campaign_ids
    if include_name_and_channel and getattr(args, "campaign_name_contains", None):
        params["campaign_name_contains"] = args.campaign_name_contains
    if include_name_and_channel and getattr(args, "channel_type", None):
        params["channel_type"] = args.channel_type
    return params


def _performance_customer_params(args: argparse.Namespace) -> dict[str, Any]:
    if args.customer_id:
        return {"customer_id": args.customer_id}
    return {"customer_ids": args.customer_ids}
