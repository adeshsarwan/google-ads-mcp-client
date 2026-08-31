# MCP Integration

This project exposes the Google Ads Function Gateway through MCP without adding a second function implementation. Both transports use `google_ads_function_gateway.mcp_server.build_mcp_server()`, which registers the same six read-only tools over the existing Google Ads Function Catalogue.

## Tools

- `list_accounts`
- `get_account_details`
- `list_campaigns`
- `get_campaign_details`
- `get_campaign_cost`
- `get_campaign_performance`

No MCP transport exposes raw GAQL, arbitrary report execution, or mutation/write operations.

## Environment Loading

The MCP server uses the project `load_local_env()` mechanism before constructing the catalogue. Google Ads credentials and allow-list values remain in the ignored project-root `.env`.

Do not put these values in MCP client JSON or remote connector settings:

- `GOOGLE_ADS_DEVELOPER_TOKEN`
- `GOOGLE_ADS_CLIENT_ID`
- `GOOGLE_ADS_CLIENT_SECRET`
- `GOOGLE_ADS_REFRESH_TOKEN`
- any other Google Ads credential

Google Ads credentials stay server-side and are never provided to ChatGPT.

## Local Stdio

```bash
cd /absolute/path/to/google-ads-mcp-client
python -m google_ads_function_gateway.mcp_server
```

Example MCP client configuration:

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

## Local Streamable HTTP

```bash
cd /absolute/path/to/google-ads-mcp-client
GOOGLE_ADS_MCP_HOST=127.0.0.1 GOOGLE_ADS_MCP_PORT=8000 \
  python -m google_ads_function_gateway.mcp_server --transport streamable-http
```

Default endpoint:

```text
http://127.0.0.1:8000/mcp
```

The server binds to `127.0.0.1` by default. Do not bind to a public interface unless a deliberate deployment layer is handling HTTPS, authentication, and access control.

## OAuth For Remote MCP Clients

Streamable HTTP defaults to OAuth for remote clients such as ChatGPT Custom Apps:

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
GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS=0
```

Generate the owner password hash with Argon2id:

```bash
python - <<'PY'
from argon2 import PasswordHasher
from getpass import getpass

print(PasswordHasher().hash(getpass("Owner password: ")))
PY
```

The OAuth implementation is for ChatGPT-to-this-MCP-service authorization only.
It does not replace or modify Google Ads OAuth, the Google Ads refresh token, or any
Google Ads API authentication path.

Supported OAuth behavior:

- authorization code grant with PKCE S256
- refresh-token grant
- dynamic client registration at `/oauth/register`
- token revocation at `/oauth/revoke`
- protected resource metadata at `/.well-known/oauth-protected-resource/mcp`
- authorization server metadata at `/.well-known/oauth-authorization-server`
- MCP resource scope `google_ads.read`
- optional `offline_access` for refresh tokens
- anonymous `initialize`, `notifications/initialized`, and `tools/list` for
  ChatGPT action scanning
- OAuth-protected `tools/call` execution using `_meta["mcp/www_authenticate"]`
  challenges when a token is missing, invalid, or under-scoped

Unsupported OAuth behavior:

- implicit grant
- caller-supplied Google Ads credentials
- Google Ads mutation/write scopes
- arbitrary scopes beyond `google_ads.read` and `offline_access`

The owner approval flow uses server-hosted login and approval pages. The owner
password is never stored in plaintext; `.env` must contain only the Argon2id hash.
OAuth tokens, authorization codes, owner sessions, and confidential-client secrets
are stored hashed in SQLite.

This mixed-auth behavior is deliberate. ChatGPT Business Custom Apps scan MCP
tool descriptors before the owner OAuth login has occurred, so the six read-only
tool definitions must be visible without exposing Google Ads data. Google Ads
catalogue execution still requires a valid MCP OAuth token and preserves the
existing customer allow-list.

## Static Bearer Fallback

The old bearer-token transport gate remains available only as an explicit fallback:

```dotenv
GOOGLE_ADS_MCP_AUTH_MODE=static_bearer
GOOGLE_ADS_MCP_AUTH_TOKEN=replace-with-a-long-random-token
```

Set `GOOGLE_ADS_MCP_AUTH_TOKEN` to require a bearer token for Streamable HTTP requests:

```bash
GOOGLE_ADS_MCP_AUTH_MODE=static_bearer \
GOOGLE_ADS_MCP_AUTH_TOKEN=replace-with-a-long-random-token \
  python -m google_ads_function_gateway.mcp_server --transport streamable-http
```

Clients must then send:

```text
Authorization: Bearer <GOOGLE_ADS_MCP_AUTH_TOKEN>
```

The token is checked at the HTTP transport layer before MCP tool execution. It is never passed into individual tools and is never logged. OAuth mode ignores `GOOGLE_ADS_MCP_AUTH_TOKEN`.

## Smoke Tests

For static-bearer fallback without a token:

```bash
curl -i http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-smoke","version":"0"}}}'
```

For OAuth mode, unauthenticated `initialize` and `tools/list` are allowed so
ChatGPT can import actions before owner OAuth. An unauthenticated `tools/call`
returns a normal MCP response with `isError=true` plus
`_meta["mcp/www_authenticate"]` so the client can start OAuth:

```bash
curl -i https://googleads-mcp.thebesads.com/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"oauth-smoke","version":"0"}}}'
```

With static bearer auth:

```bash
curl -i http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $GOOGLE_ADS_MCP_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-smoke","version":"0"}}}'
```

## ChatGPT Via HTTPS Tunnel Or Reverse Proxy

ChatGPT needs a reachable HTTPS MCP endpoint. For personal use, run the local Streamable HTTP server on `127.0.0.1` and expose only the MCP and OAuth paths through a secure HTTPS tunnel or reverse proxy.

The tunnel/proxy should:

- terminate HTTPS
- forward `/mcp` to `http://127.0.0.1:8000/mcp`
- forward `/.well-known/oauth-protected-resource/mcp`
- forward `/.well-known/oauth-authorization-server`
- forward `/oauth/authorize`, `/oauth/token`, `/oauth/register`, and `/oauth/revoke`
- preserve HTTP method, body, and MCP headers
- preserve or intentionally set the public `Host` header configured in `GOOGLE_ADS_MCP_PUBLIC_HOST`
- pass the `Authorization` header through unchanged for OAuth access tokens or static bearer fallback

The application does not depend on a specific tunnel vendor.

The MCP Python SDK validates `Host` and `Origin` headers for DNS-rebinding protection. Keep that protection enabled and add the public hostname to the server environment:

```dotenv
GOOGLE_ADS_MCP_PUBLIC_HOST=googleads-mcp.thebesads.com
```

For the current production tunnel, the MCP endpoint is:

```text
https://googleads-mcp.thebesads.com/mcp
```

Configure ChatGPT Business Custom Apps with:

- MCP server URL: `https://googleads-mcp.thebesads.com/mcp`
- Authentication: OAuth
- Scopes: `google_ads.read offline_access`

Dynamic client registration is enabled so ChatGPT can register its exact redirect
URI and complete account linking with PKCE S256.

The ChatGPT action scan should show the same six tools even before owner login:
`list_accounts`, `get_account_details`, `list_campaigns`,
`get_campaign_details`, `get_campaign_cost`, and `get_campaign_performance`.

ChatGPT will discover:

```text
https://googleads-mcp.thebesads.com/.well-known/oauth-protected-resource/mcp
https://googleads-mcp.thebesads.com/.well-known/oauth-authorization-server
```

If a browser-style `Origin` header is present, only `https://googleads-mcp.thebesads.com` is allowed for this production host. Unrelated hosts are rejected before tool execution.

## VPS Deployment

The personal VPS deployment uses this layout:

- application path: `/opt/google-ads-mcp-client`
- Python environment: `/opt/google-ads-mcp-client/.venv`
- local environment file: `/opt/google-ads-mcp-client/.env`
- systemd service: `google-ads-mcp.service`
- service user: `googleadsmcp`
- local MCP endpoint: `http://127.0.0.1:8010/mcp`

The systemd service runs:

```bash
/opt/google-ads-mcp-client/.venv/bin/python -m google_ads_function_gateway.mcp_server --transport streamable-http
```

The service reads non-secret runtime settings and server-side Google Ads credentials from `/opt/google-ads-mcp-client/.env`. The `.env` file must remain ignored by Git and readable only by the service user.

The service must bind only to localhost:

```dotenv
GOOGLE_ADS_MCP_HOST=127.0.0.1
GOOGLE_ADS_MCP_PORT=8010
GOOGLE_ADS_MCP_PUBLIC_HOST=googleads-mcp.thebesads.com
GOOGLE_ADS_MCP_PUBLIC_ORIGIN=https://googleads-mcp.thebesads.com
GOOGLE_ADS_MCP_AUTH_MODE=oauth
GOOGLE_ADS_MCP_OAUTH_DB=/var/lib/google-ads-mcp/oauth.db
GOOGLE_ADS_MCP_OWNER_USERNAME=replace-me
GOOGLE_ADS_MCP_OWNER_PASSWORD_HASH=replace-with-argon2id-hash
GOOGLE_ADS_MCP_OAUTH_SECRET=replace-with-at-least-32-random-chars
GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS=0
```

Public port `8010` must remain closed. Cloudflare Tunnel should map:

```text
https://googleads-mcp.thebesads.com -> http://127.0.0.1:8010
```

The public MCP endpoint is:

```text
https://googleads-mcp.thebesads.com/mcp
```

Do not provide Google Ads developer tokens, Google OAuth client secrets, Google Ads refresh tokens, or the server-side MCP OAuth secret to ChatGPT. ChatGPT should receive only the HTTPS MCP endpoint and complete OAuth against this MCP service.

Basic service operations:

```bash
sudo systemctl status google-ads-mcp
sudo systemctl restart google-ads-mcp
sudo journalctl -u google-ads-mcp -f
```

Application update workflow:

```bash
cd /opt/google-ads-mcp-client
sudo -u googleadsmcp git fetch origin
sudo -u googleadsmcp git checkout main
sudo -u googleadsmcp git pull --ff-only
sudo -u googleadsmcp .venv/bin/python -m pip install -e .
sudo systemctl restart google-ads-mcp
```

## Logging

Stdio stdout is reserved for JSON-RPC protocol messages. Streamable HTTP responses are MCP protocol responses. Operational logs go to stderr and must not include Google Ads credentials, OAuth secrets, refresh tokens, or MCP bearer tokens.

For short diagnostic windows, set `GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS=1` and restart
the service. The diagnostic log contains only path, HTTP status, MCP method,
request id, and duration; it does not log Authorization headers, OAuth tokens,
Google Ads credentials, request parameters, or response data.
