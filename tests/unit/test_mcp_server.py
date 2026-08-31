from __future__ import annotations

import asyncio
import contextlib
import json
import os
import unittest
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from google_ads_function_gateway.mcp_server import (
    STATIC_BEARER_AUTH_MODE,
    BearerTokenAuthMiddleware,
    McpRuntimeSettings,
    build_mcp_server,
    build_streamable_http_app,
    build_transport_security_settings,
    configure_stdio_logging,
    invoke_catalogue_tool,
    main,
)
from tests.unit.fakes import FakeGoogleAdsClient, build_catalogue

EXPECTED_TOOLS = [
    "get_account_details",
    "get_campaign_cost",
    "get_campaign_details",
    "get_campaign_performance",
    "list_accounts",
    "list_campaigns",
]


class McpServerTests(unittest.TestCase):
    def test_main_defaults_to_stdio_transport(self) -> None:
        fake_server = _FakeServer()

        with patch(
            "google_ads_function_gateway.mcp_server.load_local_env"
        ), patch(
            "google_ads_function_gateway.mcp_server.build_mcp_server",
            return_value=fake_server,
        ), contextlib.redirect_stdout(
            StringIO()
        ):
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_server.run_calls, ["stdio"])

    def test_stdio_transport_ignores_http_auth_mode_configuration(self) -> None:
        fake_server = _FakeServer()

        with patch.dict(os.environ, {"GOOGLE_ADS_MCP_AUTH_MODE": "not-valid"}), patch(
            "google_ads_function_gateway.mcp_server.load_local_env"
        ), patch(
            "google_ads_function_gateway.mcp_server.build_mcp_server",
            return_value=fake_server,
        ), contextlib.redirect_stdout(
            StringIO()
        ):
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_server.run_calls, ["stdio"])

    def test_main_starts_streamable_http_transport_from_env(self) -> None:
        fake_server = _FakeServer()
        env = {
            "GOOGLE_ADS_MCP_HOST": "127.0.0.1",
            "GOOGLE_ADS_MCP_PORT": "8765",
            "GOOGLE_ADS_MCP_AUTH_TOKEN": "unit-test-token",
            "GOOGLE_ADS_MCP_PUBLIC_HOST": "https://googleads-mcp.thebesads.com/mcp",
            "GOOGLE_ADS_MCP_AUTH_MODE": STATIC_BEARER_AUTH_MODE,
        }

        with patch.dict(os.environ, env), patch(
            "google_ads_function_gateway.mcp_server.load_local_env"
        ), patch(
            "google_ads_function_gateway.mcp_server.build_mcp_server",
            return_value=fake_server,
        ), patch(
            "google_ads_function_gateway.mcp_server.run_streamable_http_server"
        ) as run_http, contextlib.redirect_stdout(
            StringIO()
        ):
            exit_code = main(["--transport", "streamable-http"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_server.run_calls, [])
        run_http.assert_called_once()
        self.assertIs(run_http.call_args.args[0], fake_server)
        settings = run_http.call_args.args[1]
        self.assertEqual(settings.transport, "streamable-http")
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8765)
        self.assertEqual(settings.path, "/mcp")
        self.assertEqual(settings.public_host, "googleads-mcp.thebesads.com")
        self.assertEqual(settings.auth_mode, STATIC_BEARER_AUTH_MODE)
        self.assertEqual(settings.endpoint_url, "http://127.0.0.1:8765/mcp")
        self.assertIsNone(run_http.call_args.kwargs["oauth_server"])

    def test_runtime_settings_enable_opt_in_http_diagnostics(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS": "1",
                "GOOGLE_ADS_MCP_AUTH_MODE": STATIC_BEARER_AUTH_MODE,
            },
        ):
            settings = McpRuntimeSettings.from_env(transport="streamable-http")

        self.assertTrue(settings.http_diagnostics)

    def test_transport_security_settings_allow_localhost_and_public_host(self) -> None:
        settings = build_transport_security_settings(
            public_host="googleads-mcp.thebesads.com",
            port=8010,
        )

        self.assertTrue(settings.enable_dns_rebinding_protection)
        self.assertIn("127.0.0.1", settings.allowed_hosts)
        self.assertIn("127.0.0.1:8010", settings.allowed_hosts)
        self.assertIn("localhost", settings.allowed_hosts)
        self.assertIn("localhost:8010", settings.allowed_hosts)
        self.assertIn("googleads-mcp.thebesads.com", settings.allowed_hosts)
        self.assertIn("googleads-mcp.thebesads.com:443", settings.allowed_hosts)
        self.assertIn("https://googleads-mcp.thebesads.com", settings.allowed_origins)
        self.assertNotIn("*", settings.allowed_origins)

    def test_build_mcp_server_loads_local_env_without_stdout_output(self) -> None:
        fake_catalogue = _FakeCatalogue(_success_response("list_accounts", []))
        stdout = StringIO()
        stderr = StringIO()

        with patch(
            "google_ads_function_gateway.mcp_server.load_local_env"
        ) as load_env, patch(
            "google_ads_function_gateway.mcp_server.GoogleAdsFunctionCatalogue.from_settings",
            return_value=fake_catalogue,
        ), contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(
            stderr
        ):
            server = build_mcp_server()

        self.assertEqual(stdout.getvalue(), "")
        load_env.assert_called_once_with()
        self.assertEqual(
            sorted(tool.name for tool in server._tool_manager.list_tools()),
            EXPECTED_TOOLS,
        )

    def test_registered_tools_are_annotated_as_read_only(self) -> None:
        with patch("google_ads_function_gateway.mcp_server.load_local_env"):
            server = build_mcp_server(_FakeCatalogue(_success_response("list_accounts", [])))

        for tool in server._tool_manager.list_tools():
            self.assertIsNotNone(tool.annotations)
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)

    def test_no_mutation_tools_are_registered(self) -> None:
        with patch("google_ads_function_gateway.mcp_server.load_local_env"):
            server = build_mcp_server(_FakeCatalogue(_success_response("list_accounts", [])))

        tool_names = sorted(tool.name for tool in server._tool_manager.list_tools())
        self.assertEqual(tool_names, EXPECTED_TOOLS)
        self.assertFalse(
            any(
                token in name
                for name in tool_names
                for token in ("create", "delete", "mutate", "pause", "remove", "update")
            )
        )

    def test_streamable_http_app_starts_with_same_registered_tools(self) -> None:
        with patch("google_ads_function_gateway.mcp_server.load_local_env"):
            server = build_mcp_server(_FakeCatalogue(_success_response("list_accounts", [])))

        before = sorted(tool.name for tool in server._tool_manager.list_tools())
        app = build_streamable_http_app(
            server,
            McpRuntimeSettings(
                transport="streamable-http",
                auth_mode=STATIC_BEARER_AUTH_MODE,
            ),
        )

        with TestClient(app) as client:
            self.assertEqual(
                client.get("/not-found", headers={"Host": "localhost:8000"}).status_code,
                404,
            )

        self.assertEqual(before, EXPECTED_TOOLS)
        self.assertEqual(
            sorted(tool.name for tool in server._tool_manager.list_tools()),
            EXPECTED_TOOLS,
        )

    def test_streamable_http_accepts_localhost_and_public_host_headers(self) -> None:
        app = _http_mcp_app(public_host="googleads-mcp.thebesads.com", port=8010)

        with TestClient(app) as client:
            localhost = _initialize_mcp(client, host="localhost:8010")
            loopback = _initialize_mcp(client, host="127.0.0.1:8010")
            public_host = _initialize_mcp(client, host="googleads-mcp.thebesads.com")
            public_host_with_port = _initialize_mcp(
                client,
                host="googleads-mcp.thebesads.com:443",
            )

        self.assertNotEqual(localhost.status_code, 421)
        self.assertNotEqual(loopback.status_code, 421)
        self.assertNotEqual(public_host.status_code, 421)
        self.assertNotEqual(public_host_with_port.status_code, 421)

    def test_streamable_http_rejects_unrelated_host_header(self) -> None:
        app = _http_mcp_app(public_host="googleads-mcp.thebesads.com", port=8010)

        with TestClient(app) as client:
            response = _initialize_mcp(client, host="attacker.example.com")

        self.assertEqual(response.status_code, 421)
        self.assertEqual(response.text, "Invalid Host header")

    def test_streamable_http_allows_only_configured_public_origin(self) -> None:
        app = _http_mcp_app(public_host="googleads-mcp.thebesads.com", port=8010)

        with TestClient(app) as client:
            production_origin = _initialize_mcp(
                client,
                host="googleads-mcp.thebesads.com",
                origin="https://googleads-mcp.thebesads.com",
            )
            unrelated_origin = _initialize_mcp(
                client,
                host="googleads-mcp.thebesads.com",
                origin="https://attacker.example.com",
            )

        self.assertNotEqual(production_origin.status_code, 403)
        self.assertEqual(unrelated_origin.status_code, 403)
        self.assertEqual(unrelated_origin.text, "Invalid Origin header")

    def test_streamable_http_bearer_auth_still_applies_with_host_allowlist(self) -> None:
        app = _http_mcp_app(
            public_host="googleads-mcp.thebesads.com",
            port=8010,
            auth_token="expected-token",
        )

        with TestClient(app) as client:
            missing = _initialize_mcp(client, host="googleads-mcp.thebesads.com")
            invalid = _initialize_mcp(
                client,
                host="googleads-mcp.thebesads.com",
                authorization="Bearer wrong-token",
            )
            valid = _initialize_mcp(
                client,
                host="googleads-mcp.thebesads.com",
                authorization="Bearer expected-token",
            )
            invalid_host = _initialize_mcp(
                client,
                host="attacker.example.com",
                authorization="Bearer expected-token",
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertNotEqual(valid.status_code, 401)
        self.assertNotEqual(valid.status_code, 421)
        self.assertEqual(invalid_host.status_code, 421)

    def test_http_diagnostics_log_mcp_method_without_authorization_value(self) -> None:
        app = _http_mcp_app(
            public_host="googleads-mcp.thebesads.com",
            port=8010,
            auth_token="secret-token",
            http_diagnostics=True,
        )

        with TestClient(app) as client, self.assertLogs(
            "google_ads_function_gateway.mcp_server",
            level="WARNING",
        ) as logs:
            response = _initialize_mcp(
                client,
                host="googleads-mcp.thebesads.com",
                authorization="Bearer secret-token",
            )

        joined = "\n".join(logs.output)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"event": "mcp_http_request"', joined)
        self.assertIn('"mcp_method": "initialize"', joined)
        self.assertNotIn("secret-token", joined)

    def test_http_auth_rejects_missing_or_invalid_bearer_token(self) -> None:
        app = BearerTokenAuthMiddleware(_plain_text_app(), "expected-token")

        with TestClient(app) as client:
            missing = client.get("/mcp")
            invalid = client.get("/mcp", headers={"Authorization": "Bearer wrong-token"})

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertNotIn("expected-token", missing.text)
        self.assertNotIn("wrong-token", invalid.text)

    def test_http_auth_allows_valid_bearer_token(self) -> None:
        app = BearerTokenAuthMiddleware(_plain_text_app(), "expected-token")

        with TestClient(app) as client:
            response = client.get("/mcp", headers={"Authorization": "Bearer expected-token"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")

    def test_registered_tool_execution_reaches_existing_catalogue(self) -> None:
        response = _success_response("list_campaigns", [])
        fake_catalogue = _FakeCatalogue(response)
        with patch("google_ads_function_gateway.mcp_server.load_local_env"):
            server = build_mcp_server(fake_catalogue)

        result = asyncio.run(
            server.call_tool("list_campaigns", {"customer_id": "1234567890"})
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, response)
        self.assertEqual(fake_catalogue.calls[0][0], "list_campaigns")
        self.assertEqual(fake_catalogue.calls[0][1], {"customer_id": "1234567890"})

    def test_customer_allow_list_authorization_behavior_is_unchanged(self) -> None:
        fake_client = FakeGoogleAdsClient()
        catalogue = build_catalogue(fake_client, allowed_customer_ids=())
        with patch("google_ads_function_gateway.mcp_server.load_local_env"):
            server = build_mcp_server(catalogue)

        result = asyncio.run(
            server.call_tool("get_account_details", {"customer_id": "1234567890"})
        )
        envelope = result.structured_content

        self.assertTrue(result.is_error)
        self.assertEqual(envelope["error"]["category"], "authorization")
        self.assertEqual(fake_client.search_calls, [])

    def test_http_transport_has_no_direct_google_ads_implementation(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "google_ads_function_gateway"
            / "mcp_server.py"
        ).read_text()

        self.assertNotIn("google.ads.googleads", source)
        self.assertNotIn("GoogleAdsService", source)
        self.assertNotIn("SearchGoogleAdsRequest", source)
        self.assertNotIn("OfficialGoogleAdsClientWrapper", source)

    def test_invoke_catalogue_tool_returns_normalized_structured_envelope(self) -> None:
        response = _success_response("get_account_details", {"customer_id": "1234567890"})
        fake_catalogue = _FakeCatalogue(response)

        result = invoke_catalogue_tool(
            fake_catalogue,
            "get_account_details",
            {"customer_id": "1234567890"},
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, response)
        self.assertEqual(json.loads(result.content[0].text), response)
        self.assertEqual(
            fake_catalogue.calls,
            [("get_account_details", {"customer_id": "1234567890"})],
        )

    def test_invoke_catalogue_tool_marks_failure_without_raw_exception(self) -> None:
        response = {
            "success": False,
            "function": "get_account_details",
            "request_id": "req",
            "data": {},
            "meta": {"customer_ids": [], "currency_codes": [], "row_count": 0},
            "error": {
                "category": "configuration",
                "code": "missing_google_ads_credentials",
                "message": "Google Ads OAuth configuration is incomplete.",
                "retryable": False,
            },
        }
        fake_catalogue = _FakeCatalogue(response)

        result = invoke_catalogue_tool(fake_catalogue, "get_account_details", {})

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content, response)
        self.assertNotIn("Traceback", result.content[0].text)

    def test_configure_stdio_logging_uses_stderr(self) -> None:
        stderr = StringIO()
        stdout = StringIO()

        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            configure_stdio_logging()
            import logging

            logging.getLogger("google_ads_function_gateway").warning("mcp-log-test")

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("mcp-log-test", stderr.getvalue())


class _FakeServer:
    def __init__(self) -> None:
        self.run_calls: list[str] = []

    def run(self, transport: str) -> None:
        self.run_calls.append(transport)


class _FakeCatalogue:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, function_name: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((function_name, params))
        return self._response


def _success_response(function_name: str, data: Any) -> dict[str, Any]:
    return {
        "success": True,
        "function": function_name,
        "request_id": "req",
        "data": data,
        "meta": {"customer_ids": [], "currency_codes": [], "row_count": 0},
        "error": None,
    }


def _plain_text_app() -> Starlette:
    async def endpoint(request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/mcp", endpoint, methods=["GET", "POST"])])


def _http_mcp_app(
    *,
    public_host: str | None = None,
    port: int = 8000,
    auth_token: str | None = None,
    http_diagnostics: bool = False,
) -> object:
    with patch("google_ads_function_gateway.mcp_server.load_local_env"):
        server = build_mcp_server(_FakeCatalogue(_success_response("list_accounts", [])))

    return build_streamable_http_app(
        server,
        McpRuntimeSettings(
            transport="streamable-http",
            port=port,
            auth_token=auth_token,
            public_host=public_host,
            auth_mode=STATIC_BEARER_AUTH_MODE,
            http_diagnostics=http_diagnostics,
        ),
    )


def _initialize_mcp(
    client: TestClient,
    *,
    host: str,
    authorization: str | None = None,
    origin: str | None = None,
) -> object:
    headers = {
        "Host": host,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if authorization:
        headers["Authorization"] = authorization
    if origin:
        headers["Origin"] = origin

    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "unit-test", "version": "0"},
            },
        },
    )


if __name__ == "__main__":
    unittest.main()
