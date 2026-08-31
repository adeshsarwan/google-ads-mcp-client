# Google Ads Function Gateway

Standalone, deterministic Google Ads function gateway for Google Ads Function Catalogue v1 Phase A and Phase B functions 01-06.

The gateway is intentionally not an MCP conversational client. Future MCP clients should call approved function names with validated parameters; they must not supply raw GAQL or construct Google Ads API logic at runtime.

## Architecture Rules

- No arbitrary GAQL endpoint.
- No GAQL supplied by users, CLI callers, or future MCP clients.
- No runtime AI-generated Google Ads API code.
- Google Ads queries remain predefined and version-controlled.
- All reporting customer IDs require explicit authorization.
- No mutation/write operations.
- The CLI uses the same catalogue and function classes as future MCP, HTTP, cron, dashboard, and automation consumers.

## Local Setup

### A. Clone Repository

```bash
git clone https://github.com/adeshsarwan/google-ads-mcp-client.git
cd google-ads-mcp-client
```

### B. Create Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### C. Install Dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

This installs the official `google-ads` Python client, `python-dotenv`, and dev tooling including `ruff`.

### D. Create Local Environment File

```bash
cp .env.example .env
```

Never commit `.env`. It is ignored by Git.

### E. Configure Google Ads Credentials

Edit `.env` and set:

```dotenv
GOOGLE_ADS_DEVELOPER_TOKEN=replace-me
GOOGLE_ADS_CLIENT_ID=replace-me
GOOGLE_ADS_CLIENT_SECRET=replace-me
GOOGLE_ADS_REFRESH_TOKEN=replace-me
```

If you do not already have a refresh token, generate one locally:

```bash
python -m google_ads_function_gateway oauth-generate-refresh-token
```

The command reads `GOOGLE_ADS_CLIENT_ID` and `GOOGLE_ADS_CLIENT_SECRET` from `.env`, opens a local Google OAuth consent flow, and prints `GOOGLE_ADS_REFRESH_TOKEN=...` once. It does not write the token to disk.

This helper intentionally requests only the Google Ads scope and disables incremental authorization. That prevents unrelated scopes previously granted to the same Google Cloud project, such as Ad Manager, from being merged into this local Google Ads setup.

For Web Application OAuth clients, add the local loopback redirect URI in Google Cloud Console under Authorized redirect URIs. The default command uses:

```text
http://127.0.0.1:8080/
```

If you run the helper with `--port`, add the matching `http://127.0.0.1:<port>/` URI. At minimum, the redirect must use `http://127.0.0.1`; `localhost` is not the host used by this helper.

### F. Configure Login MCC

`GOOGLE_ADS_LOGIN_CUSTOMER_ID` is the manager/MCC account used as the discovery root:

```dotenv
GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890
```

The login customer ID is not the same as a reporting customer ID. It identifies the manager account context for Google Ads API access and account discovery.

### G. Run Doctor

```bash
python -m google_ads_function_gateway doctor
```

Doctor prints readiness statuses and package/API version details without printing secrets.

### H. Discover Accounts

```bash
python -m google_ads_function_gateway list-accounts
```

Discovery can list child accounts under the configured MCC even before those child accounts are in `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`. Discovery does not grant reporting authorization.

### I. Authorize Selected Customer IDs

Choose customer IDs from discovery output and add them to the allow-list:

```dotenv
GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=1112223333,4445556666
```

Reporting functions fail closed until the target customer ID is explicitly listed here.

### J. Run Standardized Functions

```bash
python -m google_ads_function_gateway get-account-details \
  --customer-id 1112223333

python -m google_ads_function_gateway list-campaigns \
  --customer-id 1112223333

python -m google_ads_function_gateway get-campaign-details \
  --customer-id 1112223333 \
  --campaign-id 987654321

python -m google_ads_function_gateway get-campaign-cost \
  --customer-id 1112223333 \
  --start-date 2026-08-30 \
  --end-date 2026-08-30

python -m google_ads_function_gateway get-campaign-performance \
  --customer-id 1112223333 \
  --start-date 2026-08-30 \
  --end-date 2026-08-30
```

Each command prints the normalized JSON envelope returned by the catalogue.

## Configuration

Supported environment variables:

- `GOOGLE_ADS_DEVELOPER_TOKEN`
- `GOOGLE_ADS_CLIENT_ID`
- `GOOGLE_ADS_CLIENT_SECRET`
- `GOOGLE_ADS_REFRESH_TOKEN`
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
- `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`
- `GOOGLE_ADS_API_VERSION` optional; when omitted, the gateway uses the highest API version packaged by `google-ads-python`
- `GOOGLE_ADS_RETRY_ATTEMPTS` optional, default `3`
- `GOOGLE_ADS_RUN_LIVE_TESTS` optional; set to `1` only when intentionally running live read-only tests

## Development Checks

```bash
python -m compileall src tests
python -m unittest discover -s tests
ruff check .
```

## Live Smoke Tests

Live tests are opt-in and read-only. They never run unless this flag is set:

```bash
GOOGLE_ADS_RUN_LIVE_TESTS=1 python -m unittest tests.integration.test_live_google_ads_smoke
```

The tests require valid credentials, a configured login MCC when using MCC discovery, and at least one explicitly allowed reporting customer ID.
