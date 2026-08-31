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

import mcp_types as mcp_types
from mcp.server.mcpserver import MCPServer
from starlette.types import ASGIApp, Receive, Scope, Send

from google_ads_function_gateway.catalogue import GoogleAdsFunctionCatalogue
from google_ads_function_gateway.env import load_local_env

SERVER_NAME = "google-ads-function-gateway"
DEFAULT_STREAMABLE_HTTP_HOST = "127.0.0.1"
DEFAULT_STREAMABLE_HTTP_PORT = 8000
DEFAULT_STREAMABLE_HTTP_PATH = "/mcp"
SUPPORTED_TRANSPORTS = ("stdio", "streamable-http")

READ_ONLY_TOOL_ANNOTATIONS = mcp_types.ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


@dataclass(frozen=True)
class McpRuntimeSettings:
    transport: str = "stdio"
    host: str = DEFAULT_STREAMABLE_HTTP_HOST
    port: int = DEFAULT_STREAMABLE_HTTP_PORT
    path: str = DEFAULT_STREAMABLE_HTTP_PATH
    auth_token: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        transport: str,
        host: str | None = None,
        port: int | None = None,
        path: str | None = None,
    ) -> McpRuntimeSettings:
        return cls(
            transport=transport,
            host=host or os.getenv("GOOGLE_ADS_MCP_HOST") or DEFAULT_STREAMABLE_HTTP_HOST,
            port=port or _port_from_env(),
            path=_normalize_http_path(path or DEFAULT_STREAMABLE_HTTP_PATH),
            auth_token=_optional_env("GOOGLE_ADS_MCP_AUTH_TOKEN"),
        )

    @property
    def endpoint_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"


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


def main(argv: list[str] | None = None) -> int:
    configure_stdio_logging()
    load_local_env()
    args = _build_parser().parse_args(argv)
    runtime_settings = McpRuntimeSettings.from_env(
        transport=args.transport,
        host=args.host,
        port=args.port,
    )
    server = build_mcp_server()
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

    @server.tool(
        name="list_accounts",
        description="Discover accessible Google Ads accounts using the configured MCC context.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    def list_accounts() -> mcp_types.CallToolResult:
        return invoke_catalogue_tool(active_catalogue, "list_accounts", {})

    @server.tool(
        name="get_account_details",
        description="Return details for one explicitly allow-listed Google Ads customer.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
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

    return server


def build_streamable_http_app(
    server: MCPServer,
    settings: McpRuntimeSettings,
) -> ASGIApp:
    """Build the SDK Streamable HTTP app with optional transport-level auth."""

    app = server.streamable_http_app(
        streamable_http_path=settings.path,
        host=settings.host,
    )
    if settings.auth_token:
        return BearerTokenAuthMiddleware(app, settings.auth_token)
    return app


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
