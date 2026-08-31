"""Stdio MCP adapter for the read-only Google Ads function catalogue."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import mcp_types as mcp_types
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from google_ads_function_gateway.catalogue import GoogleAdsFunctionCatalogue
from google_ads_function_gateway.env import load_local_env
from google_ads_function_gateway.exceptions import ConfigurationError
from google_ads_function_gateway.mcp_oauth import READ_SCOPE, McpOAuthServer, McpOAuthSettings

SERVER_NAME = "google-ads-function-gateway"
DEFAULT_STREAMABLE_HTTP_HOST = "127.0.0.1"
DEFAULT_STREAMABLE_HTTP_PORT = 8000
DEFAULT_STREAMABLE_HTTP_PATH = "/mcp"
GOOGLE_ADS_MCP_PUBLIC_HOST_ENV_VAR = "GOOGLE_ADS_MCP_PUBLIC_HOST"
GOOGLE_ADS_MCP_PUBLIC_ORIGIN_ENV_VAR = "GOOGLE_ADS_MCP_PUBLIC_ORIGIN"
GOOGLE_ADS_MCP_AUTH_MODE_ENV_VAR = "GOOGLE_ADS_MCP_AUTH_MODE"
DEFAULT_HTTP_AUTH_MODE = "oauth"
STATIC_BEARER_AUTH_MODE = "static_bearer"
OAUTH_AUTH_MODE = "oauth"
SUPPORTED_HTTP_AUTH_MODES = (OAUTH_AUTH_MODE, STATIC_BEARER_AUTH_MODE)
SUPPORTED_TRANSPORTS = ("stdio", "streamable-http")

READ_ONLY_TOOL_ANNOTATIONS = mcp_types.ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
OAUTH_TOOL_META = {
    "securitySchemes": [
        {
            "type": "oauth2",
            "scopes": [READ_SCOPE],
        }
    ]
}


@dataclass(frozen=True)
class McpRuntimeSettings:
    transport: str = "stdio"
    host: str = DEFAULT_STREAMABLE_HTTP_HOST
    port: int = DEFAULT_STREAMABLE_HTTP_PORT
    path: str = DEFAULT_STREAMABLE_HTTP_PATH
    auth_token: str | None = None
    public_host: str | None = None
    public_origin: str | None = None
    auth_mode: str = DEFAULT_HTTP_AUTH_MODE

    @classmethod
    def from_env(
        cls,
        *,
        transport: str,
        host: str | None = None,
        port: int | None = None,
        path: str | None = None,
    ) -> McpRuntimeSettings:
        resolved_host = host or os.getenv("GOOGLE_ADS_MCP_HOST") or DEFAULT_STREAMABLE_HTTP_HOST
        resolved_port = port or _port_from_env()
        resolved_path = _normalize_http_path(path or DEFAULT_STREAMABLE_HTTP_PATH)
        configured_public_origin = _normalize_public_origin(
            _optional_env(GOOGLE_ADS_MCP_PUBLIC_ORIGIN_ENV_VAR)
        )
        public_host = _normalize_public_host(
            _optional_env(GOOGLE_ADS_MCP_PUBLIC_HOST_ENV_VAR)
        ) or _public_host_from_origin(configured_public_origin)
        public_origin = configured_public_origin or _default_public_origin(
            public_host=public_host,
            host=resolved_host,
            port=resolved_port,
        )
        resolved_auth_mode = (
            _auth_mode_from_env() if transport == "streamable-http" else DEFAULT_HTTP_AUTH_MODE
        )
        return cls(
            transport=transport,
            host=resolved_host,
            port=resolved_port,
            path=resolved_path,
            auth_token=_optional_env("GOOGLE_ADS_MCP_AUTH_TOKEN"),
            public_host=public_host,
            public_origin=public_origin,
            auth_mode=resolved_auth_mode,
        )

    @property
    def endpoint_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    def transport_security_settings(self) -> TransportSecuritySettings:
        return build_transport_security_settings(public_host=self.public_host, port=self.port)

    def oauth_settings(self) -> McpOAuthSettings:
        if not self.public_origin:
            raise ConfigurationError(
                "GOOGLE_ADS_MCP_PUBLIC_ORIGIN or GOOGLE_ADS_MCP_PUBLIC_HOST is required "
                "when GOOGLE_ADS_MCP_AUTH_MODE=oauth.",
                code="missing_mcp_oauth_public_origin",
            )
        return McpOAuthSettings.from_env(
            public_origin=self.public_origin,
            resource_url=f"{self.public_origin}{self.path}",
        )


class BearerTokenAuthMiddleware:
    """Simple bearer-token gate for personal Streamable HTTP deployments."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected_authorization = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        authorization = _header_value(scope, b"authorization")
        if authorization and hmac.compare_digest(authorization, self._expected_authorization):
            await self.app(scope, receive, send)
            return

        await _send_auth_error(send)


class HostOriginSecurityMiddleware:
    """Apply MCP SDK Host/Origin checks to every HTTP route on the app."""

    def __init__(self, app: ASGIApp, settings: TransportSecuritySettings) -> None:
        self.app = app
        self._security = TransportSecurityMiddleware(settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response = await self._security.validate_request(Request(scope, receive), is_post=False)
        if response is not None:
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class OAuthChallengeScopeMiddleware:
    """Add the required MCP scope to SDK OAuth challenges for client discovery."""

    def __init__(self, app: ASGIApp, required_scope: str) -> None:
        self.app = app
        self._scope_fragment = f', scope="{required_scope}"'.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_scope(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] in {401, 403}:
                message = dict(message)
                headers = list(message.get("headers", []))
                message["headers"] = [
                    self._challenge_with_scope(name, value)
                    for name, value in headers
                ]
            await send(message)

        await self.app(scope, receive, send_with_scope)

    def _challenge_with_scope(self, name: bytes, value: bytes) -> tuple[bytes, bytes]:
        if name.lower() != b"www-authenticate":
            return name, value
        if not value.startswith(b"Bearer") or b"scope=" in value:
            return name, value
        return name, value + self._scope_fragment


def main(argv: list[str] | None = None) -> int:
    configure_stdio_logging()
    load_local_env()
    args = _build_parser().parse_args(argv)
    try:
        runtime_settings = McpRuntimeSettings.from_env(
            transport=args.transport,
            host=args.host,
            port=args.port,
        )
        oauth_server = _oauth_server_for_runtime(runtime_settings)
    except ConfigurationError as exc:
        print(f"MCP setup error: {exc.public_message}", file=sys.stderr)
        return 2

    server = build_mcp_server(oauth_server=oauth_server)
    if runtime_settings.transport == "stdio":
        server.run("stdio")
    else:
        run_streamable_http_server(server, runtime_settings)
    return 0


def configure_stdio_logging(level: int = logging.WARNING) -> None:
    """Route operational logs away from stdout, which is reserved for MCP stdio."""

    logging.basicConfig(level=level, stream=sys.stderr, force=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m google_ads_function_gateway.mcp_server",
        description="Run the Google Ads Function Gateway MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=SUPPORTED_TRANSPORTS,
        default="stdio",
        help="MCP transport to run. Defaults to stdio.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Streamable HTTP bind host. Defaults to GOOGLE_ADS_MCP_HOST or 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        default=None,
        type=int,
        help="Streamable HTTP bind port. Defaults to GOOGLE_ADS_MCP_PORT or 8000.",
    )
    return parser


def build_mcp_server(
    catalogue: GoogleAdsFunctionCatalogue | None = None,
    oauth_server: McpOAuthServer | None = None,
) -> MCPServer:
    """Build a print-free MCP server over the existing catalogue."""

    load_local_env()
    active_catalogue = catalogue or GoogleAdsFunctionCatalogue.from_settings()
    auth_kwargs: dict[str, Any] = {}
    if oauth_server is not None:
        auth_kwargs = {
            "auth": oauth_server.settings.auth_settings(),
            "token_verifier": oauth_server,
        }
    server = MCPServer(
        name=SERVER_NAME,
        title="Google Ads Function Gateway",
        description="Read-only deterministic Google Ads Function Catalogue v1 tools.",
        instructions=(
            "Call only the registered read-only catalogue tools. Do not send raw GAQL, "
            "request mutations, or bypass customer allow-list authorization."
        ),
        version="0.1.0",
        log_level="WARNING",
        **auth_kwargs,
    )

    @server.tool(
        name="list_accounts",
        description="Discover accessible Google Ads accounts using the configured MCC context.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        meta=OAUTH_TOOL_META if oauth_server is not None else None,
    )
    def list_accounts() -> mcp_types.CallToolResult:
        return invoke_catalogue_tool(active_catalogue, "list_accounts", {})

    @server.tool(
        name="get_account_details",
        description="Return details for one explicitly allow-listed Google Ads customer.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        meta=OAUTH_TOOL_META if oauth_server is not None else None,
    )
    def get_account_details(customer_id: str) -> mcp_types.CallToolResult:
        return invoke_catalogue_tool(
            active_catalogue,
            "get_account_details",
            {"customer_id": customer_id},
        )

    @server.tool(
        name="list_campaigns",
        description="List campaigns for one explicitly allow-listed Google Ads customer.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        meta=OAUTH_TOOL_META if oauth_server is not None else None,
    )
    def list_campaigns(
        customer_id: str,
        status: str | None = None,
        campaign_ids: list[int] | None = None,
        campaign_name_contains: str | None = None,
        channel_type: str | None = None,
    ) -> mcp_types.CallToolResult:
        return invoke_catalogue_tool(
            active_catalogue,
            "list_campaigns",
            _without_empty_values(
                {
                    "customer_id": customer_id,
                    "status": status,
                    "campaign_ids": campaign_ids,
                    "campaign_name_contains": campaign_name_contains,
                    "channel_type": channel_type,
                }
            ),
        )

    @server.tool(
        name="get_campaign_details",
        description="Return details for one campaign in an explicitly allow-listed customer.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        meta=OAUTH_TOOL_META if oauth_server is not None else None,
    )
    def get_campaign_details(customer_id: str, campaign_id: int) -> mcp_types.CallToolResult:
        return invoke_catalogue_tool(
            active_catalogue,
            "get_campaign_details",
            {"customer_id": customer_id, "campaign_id": campaign_id},
        )

    @server.tool(
        name="get_campaign_cost",
        description="Return daily campaign cost rows for an explicitly allow-listed customer.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        meta=OAUTH_TOOL_META if oauth_server is not None else None,
    )
    def get_campaign_cost(
        customer_id: str,
        start_date: str,
        end_date: str,
        status: str | None = None,
        campaign_ids: list[int] | None = None,
    ) -> mcp_types.CallToolResult:
        return invoke_catalogue_tool(
            active_catalogue,
            "get_campaign_cost",
            _without_empty_values(
                {
                    "customer_id": customer_id,
                    "start_date": start_date,
                    "end_date": end_date,
                    "status": status,
                    "campaign_ids": campaign_ids,
                }
            ),
        )

    @server.tool(
        name="get_campaign_performance",
        description=(
            "Return campaign performance rows for one or more explicitly allow-listed customers."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        meta=OAUTH_TOOL_META if oauth_server is not None else None,
    )
    def get_campaign_performance(
        start_date: str,
        end_date: str,
        customer_id: str | None = None,
        customer_ids: list[str] | None = None,
        status: str | None = None,
        campaign_ids: list[int] | None = None,
    ) -> mcp_types.CallToolResult:
        return invoke_catalogue_tool(
            active_catalogue,
            "get_campaign_performance",
            _without_empty_values(
                {
                    "customer_id": customer_id,
                    "customer_ids": customer_ids,
                    "start_date": start_date,
                    "end_date": end_date,
                    "status": status,
                    "campaign_ids": campaign_ids,
                }
            ),
        )

    if oauth_server is not None:
        oauth_server.install_routes(server)

    return server


def build_streamable_http_app(
    server: MCPServer,
    settings: McpRuntimeSettings,
) -> ASGIApp:
    """Build the SDK Streamable HTTP app with optional transport-level auth."""

    transport_security = settings.transport_security_settings()
    app = server.streamable_http_app(
        streamable_http_path=settings.path,
        host=settings.host,
        transport_security=transport_security,
    )
    if settings.auth_mode == STATIC_BEARER_AUTH_MODE and settings.auth_token:
        app = BearerTokenAuthMiddleware(app, settings.auth_token)
    if settings.auth_mode == OAUTH_AUTH_MODE:
        app = OAuthChallengeScopeMiddleware(app, READ_SCOPE)
    return HostOriginSecurityMiddleware(app, transport_security)


def _oauth_server_for_runtime(settings: McpRuntimeSettings) -> McpOAuthServer | None:
    if settings.transport == "stdio" or settings.auth_mode == STATIC_BEARER_AUTH_MODE:
        return None
    if settings.auth_mode == OAUTH_AUTH_MODE:
        return McpOAuthServer(settings.oauth_settings())
    raise ConfigurationError(
        f"Unsupported {GOOGLE_ADS_MCP_AUTH_MODE_ENV_VAR}: {settings.auth_mode}.",
        code="unsupported_mcp_auth_mode",
    )


def run_streamable_http_server(
    server: MCPServer,
    settings: McpRuntimeSettings,
) -> None:
    """Run the SDK Streamable HTTP transport."""

    import anyio

    anyio.run(run_streamable_http_server_async, server, settings)


async def run_streamable_http_server_async(
    server: MCPServer,
    settings: McpRuntimeSettings,
) -> None:
    import uvicorn

    app = build_streamable_http_app(server, settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="warning",
        access_log=False,
    )
    await uvicorn.Server(config).serve()


def invoke_catalogue_tool(
    catalogue: GoogleAdsFunctionCatalogue,
    function_name: str,
    params: Mapping[str, Any],
) -> mcp_types.CallToolResult:
    envelope = catalogue.invoke(function_name, dict(params))
    return mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(
                text=json.dumps(envelope, sort_keys=True, default=str),
            )
        ],
        structured_content=envelope,
        is_error=not bool(envelope.get("success")),
    )


def _without_empty_values(params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in params.items()
        if value is not None and value != [] and value != ""
    }


def _port_from_env() -> int:
    raw = os.getenv("GOOGLE_ADS_MCP_PORT")
    if not raw:
        return DEFAULT_STREAMABLE_HTTP_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_STREAMABLE_HTTP_PORT
    return port if 0 < port <= 65535 else DEFAULT_STREAMABLE_HTTP_PORT


def _normalize_http_path(path: str) -> str:
    normalized = path.strip() or DEFAULT_STREAMABLE_HTTP_PATH
    return normalized if normalized.startswith("/") else f"/{normalized}"


def _auth_mode_from_env() -> str:
    auth_mode = (
        os.getenv(GOOGLE_ADS_MCP_AUTH_MODE_ENV_VAR) or DEFAULT_HTTP_AUTH_MODE
    ).strip().lower()
    if auth_mode not in SUPPORTED_HTTP_AUTH_MODES:
        raise ConfigurationError(
            f"{GOOGLE_ADS_MCP_AUTH_MODE_ENV_VAR} must be one of: "
            f"{', '.join(SUPPORTED_HTTP_AUTH_MODES)}.",
            code="unsupported_mcp_auth_mode",
        )
    return auth_mode


def build_transport_security_settings(
    *,
    public_host: str | None,
    port: int,
) -> TransportSecuritySettings:
    allowed_hosts = [
        "127.0.0.1",
        f"127.0.0.1:{port}",
        "localhost",
        f"localhost:{port}",
    ]
    allowed_origins = [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    ]

    if public_host:
        allowed_hosts.extend(
            [
                public_host,
                f"{public_host}:443",
                f"{public_host}:*",
            ]
        )
        allowed_origins.append(f"https://{public_host}")

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_deduplicate(allowed_hosts),
        allowed_origins=_deduplicate(allowed_origins),
    )


def _normalize_public_host(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    if not parsed.hostname:
        return None
    return parsed.hostname.lower()


def _normalize_public_origin(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip().rstrip("/")
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def _public_host_from_origin(origin: str | None) -> str | None:
    if not origin:
        return None
    parsed = urlparse(origin)
    return parsed.hostname.lower() if parsed.hostname else None


def _default_public_origin(*, public_host: str | None, host: str, port: int) -> str:
    if public_host:
        return f"https://{public_host}"
    return f"http://{host}:{port}"


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _header_value(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value
    return None


async def _send_auth_error(send: Send) -> None:
    body = json.dumps({"error": "unauthorized"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    raise SystemExit(main())
