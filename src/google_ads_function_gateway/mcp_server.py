"""Stdio MCP adapter for the read-only Google Ads function catalogue."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import sys
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import mcp_types as mcp_types
from mcp.server.auth.middleware.auth_context import auth_context_var, get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
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
GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS_ENV_VAR = "GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS"
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
    http_diagnostics: bool = False

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
            http_diagnostics=_bool_from_env(GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS_ENV_VAR),
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


class OptionalOAuthContextMiddleware:
    """Attach verified OAuth context to tool requests without blocking tool discovery."""

    def __init__(self, app: ASGIApp, oauth_server: McpOAuthServer) -> None:
        self.app = app
        self._oauth_server = oauth_server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        bearer_token = _bearer_token_from_scope(scope)
        if not bearer_token:
            await self.app(scope, receive, send)
            return

        access_token = await self._oauth_server.verify_token(bearer_token)
        if access_token is None:
            await self.app(scope, receive, send)
            return

        context_token = auth_context_var.set(AuthenticatedUser(access_token))
        try:
            await self.app(scope, receive, send)
        finally:
            auth_context_var.reset(context_token)


class EmptyMcpPostProbeMiddleware:
    """Handle ChatGPT's empty unauthenticated MCP reachability probe."""

    def __init__(self, app: ASGIApp, path: str) -> None:
        self.app = app
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_empty_mcp_post_probe_candidate(
            scope,
            path=self._path,
        ):
            await self.app(scope, receive, send)
            return

        messages, body = await _buffer_http_request(receive)
        if body == b"":
            await _send_no_content(send)
            return

        async def replay_receive() -> Message:
            if messages:
                return messages.popleft()
            return await receive()

        await self.app(scope, replay_receive, send)


class HttpDiagnosticsMiddleware:
    """Emit opt-in, secret-free HTTP/MCP diagnostics to stderr logging."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._logger = logging.getLogger(__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = time.perf_counter()
        status_code: int | None = None
        messages, body = await _buffer_http_request(receive)
        diagnostic_fields = _mcp_diagnostic_fields(scope, body)

        async def replay_receive() -> Message:
            if messages:
                return messages.popleft()
            return await receive()

        async def capture_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        finally:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            self._logger.warning(
                json.dumps(
                    {
                        "duration_ms": duration_ms,
                        "event": "mcp_http_request",
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status_code,
                        **diagnostic_fields,
                    },
                    sort_keys=True,
                )
            )


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
        run_streamable_http_server(server, runtime_settings, oauth_server=oauth_server)
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
    )

    def call_catalogue(function_name: str, params: Mapping[str, Any]) -> mcp_types.CallToolResult:
        return invoke_authorized_catalogue_tool(
            active_catalogue,
            oauth_server,
            function_name,
            params,
        )

    @server.tool(
        name="list_accounts",
        title="List Google Ads Accounts",
        description="Discover accessible Google Ads accounts using the configured MCC context.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        meta=OAUTH_TOOL_META if oauth_server is not None else None,
    )
    def list_accounts() -> mcp_types.CallToolResult:
        return call_catalogue("list_accounts", {})

    @server.tool(
        name="get_account_details",
        title="Get Google Ads Account Details",
        description="Return details for one explicitly allow-listed Google Ads customer.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        meta=OAUTH_TOOL_META if oauth_server is not None else None,
    )
    def get_account_details(customer_id: str) -> mcp_types.CallToolResult:
        return call_catalogue(
            "get_account_details",
            {"customer_id": customer_id},
        )

    @server.tool(
        name="list_campaigns",
        title="List Google Ads Campaigns",
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
        return call_catalogue(
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
        title="Get Google Ads Campaign Details",
        description="Return details for one campaign in an explicitly allow-listed customer.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        meta=OAUTH_TOOL_META if oauth_server is not None else None,
    )
    def get_campaign_details(customer_id: str, campaign_id: int) -> mcp_types.CallToolResult:
        return call_catalogue(
            "get_campaign_details",
            {"customer_id": customer_id, "campaign_id": campaign_id},
        )

    @server.tool(
        name="get_campaign_cost",
        title="Get Google Ads Campaign Cost",
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
        return call_catalogue(
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
        title="Get Google Ads Campaign Performance",
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
        return call_catalogue(
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
    oauth_server: McpOAuthServer | None = None,
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
    if settings.auth_mode == OAUTH_AUTH_MODE and oauth_server is not None:
        app = OptionalOAuthContextMiddleware(app, oauth_server)
    app = EmptyMcpPostProbeMiddleware(app, settings.path)
    app = HostOriginSecurityMiddleware(app, transport_security)
    if settings.http_diagnostics:
        app = HttpDiagnosticsMiddleware(app)
    return app


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
    *,
    oauth_server: McpOAuthServer | None = None,
) -> None:
    """Run the SDK Streamable HTTP transport."""

    import anyio

    anyio.run(run_streamable_http_server_async, server, settings, oauth_server)


async def run_streamable_http_server_async(
    server: MCPServer,
    settings: McpRuntimeSettings,
    oauth_server: McpOAuthServer | None = None,
) -> None:
    import uvicorn

    app = build_streamable_http_app(server, settings, oauth_server=oauth_server)
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


def invoke_authorized_catalogue_tool(
    catalogue: GoogleAdsFunctionCatalogue,
    oauth_server: McpOAuthServer | None,
    function_name: str,
    params: Mapping[str, Any],
) -> mcp_types.CallToolResult:
    if oauth_server is None:
        return invoke_catalogue_tool(catalogue, function_name, params)

    access_token = get_access_token()
    if access_token is None:
        return _oauth_tool_auth_error(
            oauth_server,
            function_name,
            error="invalid_token",
            description="OAuth authentication is required to call Google Ads tools.",
        )
    if READ_SCOPE not in access_token.scopes:
        return _oauth_tool_auth_error(
            oauth_server,
            function_name,
            error="insufficient_scope",
            description=f"The OAuth access token must include the {READ_SCOPE} scope.",
        )
    if access_token.resource != oauth_server.settings.resource_url:
        return _oauth_tool_auth_error(
            oauth_server,
            function_name,
            error="invalid_token",
            description="The OAuth access token is not valid for this MCP resource.",
        )

    return invoke_catalogue_tool(catalogue, function_name, params)


def _oauth_tool_auth_error(
    oauth_server: McpOAuthServer,
    function_name: str,
    *,
    error: str,
    description: str,
) -> mcp_types.CallToolResult:
    envelope = {
        "success": False,
        "function": function_name,
        "request_id": None,
        "data": {},
        "meta": {"customer_ids": [], "currency_codes": [], "row_count": 0},
        "error": {
            "category": "authorization",
            "code": error,
            "message": description,
            "retryable": False,
        },
    }
    challenge = _oauth_www_authenticate_challenge(
        oauth_server,
        error=error,
        description=description,
    )
    return mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(
                text=json.dumps(envelope, sort_keys=True, default=str),
            )
        ],
        structured_content=envelope,
        is_error=True,
        _meta={"mcp/www_authenticate": [challenge]},
    )


def _oauth_www_authenticate_challenge(
    oauth_server: McpOAuthServer,
    *,
    error: str,
    description: str,
) -> str:
    return (
        f'Bearer resource_metadata="{oauth_server.settings.protected_resource_metadata_url}", '
        f'error="{error}", error_description="{description}", scope="{READ_SCOPE}"'
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


def _bool_from_env(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


def _has_header(scope: Scope, name: bytes) -> bool:
    return any(key.lower() == name for key, _ in scope.get("headers", []))


async def _buffer_http_request(receive: Receive) -> tuple[deque[Message], bytes]:
    messages: deque[Message] = deque()

    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request" or not message.get("more_body", False):
            break

    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.request"
    )
    return messages, body


def _is_empty_mcp_post_probe_candidate(scope: Scope, *, path: str) -> bool:
    if scope.get("method") != "POST" or scope.get("path") != path:
        return False
    if not _content_length_is_zero(scope):
        return False
    return not any(
        _has_header(scope, header_name)
        for header_name in (
            b"authorization",
            b"mcp-session-id",
            b"mcp-protocol-version",
            b"mcp-method",
            b"mcp-name",
        )
    )


def _content_length_is_zero(scope: Scope) -> bool:
    content_length = _header_value(scope, b"content-length")
    if content_length is None:
        return False
    try:
        return int(content_length.decode("ascii").strip()) == 0
    except ValueError:
        return False


def _bearer_token_from_scope(scope: Scope) -> str | None:
    authorization = _header_value(scope, b"authorization")
    if not authorization:
        return None
    marker = b"bearer "
    if not authorization.lower().startswith(marker):
        return None
    token = authorization[len(marker) :].decode(errors="ignore").strip()
    return token or None


def _mcp_diagnostic_fields(scope: Scope, body: bytes) -> dict[str, Any]:
    headers = _diagnostic_headers(scope)
    fields: dict[str, Any] = {
        "accept": _truncate_header(headers.get("accept")),
        "authorization_present": "authorization" in headers,
        "body_bytes": len(body),
        "content_length": _truncate_header(headers.get("content-length")),
        "content_type": _truncate_header(headers.get("content-type")),
        "json_top_level_type": None,
        "jsonrpc_id_type": None,
        "jsonrpc_version": None,
        "mcp_method": None,
        "mcp_method_header": _truncate_header(headers.get("mcp-method")),
        "mcp_protocol_version": _truncate_header(headers.get("mcp-protocol-version")),
        "mcp_protocol_version_present": "mcp-protocol-version" in headers,
        "mcp_session_id_present": "mcp-session-id" in headers,
        "origin_hostname": _origin_hostname(headers.get("origin")),
        "origin_present": "origin" in headers,
        "parse_failure": None,
        "user_agent": _truncate_header(headers.get("user-agent")),
    }
    if not body:
        fields["parse_failure"] = "empty_body"
        return fields
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        fields["parse_failure"] = "invalid_json"
        return fields

    fields["json_top_level_type"] = _json_type_name(payload)
    if not isinstance(payload, dict):
        fields["parse_failure"] = "json_top_level_not_object"
        return fields

    method = payload.get("method")
    jsonrpc_version = payload.get("jsonrpc")
    if isinstance(method, str):
        fields["mcp_method"] = method
    elif "method" not in payload:
        fields["parse_failure"] = "missing_method"
    else:
        fields["parse_failure"] = "invalid_method_type"
    if isinstance(jsonrpc_version, str):
        fields["jsonrpc_version"] = jsonrpc_version
    if "id" in payload:
        fields["jsonrpc_id_type"] = _json_type_name(payload["id"])
    return fields


def _diagnostic_headers(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in scope.get("headers", []):
        header_name = key.decode("latin-1").lower()
        if header_name in {"authorization", "cookie", "set-cookie"}:
            headers[header_name] = ""
            continue
        headers[header_name] = value.decode("latin-1", errors="replace")
    return headers


def _truncate_header(value: str | None, *, max_length: int = 200) -> str | None:
    if value is None:
        return None
    return value[:max_length]


def _origin_hostname(origin: str | None) -> str | None:
    if not origin:
        return None
    parsed = urlparse(origin)
    return parsed.hostname


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float):
        return "number"
    return type(value).__name__


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


async def _send_no_content(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [],
        }
    )
    await send({"type": "http.response.body", "body": b""})


if __name__ == "__main__":
    raise SystemExit(main())
