# Google Ads Function Catalogue v1

## Scope

This repository implements a standalone Google Ads Function Gateway for Catalogue v1 Phase A and read-only functions 01-06. It is a deterministic execution layer for future MCP, HTTP, CLI, cron, dashboard, and automation callers.

The MCP or AI layer may decide which approved function to call and may provide validated parameters. It must never provide raw GAQL, mutate Google Ads entities, or generate production Google Ads API logic at runtime.

## Architecture

The implementation is a Python package under `src/google_ads_function_gateway`.

- `config.py`: centralized settings for OAuth, login customer ID, API version, page size, retries, and customer allow-list.
- `client/`: fakeable Google Ads client protocol plus the official `google-ads-python` adapter.
- `auth/`: OAuth settings bridge.
- `security/`: customer access-policy abstractions. The default production policy is allow-list based and deny-all when no allow-list is configured.
- `query/`: deterministic GAQL helpers and `FixedGaqlExecutor`, which performs authorization, pagination, retry, and normalized API error handling.
- `functions/`: approved function classes. GAQL lives inside these classes and helpers, never in caller input.
- `dto/`: response-envelope and validation helpers.
- `exceptions.py`: normalized public errors.
- `log.py`: structured JSON logging with credential redaction.

## Configuration

Environment variables for live Google Ads execution:

- `GOOGLE_ADS_DEVELOPER_TOKEN`
- `GOOGLE_ADS_CLIENT_ID`
- `GOOGLE_ADS_CLIENT_SECRET`
- `GOOGLE_ADS_REFRESH_TOKEN`
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
- `GOOGLE_ADS_API_VERSION` optional; if omitted, the installed Google Ads library default is used
- `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` comma-separated allow-list
- `GOOGLE_ADS_PAGE_SIZE` optional, default `1000`
- `GOOGLE_ADS_RETRY_ATTEMPTS` optional, default `3`

Install the official Google Ads dependency for live calls:

```bash
python3 -m pip install -e ".[google-ads]"
```

## Authorization Behavior

Every customer-specific GAQL execution goes through `CustomerAccessPolicy.ensure_can_access_customer` before the API call is made.

The default `AllowListCustomerAccessPolicy` permits only customer IDs configured in `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`. If the allow-list is empty, all customer-specific calls fail closed.

`list_accounts` behaves as follows:

- With `GOOGLE_ADS_LOGIN_CUSTOMER_ID`, it queries `customer_client` through the configured manager account after the manager ID passes the access policy.
- Without a login customer ID, it calls `CustomerService.list_accessible_customers` and returns IDs only.
- In both modes, returned accounts are filtered through the access policy before being returned.

## Fixed-Query Principle

Callers never supply GAQL. Function parameters are validated and then applied only to predefined query templates owned by this repository. Unknown function names are rejected. No arbitrary GAQL execution endpoint exists.

## Response Envelope

Success:

```json
{
  "success": true,
  "function": "get_campaign_cost",
  "request_id": "req-123",
  "data": [],
  "meta": {
    "customer_ids": ["1234567890"],
    "currency_codes": ["USD"],
    "row_count": 0
  },
  "error": null
}
```

Failure:

```json
{
  "success": false,
  "function": "get_campaign_cost",
  "request_id": "req-123",
  "data": {},
  "meta": {
    "customer_ids": [],
    "currency_codes": [],
    "row_count": 0
  },
  "error": {
    "category": "authorization",
    "code": "unauthorized_customer",
    "message": "Customer access is not authorized.",
    "retryable": false
  }
}
```

Error categories include `validation`, `authorization`, `configuration`, `rate_limit`, `transient`, `google_ads_api`, and `internal`. Raw credentials, developer tokens, refresh tokens, and sensitive Google API internals are not exposed in responses or structured logs.

## Function Contracts

### 01 list_accounts

Purpose: return Google Ads customer accounts accessible through the configured MCC/account relationship.

Inputs:

```json
{}
```

Output rows:

- `customer_id`
- `descriptive_name`
- `currency_code`
- `timezone`
- `status`
- `manager`

Example:

```python
catalogue.invoke("list_accounts", {})
```

### 02 get_account_details

Required input:

```json
{ "customer_id": "1234567890" }
```

Output:

- `customer_id`
- `descriptive_name`
- `currency_code`
- `timezone`
- `status`
- `manager`

Example:

```python
catalogue.invoke("get_account_details", {"customer_id": "1234567890"})
```

### 03 list_campaigns

Required input:

```json
{ "customer_id": "1234567890" }
```

Optional inputs:

- `status`
- `campaign_ids`
- `campaign_name_contains`
- `channel_type`

Output rows:

- `campaign_id`
- `campaign_name`
- `status`
- `channel_type`
- `channel_sub_type`

Example:

```python
catalogue.invoke(
    "list_campaigns",
    {
        "customer_id": "1234567890",
        "status": "ENABLED",
        "campaign_ids": [111, 222],
        "campaign_name_contains": "Brand",
        "channel_type": "SEARCH"
    },
)
```

### 04 get_campaign_details

Required input:

```json
{
  "customer_id": "1234567890",
  "campaign_id": 111
}
```

Output:

- `customer_id`
- `campaign_id`
- `campaign_name`
- `status`
- `channel_type`
- `channel_sub_type`
- `budget_resource`
- `budget_id`
- `daily_budget_micros`
- `daily_budget`
- `currency`
- `bidding_strategy`
- `bidding_strategy_type`
- `target_cpa_micros`
- `target_cpa`
- `target_roas`

Non-applicable bidding fields return `null`; they do not fail the whole response.

Example:

```python
catalogue.invoke(
    "get_campaign_details",
    {"customer_id": "1234567890", "campaign_id": 111},
)
```

### 05 get_campaign_cost

Required input:

```json
{
  "customer_id": "1234567890",
  "start_date": "2026-08-01",
  "end_date": "2026-08-31"
}
```

Optional inputs:

- `status`
- `campaign_ids`

Output rows:

- `customer_id`
- `campaign_id`
- `campaign_name`
- `status`
- `date`
- `currency_code`
- `cost_micros`
- `cost`

Cost conversion is deterministic:

```text
cost = cost_micros / 1000000
```

Example:

```python
catalogue.invoke(
    "get_campaign_cost",
    {
        "customer_id": "1234567890",
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
        "campaign_ids": [111, 222]
    },
)
```

### 06 get_campaign_performance

Required input:

```json
{
  "customer_id": "1234567890",
  "start_date": "2026-08-01",
  "end_date": "2026-08-31"
}
```

Alternative multi-account input:

```json
{
  "customer_ids": ["1234567890", "9876543210"],
  "start_date": "2026-08-01",
  "end_date": "2026-08-31"
}
```

Optional inputs:

- `status`
- `campaign_ids`

Output rows:

- `customer_id`
- `campaign_id`
- `campaign_name`
- `status`
- `impressions`
- `clicks`
- `cost_micros`
- `cost`
- `conversions`
- `conversion_value`
- `ctr`
- `average_cpc`
- `currency_code`
- `cpa`

CPA is calculated only when `conversions > 0`; otherwise it returns `null`.

Example:

```python
catalogue.invoke(
    "get_campaign_performance",
    {
        "customer_ids": ["1234567890", "9876543210"],
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
        "status": "ENABLED"
    },
)
```

## Future MCP Invocation Rules

Future MCP clients should:

- Maintain an allow-list of function names exposed to the model.
- Validate parameters before calling the gateway.
- Send only the approved function name and JSON parameters.
- Preserve and forward `request_id` when available for traceability.
- Display normalized `error` details to users, not raw Google Ads exceptions.

Future MCP clients must never:

- Supply raw GAQL.
- Ask the model to generate GAQL for production execution.
- Expose arbitrary report execution.
- Expose write or mutation operations through this read-only catalogue.
- Bypass the customer access policy.

## Implementation Status

Phase A:

- DONE: Google Ads API client wrapper protocol and official adapter.
- DONE: OAuth/authentication configuration from environment variables.
- DONE: MCC/login-customer-id handling.
- DONE: Customer/account access-policy abstraction with default deny-all allow-list.
- DONE: Fixed GAQL report execution.
- DONE: Pagination handling.
- DONE: Retry handling for retryable transient/rate-limit errors.
- DONE: Rate-limit/error normalization.
- DONE: Standard normalized JSON response envelope.
- DONE: Structured logging with credential redaction.
- DONE: Unit-test structure and skipped live integration-test structure.

Phase B:

- DONE: 01 `list_accounts`
- DONE: 02 `get_account_details`
- DONE: 03 `list_campaigns`
- DONE: 04 `get_campaign_details`
- DONE: 05 `get_campaign_cost`
- DONE: 06 `get_campaign_performance`

Partial/TODO:

- PARTIAL: Live Google Ads integration tests are scaffolded but skipped by default. They require real credentials, an allow-list, and `GOOGLE_ADS_RUN_LIVE_TESTS=1`.
- TODO: Validate selected GAQL fields against the exact installed Google Ads API version during live integration setup.
- TODO: Add an HTTP, CLI, cron, dashboard, MCP, or automation adapter when those surfaces are requested.
- TODO: Add write/mutation functions only through a separate approved catalogue phase.
