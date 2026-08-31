# Google Ads Function Gateway

Standalone, deterministic Google Ads function gateway for Google Ads Function Catalogue v1 Phase A and Phase B functions 01-06.

The gateway includes a stdio MCP server adapter, but it is intentionally not an MCP conversational client. MCP clients should call approved function names with validated parameters; they must not supply raw GAQL or construct Google Ads API logic at runtime.

## Architecture Rules

- No arbitrary GAQL endpoint.
- No GAQL supplied by users, CLI callers, or MCP clients.
- No runtime AI-generated Google Ads API code.
- Google Ads queries remain predefined and version-controlled.
- All reporting customer IDs require explicit authorization.
- No mutation/write operations.
- The CLI and MCP server use the same catalogue and function classes as future HTTP, cron, dashboard, and automation consumers.

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

## MCP Transports

The MCP server exposes only the six existing read-only catalogue functions through both stdio and Streamable HTTP. Both transports use the same MCP server object, tool handlers, and Google Ads Function Catalogue. They do not add conversational routing, caller-supplied GAQL, direct HTTP Google Ads calls, or write operations.

Install the project into the local virtual environment first:

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

### Local Stdio Mode

Configure a local MCP client to launch the stdio server from the project root. The client configuration should contain only the executable, arguments, working directory if supported, and non-secret runtime options:

```json
{
  "mcpServers": {
    "google-ads-function-gateway": {
      "command": "/absolute/path/to/google-ads-mcp-client/.venv/bin/python",
      "args": ["-m", "google_ads_function_gateway.mcp_server"],
      "cwd": "/absolute/path/to/google-ads-mcp-client"
    }
  }
}
```

### Local Streamable HTTP Mode

Streamable HTTP uses OAuth by default because it is the remote-capable mode intended
for ChatGPT Custom Apps. Set the OAuth environment in the server-side `.env` first:

```dotenv
GOOGLE_ADS_MCP_AUTH_MODE=oauth
GOOGLE_ADS_MCP_PUBLIC_HOST=googleads-mcp.thebesads.com
GOOGLE_ADS_MCP_PUBLIC_ORIGIN=https://googleads-mcp.thebesads.com
GOOGLE_ADS_MCP_OAUTH_DB=/var/lib/google-ads-mcp/oauth.db
GOOGLE_ADS_MCP_OWNER_USERNAME=replace-me
GOOGLE_ADS_MCP_OWNER_PASSWORD_HASH=replace-with-argon2id-hash
GOOGLE_ADS_MCP_OAUTH_SECRET=replace-with-at-least-32-random-chars
GOOGLE_ADS_MCP_ACCESS_TOKEN_TTL_SECONDS=3600
GOOGLE_ADS_MCP_AUTH_CODE_TTL_SECONDS=300
GOOGLE_ADS_MCP_REFRESH_TOKEN_TTL_SECONDS=2592000
```

Generate the owner password hash locally without printing the plaintext password:

```bash
python - <<'PY'
from argon2 import PasswordHasher
from getpass import getpass

print(PasswordHasher().hash(getpass("Owner password: ")))
PY
```

Start the Streamable HTTP MCP endpoint:

```bash
GOOGLE_ADS_MCP_HOST=127.0.0.1 GOOGLE_ADS_MCP_PORT=8000 \
  python -m google_ads_function_gateway.mcp_server --transport streamable-http
```

Default endpoint:

```text
http://127.0.0.1:8000/mcp
```

The server defaults to `127.0.0.1` for safety. OAuth discovery endpoints are exposed
by the same process:

```text
https://googleads-mcp.thebesads.com/.well-known/oauth-protected-resource/mcp
https://googleads-mcp.thebesads.com/.well-known/oauth-authorization-server
https://googleads-mcp.thebesads.com/oauth/authorize
https://googleads-mcp.thebesads.com/oauth/token
https://googleads-mcp.thebesads.com/oauth/register
https://googleads-mcp.thebesads.com/oauth/revoke
```

The OAuth server supports authorization code with PKCE S256 and refresh-token grant.
It does not support the implicit grant. The MCP resource scope is:

```text
google_ads.read
```

`offline_access` may be requested when the client needs refresh tokens.

OAuth owner approval happens on server-hosted login and approval pages. The password
stored in `.env` must be an Argon2id hash in `GOOGLE_ADS_MCP_OWNER_PASSWORD_HASH`;
do not put a plaintext owner password in `.env`.

For a local protocol-only smoke test without OAuth, explicitly choose the fallback
mode:

```bash
GOOGLE_ADS_MCP_AUTH_MODE=static_bearer \
  python -m google_ads_function_gateway.mcp_server --transport streamable-http
```

Example local MCP protocol smoke test in fallback mode:

```bash
curl -i http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-smoke","version":"0"}}}'
```

To use the legacy static bearer fallback, set both:

```dotenv
GOOGLE_ADS_MCP_AUTH_MODE=static_bearer
GOOGLE_ADS_MCP_AUTH_TOKEN=replace-with-a-long-random-token
```

Then add this header to MCP requests:

```bash
-H "Authorization: Bearer $GOOGLE_ADS_MCP_AUTH_TOKEN"
```

OAuth mode ignores `GOOGLE_ADS_MCP_AUTH_TOKEN`.

### Secret Handling

Do not place `GOOGLE_ADS_DEVELOPER_TOKEN`, OAuth client secrets, refresh tokens, or any other Google Ads credentials in MCP client JSON or remote connector settings. The MCP server uses the same `load_local_env()` mechanism as the CLI and loads the ignored `.env` file from the project root.

Google Ads credentials stay server-side and are never provided to ChatGPT. If you expose the Streamable HTTP endpoint through a secure tunnel or HTTPS reverse proxy, ChatGPT connects only to the MCP protocol endpoint and completes OAuth with this MCP service.

MCP stdio reserves stdout for JSON-RPC protocol messages. The MCP entrypoint does not print banners or debug output, and operational logging is configured for stderr.

### Tunnel Compatibility

The Streamable HTTP server is designed to sit behind a standard HTTPS reverse proxy or secure tunnel. Keep the local server bound to `127.0.0.1`, expose the MCP and OAuth paths over HTTPS, and ensure the proxy forwards request bodies and MCP headers unchanged. The application does not depend on a specific tunnel vendor.

The MCP Python SDK keeps DNS-rebinding protection enabled for Streamable HTTP. When deploying behind Cloudflare Tunnel or another HTTPS reverse proxy, set the public hostname so the forwarded `Host` header is explicitly allowed:

```dotenv
GOOGLE_ADS_MCP_PUBLIC_HOST=googleads-mcp.thebesads.com
```

Current production MCP endpoint:

```text
https://googleads-mcp.thebesads.com/mcp
```

The production origin allowlist is limited to `https://googleads-mcp.thebesads.com` when an `Origin` header is present.

For ChatGPT Business Custom Apps, configure:

- MCP server URL: `https://googleads-mcp.thebesads.com/mcp`
- Authentication: OAuth
- Scopes: `google_ads.read offline_access`

ChatGPT receives OAuth access and refresh tokens for this MCP server only. It does
not receive the Google Ads developer token, Google OAuth client secret, Google Ads
refresh token, or any other server-side Google Ads credential.

Dynamic client registration is enabled so ChatGPT can register its exact redirect
URI and use PKCE S256 during account linking.

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
- `GOOGLE_ADS_MCP_HOST` optional Streamable HTTP bind host, default `127.0.0.1`
- `GOOGLE_ADS_MCP_PORT` optional Streamable HTTP bind port, default `8000`
- `GOOGLE_ADS_MCP_PUBLIC_HOST` optional public HTTPS tunnel or reverse-proxy hostname allowed by MCP DNS-rebinding protection
- `GOOGLE_ADS_MCP_PUBLIC_ORIGIN` optional explicit OAuth issuer/origin; defaults to `https://GOOGLE_ADS_MCP_PUBLIC_HOST` when a public host is configured
- `GOOGLE_ADS_MCP_AUTH_MODE` optional Streamable HTTP auth mode, default `oauth`; set to `static_bearer` only for the legacy bearer-token fallback
- `GOOGLE_ADS_MCP_OAUTH_DB` SQLite OAuth persistence path, default `/var/lib/google-ads-mcp/oauth.db`
- `GOOGLE_ADS_MCP_OWNER_USERNAME` OAuth owner approval username
- `GOOGLE_ADS_MCP_OWNER_PASSWORD_HASH` Argon2id hash for the OAuth owner approval password
- `GOOGLE_ADS_MCP_ACCESS_TOKEN_TTL_SECONDS` OAuth access-token lifetime, default `3600`
- `GOOGLE_ADS_MCP_AUTH_CODE_TTL_SECONDS` OAuth authorization-code lifetime, default `300`
- `GOOGLE_ADS_MCP_REFRESH_TOKEN_TTL_SECONDS` OAuth refresh-token lifetime, default `2592000`
- `GOOGLE_ADS_MCP_OAUTH_SECRET` server-side HMAC secret for hashing OAuth tokens, authorization codes, client secrets, and owner sessions at rest
- `GOOGLE_ADS_MCP_AUTH_TOKEN` optional bearer token used only when `GOOGLE_ADS_MCP_AUTH_MODE=static_bearer`

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
