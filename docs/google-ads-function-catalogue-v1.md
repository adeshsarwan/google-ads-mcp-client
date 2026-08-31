# Google Ads Function Catalogue v1

## Scope

This repository implements a standalone Google Ads Function Gateway for Catalogue v1 Phase A and read-only functions 01-06. It is a deterministic execution layer for MCP, HTTP, CLI, cron, dashboard, and automation callers.

The MCP or AI layer may decide which approved function to call and may provide validated parameters. It must never provide raw GAQL, mutate Google Ads entities, or generate production Google Ads API logic at runtime.

## Architecture

The implementation is a Python package under `src/google_ads_function_gateway`.

- `config.py`: centralized settings for OAuth, login customer ID, API version, retries, and customer allow-list.
- `client/`: fakeable Google Ads client protocol plus the official `google-ads-python` adapter.
- `auth/`: OAuth settings bridge.
- `security/`: customer access-policy abstractions. The default production policy is allow-list based and deny-all when no allow-list is configured.
- `query/`: deterministic GAQL helpers, authorized report execution, and dedicated discovery execution.
- `functions/`: approved function classes. GAQL lives inside these classes and helpers, never in caller input.
- `dto/`: response-envelope and validation helpers.
- `exceptions.py`: normalized public errors.
- `log.py`: structured JSON logging with credential redaction.
- `mcp_server.py`: stdio and Streamable HTTP MCP adapter for the approved read-only catalogue functions.

## Configuration

Environment variables for live Google Ads execution:

- `GOOGLE_ADS_DEVELOPER_TOKEN`
- `GOOGLE_ADS_CLIENT_ID`
- `GOOGLE_ADS_CLIENT_SECRET`
- `GOOGLE_ADS_REFRESH_TOKEN`
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
- `GOOGLE_ADS_API_VERSION` optional; if omitted, the gateway uses the highest API version packaged by `google-ads-python`
- `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` comma-separated allow-list
- `GOOGLE_ADS_RETRY_ATTEMPTS` optional, default `3`

Install the official Google Ads dependency and dev tooling:

```bash
python -m pip install -e ".[dev]"
```

## Authorization Behavior

Every customer-specific GAQL execution goes through `CustomerAccessPolicy.ensure_can_access_customer` before the API call is made.

The default `AllowListCustomerAccessPolicy` permits only customer IDs configured in `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`. If the allow-list is empty, all customer-specific calls fail closed.

Discovery and reporting use separate execution paths:

- `list_accounts` is discovery-only. It may inspect accounts reachable from the configured `GOOGLE_ADS_LOGIN_CUSTOMER_ID` even when the allow-list is empty.
- The configured login customer ID is trusted only as the discovery root. It is not a general reporting authorization bypass.
- Discovered child accounts are returned even when they are not yet present in `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`.
- Without a login customer ID, `list_accounts` calls `CustomerService.list_accessible_customers` and returns directly accessible customer IDs only.
- Reporting functions still call `FixedGaqlExecutor.search`, which fails closed unless the target customer ID is explicitly allow-listed.
- To authorize reporting, copy selected discovered customer IDs into `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`.

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
- `level`
- `discovery_source`
- `account_relationship`

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

The implementation first uses a predefined details query that includes target CPA and target ROAS fields. If Google Ads rejects that predefined query for API-version field compatibility, it retries once with a second predefined details query that excludes those optional bidding-scheme fields. It does not generate GAQL at runtime.

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

## MCP Invocation Rules

MCP clients should:

- Maintain an allow-list of function names exposed to the model.
- Validate parameters before calling the gateway.
- Send only the approved function name and JSON parameters.
- Preserve and forward `request_id` when available for traceability.
- Display normalized `error` details to users, not raw Google Ads exceptions.

MCP clients must never:

- Supply raw GAQL.
- Ask the model to generate GAQL for production execution.
- Expose arbitrary report execution.
- Expose write or mutation operations through this read-only catalogue.
- Bypass the customer access policy.

The stdio MCP server must be launched with a secret-free client configuration. Put only the executable, arguments, working directory if the client supports it, and non-secret runtime options in the MCP JSON. Google Ads developer tokens, OAuth client secrets, refresh tokens, and allow-list values remain in the ignored project-root `.env`.

The MCP server uses the same `load_local_env()` mechanism as the CLI. It loads the ignored local `.env` from the project root before constructing the catalogue, so credentials do not need to be duplicated into the MCP client configuration.

MCP stdio stdout is reserved for JSON-RPC protocol messages. Operational logs must go to stderr; the MCP entrypoint configures stdlib logging accordingly and must not print banners, debug output, or credential status messages to stdout.

The same MCP server also supports Streamable HTTP at `/mcp` by running:

```bash
python -m google_ads_function_gateway.mcp_server --transport streamable-http
```

The Streamable HTTP transport uses the same registered MCP tool handlers as stdio.
It must not add HTTP-specific GAQL, Google Ads API calls, catalogue authorization
checks, or mutation operations. By default it binds to `127.0.0.1:8000`.

Remote Streamable HTTP defaults to OAuth for ChatGPT and other MCP clients:

```dotenv
GOOGLE_ADS_MCP_AUTH_MODE=oauth
GOOGLE_ADS_MCP_PUBLIC_HOST=googleads-mcp.thebesads.com
GOOGLE_ADS_MCP_PUBLIC_ORIGIN=https://googleads-mcp.thebesads.com
GOOGLE_ADS_MCP_OAUTH_DB=/var/lib/google-ads-mcp/oauth.db
GOOGLE_ADS_MCP_OWNER_USERNAME=replace-me
GOOGLE_ADS_MCP_OWNER_PASSWORD_HASH=replace-with-argon2id-hash
GOOGLE_ADS_MCP_OAUTH_SECRET=replace-with-at-least-32-random-chars
```

The MCP OAuth layer exposes authorization code with PKCE S256, refresh-token grant,
dynamic client registration, token revocation, protected-resource metadata, and
authorization-server metadata. The only MCP resource scope is `google_ads.read`;
`offline_access` may be requested for refresh tokens. This OAuth layer authorizes
access to the MCP service only. It does not alter the Google Ads OAuth credentials,
Google Ads refresh token, fixed GAQL, or customer allow-list behavior.

For legacy local testing, `GOOGLE_ADS_MCP_AUTH_MODE=static_bearer` can enable the
older `GOOGLE_ADS_MCP_AUTH_TOKEN` transport gate. OAuth mode ignores that token.

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
- DONE: CLI entrypoint for standardized function invocation.
- DONE: Safe configuration doctor command.
- DONE: Stdio and Streamable HTTP MCP server adapter for the approved read-only functions.

Phase B:

- DONE: 01 `list_accounts`
- DONE: 02 `get_account_details`
- DONE: 03 `list_campaigns`
- DONE: 04 `get_campaign_details`
- DONE: 05 `get_campaign_cost`
- DONE: 06 `get_campaign_performance`

Stage 1B:

- DONE: Official `google-ads` Python client added as an application dependency.
- DONE: Dev install path added with `ruff`.
- DONE: `.env.example` added for local credential setup without committing secrets.
- DONE: `python -m google_ads_function_gateway doctor` added.
- DONE: CLI commands added for all six standardized functions.
- DONE: Bootstrap authorization bug fixed with a dedicated discovery executor.
- DONE: MCC discovery can run with an empty `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`.
- DONE: Reporting functions still fail closed until target customer IDs are allow-listed.
- DONE: Opt-in live read-only smoke tests added behind `GOOGLE_ADS_RUN_LIVE_TESTS=1`.
- DONE: Installed `google-ads` package version confirmed locally as `31.4.0`.
- DONE: Packaged Google Ads API versions confirmed locally as `v21`, `v22`, `v23`, `v24`, and `v25`; default gateway API version resolves to `v25`.

Stage 1B API Compatibility Notes:

- `list_accounts`: uses a predefined `customer_client` hierarchy query for MCC discovery.
- `get_account_details`: uses a predefined `customer` query.
- `list_campaigns`: uses predefined `campaign` summary fields and optional validated filters.
- `get_campaign_details`: uses predefined budget and bidding fields, with a second predefined fallback query for optional target CPA/target ROAS compatibility.
- `get_campaign_cost`: uses predefined `segments.date` and `metrics.cost_micros` fields; `cost = cost_micros / 1000000`.
- `get_campaign_performance`: uses predefined impressions, clicks, cost, conversions, conversion value, CTR, and average CPC fields; CPA returns `null` when conversions are zero.
- Live compatibility validation requires real credentials and `GOOGLE_ADS_RUN_LIVE_TESTS=1`; it was not attempted without credentials.

Partial/TODO:

- PARTIAL: Live Google Ads integration tests are scaffolded but skipped by default. They require real credentials, an allow-list, and `GOOGLE_ADS_RUN_LIVE_TESTS=1`.
- TODO: Add a cron, dashboard, or automation adapter when those surfaces are requested.
- TODO: Add write/mutation functions only through a separate approved catalogue phase.
