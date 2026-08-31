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

## Optional Bearer Token

Set `GOOGLE_ADS_MCP_AUTH_TOKEN` to require a bearer token for Streamable HTTP requests:

```bash
GOOGLE_ADS_MCP_AUTH_TOKEN=replace-with-a-long-random-token \
  python -m google_ads_function_gateway.mcp_server --transport streamable-http
```

Clients must then send:

```text
Authorization: Bearer <GOOGLE_ADS_MCP_AUTH_TOKEN>
```

The token is checked at the HTTP transport layer before MCP tool execution. It is never passed into individual tools and is never logged.

## Smoke Test

Without bearer auth:

```bash
curl -i http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-smoke","version":"0"}}}'
```

With bearer auth:

```bash
curl -i http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $GOOGLE_ADS_MCP_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-smoke","version":"0"}}}'
```

## ChatGPT Via HTTPS Tunnel Or Reverse Proxy

ChatGPT needs a reachable HTTPS MCP endpoint. For personal use, run the local Streamable HTTP server on `127.0.0.1`, set `GOOGLE_ADS_MCP_AUTH_TOKEN`, and expose only the `/mcp` endpoint through a secure HTTPS tunnel or reverse proxy.

The tunnel/proxy should:

- terminate HTTPS
- forward the `/mcp` path to `http://127.0.0.1:8000/mcp`
- preserve HTTP method, body, and MCP headers
- preserve or intentionally set the public `Host` header configured in `GOOGLE_ADS_MCP_PUBLIC_HOST`
- pass the `Authorization` header through unchanged if bearer auth is enabled

The application does not depend on a specific tunnel vendor.

The MCP Python SDK validates `Host` and `Origin` headers for DNS-rebinding protection. Keep that protection enabled and add the public hostname to the server environment:

```dotenv
GOOGLE_ADS_MCP_PUBLIC_HOST=googleads-mcp.thebesads.com
```

For the current production tunnel, the MCP endpoint is:

```text
https://googleads-mcp.thebesads.com/mcp
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
```

Public port `8010` must remain closed. Cloudflare Tunnel should map:

```text
https://googleads-mcp.thebesads.com -> http://127.0.0.1:8010
```

The public MCP endpoint is:

```text
https://googleads-mcp.thebesads.com/mcp
```

Do not provide Google Ads developer tokens, OAuth client secrets, refresh tokens, or the full MCP bearer token to ChatGPT. ChatGPT should receive only the HTTPS MCP endpoint and, when enabled, the MCP bearer token required by the server.

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
