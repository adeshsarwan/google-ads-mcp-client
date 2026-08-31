from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from argon2 import PasswordHasher
from starlette.testclient import TestClient

from google_ads_function_gateway.exceptions import ConfigurationError
from google_ads_function_gateway.mcp_oauth import (
    AUTHORIZATION_SERVER_METADATA_PATH,
    OFFLINE_ACCESS_SCOPE,
    PROTECTED_RESOURCE_METADATA_PATH,
    READ_SCOPE,
    McpOAuthServer,
    McpOAuthSettings,
)
from google_ads_function_gateway.mcp_server import (
    DEFAULT_HTTP_AUTH_MODE,
    OAUTH_AUTH_MODE,
    McpRuntimeSettings,
    build_mcp_server,
    build_streamable_http_app,
)

PUBLIC_HOST = "googleads-mcp.thebesads.com"
PUBLIC_ORIGIN = f"https://{PUBLIC_HOST}"
RESOURCE_URL = f"{PUBLIC_ORIGIN}/mcp"
REDIRECT_URI = "https://chat.openai.com/aip/oauth/callback"
OWNER_USERNAME = "owner"
OWNER_PASSWORD = "owner-password"
EXPECTED_TOOLS = [
    "get_account_details",
    "get_campaign_cost",
    "get_campaign_details",
    "get_campaign_performance",
    "list_accounts",
    "list_campaigns",
]


class McpOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmpdir.name)
        self._password_hash = PasswordHasher().hash(OWNER_PASSWORD)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_oauth_metadata_advertises_code_pkce_dcr_refresh_and_resource(self) -> None:
        app, _, _ = self._build_oauth_app()

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            auth_metadata = client.get(
                AUTHORIZATION_SERVER_METADATA_PATH,
                headers={"Host": PUBLIC_HOST},
            )
            resource_metadata = client.get(
                f"{PROTECTED_RESOURCE_METADATA_PATH}/mcp",
                headers={"Host": PUBLIC_HOST},
            )
            invalid_authorization = client.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": "missing-client",
                    "redirect_uri": REDIRECT_URI,
                    "scope": READ_SCOPE,
                    "state": "unit-state",
                    "code_challenge": _pkce_s256("valid-verifier"),
                    "code_challenge_method": "S256",
                    "resource": RESOURCE_URL,
                },
                headers={"Host": PUBLIC_HOST},
            )

        self.assertEqual(auth_metadata.status_code, 200)
        auth_body = auth_metadata.json()
        self.assertEqual(auth_body["issuer"], PUBLIC_ORIGIN)
        self.assertEqual(auth_body["authorization_endpoint"], f"{PUBLIC_ORIGIN}/oauth/authorize")
        self.assertEqual(auth_body["token_endpoint"], f"{PUBLIC_ORIGIN}/oauth/token")
        self.assertEqual(auth_body["registration_endpoint"], f"{PUBLIC_ORIGIN}/oauth/register")
        self.assertEqual(auth_body["revocation_endpoint"], f"{PUBLIC_ORIGIN}/oauth/revoke")
        self.assertEqual(auth_body["response_types_supported"], ["code"])
        self.assertNotIn("token", auth_body["response_types_supported"])
        self.assertIn("authorization_code", auth_body["grant_types_supported"])
        self.assertIn("refresh_token", auth_body["grant_types_supported"])
        self.assertIn("none", auth_body["token_endpoint_auth_methods_supported"])
        self.assertEqual(auth_body["code_challenge_methods_supported"], ["S256"])
        self.assertIn(READ_SCOPE, auth_body["scopes_supported"])
        self.assertIn(OFFLINE_ACCESS_SCOPE, auth_body["scopes_supported"])

        self.assertEqual(resource_metadata.status_code, 200)
        resource_body = resource_metadata.json()
        self.assertEqual(resource_body["resource"], RESOURCE_URL)
        self.assertEqual(resource_body["authorization_servers"], [PUBLIC_ORIGIN])
        self.assertEqual(resource_body["scopes_supported"], [READ_SCOPE])
        self.assertEqual(invalid_authorization.status_code, 400)
        self.assertEqual(invalid_authorization.json()["iss"], PUBLIC_ORIGIN)

    def test_dynamic_registration_defaults_to_public_client_and_rejects_bad_metadata(self) -> None:
        app, oauth_server, _ = self._build_oauth_app()

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            registered = _register_client(client)
            missing_read_scope = client.post(
                "/oauth/register",
                json={
                    "redirect_uris": [REDIRECT_URI],
                    "scope": OFFLINE_ACCESS_SCOPE,
                },
                headers={"Host": PUBLIC_HOST},
            )
            unsafe_redirect = client.post(
                "/oauth/register",
                json={
                    "redirect_uris": ["https://*.example.com/callback"],
                    "scope": READ_SCOPE,
                },
                headers={"Host": PUBLIC_HOST},
            )
            unsupported_client_auth = client.post(
                "/oauth/register",
                json={
                    "redirect_uris": [REDIRECT_URI],
                    "scope": READ_SCOPE,
                    "token_endpoint_auth_method": "private_key_jwt",
                },
                headers={"Host": PUBLIC_HOST},
            )
            rejected_schemes = [
                client.post(
                    "/oauth/register",
                    json={
                        "redirect_uris": [f"{scheme}:alert(1)"],
                        "scope": READ_SCOPE,
                    },
                    headers={"Host": PUBLIC_HOST},
                )
                for scheme in ("javascript", "data", "file")
            ]

        self.assertEqual(registered["token_endpoint_auth_method"], "none")
        self.assertNotIn("client_secret", registered)
        stored_client = oauth_server.store.get_client(registered["client_id"])
        self.assertIsNotNone(stored_client)
        assert stored_client is not None
        self.assertEqual(stored_client.redirect_uris, [REDIRECT_URI])
        self.assertEqual(missing_read_scope.status_code, 400)
        self.assertEqual(missing_read_scope.json()["error"], "invalid_client_metadata")
        self.assertEqual(unsafe_redirect.status_code, 400)
        self.assertEqual(unsafe_redirect.json()["error"], "invalid_redirect_uri")
        self.assertEqual(unsupported_client_auth.status_code, 400)
        self.assertNotIn("private_key_jwt", registered)
        for response in rejected_schemes:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "invalid_redirect_uri")

    def test_oauth_register_diagnostics_log_safe_metadata_only(self) -> None:
        app, _, _ = self._build_oauth_app()

        with patch.dict(os.environ, {"GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS": "1"}), TestClient(
            app,
            base_url=PUBLIC_ORIGIN,
        ) as client, self.assertLogs(
            "google_ads_function_gateway.mcp_oauth",
            level="WARNING",
        ) as logs:
            response = client.post(
                "/oauth/register",
                json={
                    "redirect_uris": [REDIRECT_URI],
                    "client_name": "ChatGPT",
                    "scope": f"{READ_SCOPE} {OFFLINE_ACCESS_SCOPE}",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "client_secret_post",
                    "software_id": "openai-chatgpt",
                },
                headers={"Host": PUBLIC_HOST},
            )

        response_body = response.json()
        joined = "\n".join(logs.output)
        self.assertEqual(response.status_code, 201)
        self.assertIn('"event": "mcp_oauth_register"', joined)
        self.assertIn(f'"request_redirect_uris": ["{REDIRECT_URI}"]', joined)
        self.assertIn('"request_token_endpoint_auth_method": "client_secret_post"', joined)
        self.assertIn('"request_grant_types": ["authorization_code", "refresh_token"]', joined)
        self.assertIn('"request_response_types": ["code"]', joined)
        self.assertIn('"request_scopes": ["google_ads.read", "offline_access"]', joined)
        self.assertIn('"request_client_name": "ChatGPT"', joined)
        self.assertIn('"request_software_id": "openai-chatgpt"', joined)
        self.assertIn('"response_client_id_issued": true', joined)
        self.assertIn('"response_client_secret_issued": true', joined)
        self.assertIn('"response_token_endpoint_auth_method": "client_secret_post"', joined)
        self.assertNotIn(response_body["client_secret"], joined)

    def test_oauth_authorize_diagnostics_log_safe_metadata_only(self) -> None:
        app, _, _ = self._build_oauth_app()
        verifier = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~abc"
        code_challenge = _pkce_s256(verifier)

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            registered = _register_client(client)
            with patch.dict(
                os.environ,
                {"GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS": "1"},
            ), self.assertLogs(
                "google_ads_function_gateway.mcp_oauth",
                level="WARNING",
            ) as logs:
                response = client.get(
                    "/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": registered["client_id"],
                        "redirect_uri": REDIRECT_URI,
                        "scope": f"{READ_SCOPE} {OFFLINE_ACCESS_SCOPE}",
                        "state": "unit-state",
                        "code_challenge": code_challenge,
                        "code_challenge_method": "S256",
                        "resource": RESOURCE_URL,
                    },
                    headers={"Host": PUBLIC_HOST},
                    follow_redirects=False,
                )

        joined = "\n".join(logs.output)
        self.assertEqual(response.status_code, 302)
        self.assertIn('"event": "mcp_oauth_authorize"', joined)
        self.assertIn('"client_id_present": true', joined)
        self.assertIn('"client_registered": true', joined)
        self.assertIn('"code_challenge_method": "S256"', joined)
        self.assertIn('"code_challenge_present": true', joined)
        self.assertIn('"next_step": "owner_login"', joined)
        self.assertIn(f'"redirect_uri": "{REDIRECT_URI}"', joined)
        self.assertIn('"requested_scopes": ["google_ads.read", "offline_access"]', joined)
        self.assertIn(f'"resource": "{RESOURCE_URL}"', joined)
        self.assertIn('"state_present": true', joined)
        self.assertNotIn(code_challenge, joined)
        self.assertNotIn("unit-state", joined)
        self.assertNotIn(registered["client_id"], joined)

    def test_authorization_code_pkce_exchange_and_refresh_rotation(self) -> None:
        app, oauth_server, _ = self._build_oauth_app()

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            registered = _register_client(client)
            flow = _authorize_owner(
                client,
                client_id=registered["client_id"],
                scope=f"{READ_SCOPE} {OFFLINE_ACCESS_SCOPE}",
            )
            wrong_resource = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": registered["client_id"],
                    "code": flow["code"],
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": flow["verifier"],
                    "resource": "https://attacker.example.com/mcp",
                },
                headers={"Host": PUBLIC_HOST},
            )
            first_tokens = _exchange_authorization_code(
                client,
                client_id=registered["client_id"],
                code=flow["code"],
                verifier=flow["verifier"],
            )
            reused_code = _exchange_authorization_code_response(
                client,
                client_id=registered["client_id"],
                code=flow["code"],
                verifier=flow["verifier"],
            )
            rotated_tokens = client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": registered["client_id"],
                    "refresh_token": first_tokens["refresh_token"],
                    "resource": RESOURCE_URL,
                },
                headers={"Host": PUBLIC_HOST},
            )
            old_refresh_reuse = client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": registered["client_id"],
                    "refresh_token": first_tokens["refresh_token"],
                    "resource": RESOURCE_URL,
                },
                headers={"Host": PUBLIC_HOST},
            )

        self.assertEqual(wrong_resource.status_code, 400)
        self.assertEqual(wrong_resource.json()["error"], "invalid_target")
        self.assertEqual(first_tokens["token_type"], "Bearer")
        self.assertEqual(first_tokens["scope"], f"{READ_SCOPE} {OFFLINE_ACCESS_SCOPE}")
        self.assertIn("refresh_token", first_tokens)
        self.assertEqual(reused_code.status_code, 400)
        self.assertEqual(reused_code.json()["error"], "invalid_grant")
        self.assertEqual(rotated_tokens.status_code, 200)
        self.assertNotEqual(rotated_tokens.json()["refresh_token"], first_tokens["refresh_token"])
        self.assertEqual(old_refresh_reuse.status_code, 400)
        self.assertEqual(old_refresh_reuse.json()["error"], "invalid_grant")

        db_bytes = oauth_server.settings.db_path.read_bytes()
        self.assertNotIn(flow["code"].encode(), db_bytes)
        self.assertNotIn(first_tokens["access_token"].encode(), db_bytes)
        self.assertNotIn(first_tokens["refresh_token"].encode(), db_bytes)

    def test_pkce_validation_rejects_wrong_missing_plain_and_reused_codes(self) -> None:
        app, _, _ = self._build_oauth_app()

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            registered = _register_client(client)
            wrong_verifier_flow = _authorize_owner(
                client,
                client_id=registered["client_id"],
                scope=READ_SCOPE,
            )
            wrong_verifier = _exchange_authorization_code_response(
                client,
                client_id=registered["client_id"],
                code=wrong_verifier_flow["code"],
                verifier="wrong-verifier",
            )

            missing_verifier_flow = _authorize_owner(
                client,
                client_id=registered["client_id"],
                scope=READ_SCOPE,
            )
            missing_verifier = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": registered["client_id"],
                    "code": missing_verifier_flow["code"],
                    "redirect_uri": REDIRECT_URI,
                    "resource": RESOURCE_URL,
                },
                headers={"Host": PUBLIC_HOST},
            )

            plain_method = client.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": registered["client_id"],
                    "redirect_uri": REDIRECT_URI,
                    "scope": READ_SCOPE,
                    "state": "unit-state",
                    "code_challenge": "plain-verifier",
                    "code_challenge_method": "plain",
                    "resource": RESOURCE_URL,
                },
                headers={"Host": PUBLIC_HOST},
                follow_redirects=False,
            )

            valid_flow = _authorize_owner(
                client,
                client_id=registered["client_id"],
                scope=READ_SCOPE,
            )
            successful = _exchange_authorization_code_response(
                client,
                client_id=registered["client_id"],
                code=valid_flow["code"],
                verifier=valid_flow["verifier"],
            )
            reused_code = _exchange_authorization_code_response(
                client,
                client_id=registered["client_id"],
                code=valid_flow["code"],
                verifier=valid_flow["verifier"],
            )

        self.assertEqual(wrong_verifier.status_code, 400)
        self.assertEqual(wrong_verifier.json()["error"], "invalid_grant")
        self.assertEqual(missing_verifier.status_code, 400)
        self.assertEqual(missing_verifier.json()["error"], "invalid_request")
        self.assertEqual(plain_method.status_code, 302)
        self.assertIn("error=invalid_request", plain_method.headers["location"])
        self.assertEqual(_query_value(plain_method.headers["location"], "iss"), PUBLIC_ORIGIN)
        self.assertEqual(successful.status_code, 200)
        self.assertEqual(reused_code.status_code, 400)
        self.assertEqual(reused_code.json()["error"], "invalid_grant")

    def test_oauth_state_persists_across_restart_and_revocation(self) -> None:
        app, oauth_server, _ = self._build_oauth_app()

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            registered = _register_client(client)
            flow = _authorize_owner(client, client_id=registered["client_id"], scope=READ_SCOPE)
            first_tokens = _exchange_authorization_code(
                client,
                client_id=registered["client_id"],
                code=flow["code"],
                verifier=flow["verifier"],
            )

        restarted_server = McpOAuthServer(oauth_server.settings)
        self.assertIsNotNone(restarted_server.store.get_client(registered["client_id"]))
        self.assertIsNotNone(
            restarted_server.store.get_token(first_tokens["refresh_token"], "refresh")
        )
        self.assertIsNotNone(
            restarted_server.store.get_token(first_tokens["access_token"], "access")
        )

        restarted_app = self._build_app_for_oauth_server(restarted_server)
        with TestClient(restarted_app, base_url=PUBLIC_ORIGIN) as client:
            refreshed = client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": registered["client_id"],
                    "refresh_token": first_tokens["refresh_token"],
                    "resource": RESOURCE_URL,
                },
                headers={"Host": PUBLIC_HOST},
            )
            revoked = client.post(
                "/oauth/revoke",
                data={
                    "client_id": registered["client_id"],
                    "token": refreshed.json()["refresh_token"],
                },
                headers={"Host": PUBLIC_HOST},
            )
            revoked_reuse = client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": registered["client_id"],
                    "refresh_token": refreshed.json()["refresh_token"],
                    "resource": RESOURCE_URL,
                },
                headers={"Host": PUBLIC_HOST},
            )

        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked_reuse.status_code, 400)
        self.assertEqual(revoked_reuse.json()["error"], "invalid_grant")

    def test_mcp_discovery_is_anonymous_but_tool_calls_require_oauth(self) -> None:
        catalogue = _FakeCatalogue(_success_response("list_accounts", []))
        app, oauth_server, _ = self._build_oauth_app(catalogue)
        valid_token = oauth_server.issue_test_token(
            client_id="unit-client",
            scopes=[READ_SCOPE],
        )
        wrong_scope_token = oauth_server.issue_test_token(
            client_id="unit-client",
            scopes=[OFFLINE_ACCESS_SCOPE],
        )

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            missing = _initialize_mcp(client)
            session_id = missing.headers["mcp-session-id"]
            initialized = _mcp_request(
                client,
                "",
                session_id,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            tools = _mcp_request(
                client,
                "",
                session_id,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            missing_token_call = _mcp_request(
                client,
                "",
                session_id,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "list_accounts", "arguments": {}},
                },
            )
            invalid_token_call = _mcp_request(
                client,
                "",
                session_id,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "list_accounts", "arguments": {}},
                },
                authorization="Bearer wrong-token",
            )
            wrong_scope_call = _mcp_request(
                client,
                wrong_scope_token,
                session_id,
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "list_accounts", "arguments": {}},
                },
            )
            valid = _mcp_request(
                client,
                valid_token,
                session_id,
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "list_accounts", "arguments": {}},
                },
            )
            invalid_host = _initialize_mcp(
                client,
                authorization=f"Bearer {valid_token}",
                host="attacker.example.com",
            )

        self.assertEqual(missing.status_code, 200)
        self.assertEqual(initialized.status_code, 202)
        self.assertEqual(tools.status_code, 200)
        tool_body = _sse_json(tools)
        tool_names = sorted(tool["name"] for tool in tool_body["result"]["tools"])
        self.assertEqual(tool_names, EXPECTED_TOOLS)
        for tool in tool_body["result"]["tools"]:
            self.assertIsInstance(tool["title"], str)
            self.assertTrue(tool["title"])
            self.assertEqual(
                tool["_meta"]["securitySchemes"],
                [{"type": "oauth2", "scopes": [READ_SCOPE]}],
            )

        for response in (missing_token_call, invalid_token_call, wrong_scope_call):
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("wrong-token", response.text)
            body = _sse_json(response)
            self.assertTrue(body["result"]["isError"])
            challenge = body["result"]["_meta"]["mcp/www_authenticate"][0]
            self.assertIn("Bearer", challenge)
            self.assertIn('resource_metadata="', challenge)
            self.assertIn(f'scope="{READ_SCOPE}"', challenge)

        self.assertEqual(
            _sse_json(missing_token_call)["result"]["structuredContent"]["error"]["code"],
            "invalid_token",
        )
        self.assertEqual(
            _sse_json(invalid_token_call)["result"]["structuredContent"]["error"]["code"],
            "invalid_token",
        )
        self.assertEqual(
            _sse_json(wrong_scope_call)["result"]["structuredContent"]["error"]["code"],
            "insufficient_scope",
        )
        self.assertEqual(valid.status_code, 200)
        self.assertFalse(_sse_json(valid)["result"]["isError"])
        self.assertEqual(catalogue.calls, [("list_accounts", {})])
        self.assertEqual(invalid_host.status_code, 421)

    def test_http_oauth_tools_list_and_call_use_existing_catalogue(self) -> None:
        catalogue = _FakeCatalogue(_success_response("list_accounts", []))
        app, oauth_server, _ = self._build_oauth_app(catalogue)
        access_token = oauth_server.issue_test_token(
            client_id="unit-client",
            scopes=[READ_SCOPE],
        )

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            initialize = _initialize_mcp(client, authorization=f"Bearer {access_token}")
            session_id = initialize.headers["mcp-session-id"]
            initialized = _mcp_request(
                client,
                access_token,
                session_id,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            tools = _mcp_request(
                client,
                access_token,
                session_id,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            tool_call = _mcp_request(
                client,
                access_token,
                session_id,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "list_accounts", "arguments": {}},
                },
            )

        self.assertEqual(initialize.status_code, 200)
        self.assertEqual(initialized.status_code, 202)
        tool_body = _sse_json(tools)
        tool_names = sorted(tool["name"] for tool in tool_body["result"]["tools"])
        self.assertEqual(tool_names, EXPECTED_TOOLS)
        for tool in tool_body["result"]["tools"]:
            self.assertIsInstance(tool["title"], str)
            self.assertTrue(tool["title"])
            self.assertEqual(
                tool["_meta"]["securitySchemes"],
                [{"type": "oauth2", "scopes": [READ_SCOPE]}],
            )

        call_body = _sse_json(tool_call)
        self.assertFalse(call_body["result"]["isError"])
        self.assertEqual(call_body["result"]["structuredContent"]["function"], "list_accounts")
        self.assertEqual(catalogue.calls, [("list_accounts", {})])

    def test_chatgpt_action_discovery_initial_post_compatibility_variants(self) -> None:
        app, _, _ = self._build_oauth_app()
        initialize_body = _initialize_payload()
        modern_meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "unit-test", "version": "0"},
        }
        modern_discover_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {"_meta": modern_meta},
        }

        variants = [
            (
                "accept_both",
                {"Accept": "application/json, text/event-stream"},
                initialize_body,
                200,
            ),
            ("accept_json_only", {"Accept": "application/json"}, initialize_body, 406),
            ("accept_sse_only", {"Accept": "text/event-stream"}, initialize_body, 406),
            ("accept_wildcard", {"Accept": "*/*"}, initialize_body, 200),
            (
                "no_protocol_header",
                {"Accept": "application/json, text/event-stream"},
                initialize_body,
                200,
            ),
            (
                "protocol_2025_06_18",
                {
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                },
                initialize_body,
                200,
            ),
            (
                "legacy_initialize_with_current_protocol_header",
                {
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                },
                initialize_body,
                400,
            ),
            (
                "modern_server_discover",
                {
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "server/discover",
                },
                modern_discover_body,
                200,
            ),
            (
                "no_session_id_initial",
                {"Accept": "application/json, text/event-stream"},
                initialize_body,
                200,
            ),
            (
                "chatgpt_user_agent",
                {"Accept": "application/json, text/event-stream", "User-Agent": "ChatGPT-User/1.0"},
                initialize_body,
                200,
            ),
            (
                "origin_absent",
                {"Accept": "application/json, text/event-stream"},
                initialize_body,
                200,
            ),
            (
                "chatgpt_origin",
                {"Accept": "application/json, text/event-stream", "Origin": "https://chatgpt.com"},
                initialize_body,
                403,
            ),
        ]

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            statuses = {
                name: client.post(
                    "/mcp",
                    headers={
                        "Host": PUBLIC_HOST,
                        "Content-Type": "application/json",
                        **headers,
                    },
                    json=body,
                ).status_code
                for name, headers, body, _ in variants
            }

        self.assertEqual(
            statuses,
            {name: expected_status for name, _, _, expected_status in variants},
        )

    def test_chatgpt_empty_post_probe_returns_oauth_challenge_without_side_effects(
        self,
    ) -> None:
        catalogue = _FakeCatalogue(_success_response("list_accounts", []))
        app, oauth_server, _ = self._build_oauth_app(catalogue)

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            token_rows_before = _oauth_table_count(oauth_server, "oauth_tokens")
            probe = _chatgpt_empty_probe(client)
            token_rows_after_probe = _oauth_table_count(oauth_server, "oauth_tokens")
            initialize = _initialize_mcp(client)
            session_id = initialize.headers["mcp-session-id"]
            tools = _mcp_request(
                client,
                "",
                session_id,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            protected_call = _mcp_request(
                client,
                "",
                session_id,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "list_accounts", "arguments": {}},
                },
            )

        self.assertEqual(probe.status_code, 401)
        self.assertEqual(probe.json(), {"error": "unauthorized"})
        self.assertEqual(
            probe.headers["www-authenticate"],
            (
                f'Bearer resource_metadata="{PUBLIC_ORIGIN}'
                f'/.well-known/oauth-protected-resource/mcp", scope="{READ_SCOPE}"'
            ),
        )
        self.assertNotIn("mcp-session-id", probe.headers)
        self.assertEqual(token_rows_after_probe, token_rows_before)

        self.assertEqual(initialize.status_code, 200)
        self.assertEqual(tools.status_code, 200)
        tool_names = sorted(tool["name"] for tool in _sse_json(tools)["result"]["tools"])
        self.assertEqual(tool_names, EXPECTED_TOOLS)

        self.assertEqual(protected_call.status_code, 200)
        call_body = _sse_json(protected_call)
        self.assertTrue(call_body["result"]["isError"])
        self.assertEqual(
            call_body["result"]["structuredContent"]["error"]["code"],
            "invalid_token",
        )
        self.assertEqual(catalogue.calls, [])
        self.assertEqual(_oauth_table_count(oauth_server, "oauth_tokens"), token_rows_before)

    def test_empty_post_probe_nearby_malformed_requests_stay_strict(self) -> None:
        app, oauth_server, _ = self._build_oauth_app()
        access_token = oauth_server.issue_test_token(
            client_id="unit-client",
            scopes=[READ_SCOPE],
        )

        with TestClient(app, base_url=PUBLIC_ORIGIN) as client:
            responses = {
                "empty_with_authorization": _chatgpt_empty_probe(
                    client,
                    authorization=f"Bearer {access_token}",
                ),
                "empty_with_session": _chatgpt_empty_probe(
                    client,
                    extra_headers={"mcp-session-id": "unit-session"},
                ),
                "empty_with_protocol": _chatgpt_empty_probe(
                    client,
                    extra_headers={"MCP-Protocol-Version": "2025-06-18"},
                ),
                "malformed_json": client.post(
                    "/mcp",
                    headers={
                        "Host": PUBLIC_HOST,
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    content=b"{",
                ),
                "json_array_batch": client.post(
                    "/mcp",
                    headers={
                        "Host": PUBLIC_HOST,
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    json=[
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "unit-test", "version": "0"},
                            },
                        }
                    ],
                ),
                "non_empty_octet_stream": client.post(
                    "/mcp",
                    headers={
                        "Host": PUBLIC_HOST,
                        "Content-Type": "application/octet-stream",
                        "Accept": "*/*",
                    },
                    content=b"not-json",
                ),
            }

        self.assertEqual(
            {name: int(response.status_code) for name, response in responses.items()},
            {
                "empty_with_authorization": 400,
                "empty_with_session": 404,
                "empty_with_protocol": 400,
                "malformed_json": 400,
                "json_array_batch": 400,
                "non_empty_octet_stream": 400,
            },
        )

    def test_oauth_env_configuration_defaults_for_remote_http(self) -> None:
        env = {
            "GOOGLE_ADS_MCP_PUBLIC_HOST": PUBLIC_HOST,
            "GOOGLE_ADS_MCP_OWNER_USERNAME": OWNER_USERNAME,
            "GOOGLE_ADS_MCP_OWNER_PASSWORD_HASH": self._password_hash,
            "GOOGLE_ADS_MCP_OAUTH_SECRET": "s" * 64,
            "GOOGLE_ADS_MCP_OAUTH_DB": str(self._tmp_path / "oauth.db"),
        }

        with patch.dict(os.environ, env, clear=True):
            runtime = McpRuntimeSettings.from_env(
                transport="streamable-http",
                port=8010,
            )
            oauth_settings = runtime.oauth_settings()

        self.assertEqual(runtime.auth_mode, DEFAULT_HTTP_AUTH_MODE)
        self.assertEqual(runtime.auth_mode, OAUTH_AUTH_MODE)
        self.assertEqual(runtime.public_origin, PUBLIC_ORIGIN)
        self.assertEqual(oauth_settings.issuer_url, PUBLIC_ORIGIN)
        self.assertEqual(oauth_settings.resource_url, RESOURCE_URL)

    def test_oauth_env_rejects_plaintext_owner_password(self) -> None:
        env = {
            "GOOGLE_ADS_MCP_OWNER_USERNAME": OWNER_USERNAME,
            "GOOGLE_ADS_MCP_OWNER_PASSWORD_HASH": "plaintext-password",
            "GOOGLE_ADS_MCP_OAUTH_SECRET": "s" * 64,
        }

        with patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigurationError):
            McpOAuthSettings.from_env(
                public_origin=PUBLIC_ORIGIN,
                resource_url=RESOURCE_URL,
            )

    def test_oauth_module_has_no_http_specific_google_ads_implementation(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "google_ads_function_gateway"
            / "mcp_oauth.py"
        ).read_text()

        self.assertNotIn("google.ads.googleads", source)
        self.assertNotIn("GoogleAdsService", source)
        self.assertNotIn("SearchGoogleAdsRequest", source)
        self.assertNotIn("OfficialGoogleAdsClientWrapper", source)

    def _build_oauth_app(
        self,
        catalogue: _FakeCatalogue | None = None,
    ) -> tuple[object, McpOAuthServer, _FakeCatalogue]:
        oauth_settings = McpOAuthSettings(
            issuer_url=PUBLIC_ORIGIN,
            resource_url=RESOURCE_URL,
            public_origin=PUBLIC_ORIGIN,
            db_path=self._tmp_path / "oauth.db",
            owner_username=OWNER_USERNAME,
            owner_password_hash=self._password_hash,
            oauth_secret="s" * 64,
        )
        oauth_server = McpOAuthServer(oauth_settings)
        active_catalogue = catalogue or _FakeCatalogue(_success_response("list_accounts", []))
        app = self._build_app_for_oauth_server(oauth_server, active_catalogue)
        return app, oauth_server, active_catalogue

    def _build_app_for_oauth_server(
        self,
        oauth_server: McpOAuthServer,
        catalogue: _FakeCatalogue | None = None,
    ) -> object:
        active_catalogue = catalogue or _FakeCatalogue(_success_response("list_accounts", []))
        with patch("google_ads_function_gateway.mcp_server.load_local_env"):
            server = build_mcp_server(active_catalogue, oauth_server=oauth_server)
        return build_streamable_http_app(
            server,
            McpRuntimeSettings(
                transport="streamable-http",
                port=8010,
                public_host=PUBLIC_HOST,
                public_origin=PUBLIC_ORIGIN,
                auth_mode=OAUTH_AUTH_MODE,
            ),
            oauth_server=oauth_server,
        )


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


def _register_client(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/oauth/register",
        json={
            "redirect_uris": [REDIRECT_URI],
            "client_name": "ChatGPT",
            "scope": f"{READ_SCOPE} {OFFLINE_ACCESS_SCOPE}",
            "token_endpoint_auth_method": "none",
        },
        headers={"Host": PUBLIC_HOST},
    )
    if response.status_code != 201:
        raise AssertionError(response.text)
    return response.json()


def _authorize_owner(client: TestClient, *, client_id: str, scope: str) -> dict[str, str]:
    verifier = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~abc"
    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": scope,
            "state": "unit-state",
            "code_challenge": _pkce_s256(verifier),
            "code_challenge_method": "S256",
            "resource": RESOURCE_URL,
        },
        headers={"Host": PUBLIC_HOST},
        follow_redirects=False,
    )
    if response.status_code != 302:
        raise AssertionError(response.text)
    request_id = _query_value(response.headers["location"], "request")

    login_page = client.get(response.headers["location"], headers={"Host": PUBLIC_HOST})
    login_response = client.post(
        "/oauth/owner/login",
        data={
            "request": request_id,
            "csrf_token": _csrf_token(login_page.text),
            "username": OWNER_USERNAME,
            "password": OWNER_PASSWORD,
        },
        headers={"Host": PUBLIC_HOST},
        follow_redirects=False,
    )
    if login_response.status_code != 302:
        raise AssertionError(login_response.text)

    approval_page = client.get(login_response.headers["location"], headers={"Host": PUBLIC_HOST})
    approval_response = client.post(
        "/oauth/owner/approve",
        data={
            "request": request_id,
            "csrf_token": _csrf_token(approval_page.text),
            "decision": "approve",
        },
        headers={"Host": PUBLIC_HOST},
        follow_redirects=False,
    )
    if approval_response.status_code != 302:
        raise AssertionError(approval_response.text)

    location = approval_response.headers["location"]
    return {
        "code": _query_value(location, "code"),
        "state": _query_value(location, "state"),
        "iss": _query_value(location, "iss"),
        "verifier": verifier,
    }


def _exchange_authorization_code(
    client: TestClient,
    *,
    client_id: str,
    code: str,
    verifier: str,
) -> dict[str, Any]:
    response = _exchange_authorization_code_response(
        client,
        client_id=client_id,
        code=code,
        verifier=verifier,
    )
    if response.status_code != 200:
        raise AssertionError(response.text)
    return response.json()


def _exchange_authorization_code_response(
    client: TestClient,
    *,
    client_id: str,
    code: str,
    verifier: str,
) -> Any:
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "resource": RESOURCE_URL,
        },
        headers={"Host": PUBLIC_HOST},
    )


def _initialize_mcp(
    client: TestClient,
    *,
    authorization: str | None = None,
    host: str = PUBLIC_HOST,
) -> Any:
    return _mcp_request(
        client,
        authorization[7:] if authorization else "",
        None,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "unit-test", "version": "0"},
            },
        },
        authorization=authorization,
        host=host,
    )


def _initialize_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "unit-test", "version": "0"},
        },
    }


def _chatgpt_empty_probe(
    client: TestClient,
    *,
    authorization: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers = {
        "Host": PUBLIC_HOST,
        "Content-Length": "0",
        "Content-Type": "application/octet-stream",
        "Accept": "*/*",
        "User-Agent": "Python/3.13 aiohttp/3.13.5",
        **(extra_headers or {}),
    }
    if authorization:
        headers["Authorization"] = authorization
    return client.post("/mcp", headers=headers, content=b"")


def _mcp_request(
    client: TestClient,
    access_token: str,
    session_id: str | None,
    payload: dict[str, Any],
    *,
    authorization: str | None = None,
    host: str = PUBLIC_HOST,
) -> Any:
    headers = {
        "Host": host,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    bearer = authorization or (f"Bearer {access_token}" if access_token else None)
    if bearer:
        headers["Authorization"] = bearer
    if session_id:
        headers["mcp-session-id"] = session_id
    return client.post("/mcp", headers=headers, json=payload)


def _sse_json(response: Any) -> dict[str, Any]:
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError(response.text)


def _oauth_table_count(oauth_server: McpOAuthServer, table_name: str) -> int:
    conn = sqlite3.connect(oauth_server.store.db_path)
    try:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table_name}")
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def _csrf_token(body: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    if not match:
        raise AssertionError(body)
    return match.group(1)


def _query_value(url: str, name: str) -> str:
    values = parse_qs(urlparse(url).query).get(name)
    if not values:
        raise AssertionError(url)
    return values[0]


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


if __name__ == "__main__":
    unittest.main()
