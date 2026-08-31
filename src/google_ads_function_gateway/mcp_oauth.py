"""OAuth 2.0 support for the Streamable HTTP MCP transport."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from google_ads_function_gateway.exceptions import ConfigurationError

READ_SCOPE = "google_ads.read"
OFFLINE_ACCESS_SCOPE = "offline_access"
SUPPORTED_OAUTH_SCOPES = (READ_SCOPE, OFFLINE_ACCESS_SCOPE)
REQUIRED_MCP_SCOPES = (READ_SCOPE,)

AUTHORIZATION_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REGISTRATION_PATH = "/oauth/register"
REVOCATION_PATH = "/oauth/revoke"
OWNER_LOGIN_PATH = "/oauth/owner/login"
OWNER_APPROVE_PATH = "/oauth/owner/approve"
AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
DEFAULT_OAUTH_DB_PATH = "/var/lib/google-ads-mcp/oauth.db"
DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 3600
DEFAULT_AUTH_CODE_TTL_SECONDS = 300
DEFAULT_REFRESH_TOKEN_TTL_SECONDS = 2_592_000
DEFAULT_OWNER_SESSION_TTL_SECONDS = 900
GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS_ENV_VAR = "GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS"

SESSION_COOKIE = "google_ads_mcp_owner_session"
CSRF_COOKIE = "google_ads_mcp_csrf"

NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

HTML_SECURITY_HEADERS = {
    **NO_STORE_HEADERS,
    "Content-Security-Policy": (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "form-action 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
}


@dataclass(frozen=True)
class McpOAuthSettings:
    issuer_url: str
    resource_url: str
    public_origin: str
    db_path: Path
    owner_username: str
    owner_password_hash: str
    oauth_secret: str
    access_token_ttl_seconds: int = DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    auth_code_ttl_seconds: int = DEFAULT_AUTH_CODE_TTL_SECONDS
    refresh_token_ttl_seconds: int = DEFAULT_REFRESH_TOKEN_TTL_SECONDS
    owner_session_ttl_seconds: int = DEFAULT_OWNER_SESSION_TTL_SECONDS

    @classmethod
    def from_env(
        cls,
        *,
        public_origin: str,
        resource_url: str,
    ) -> McpOAuthSettings:
        owner_username = _required_env("GOOGLE_ADS_MCP_OWNER_USERNAME")
        owner_password_hash = _required_env("GOOGLE_ADS_MCP_OWNER_PASSWORD_HASH")
        if not owner_password_hash.startswith("$argon2id$"):
            raise ConfigurationError(
                "GOOGLE_ADS_MCP_OWNER_PASSWORD_HASH must be an Argon2id password hash.",
                code="invalid_mcp_owner_password_hash",
            )
        oauth_secret = _required_env("GOOGLE_ADS_MCP_OAUTH_SECRET")
        if len(oauth_secret) < 32:
            raise ConfigurationError(
                "GOOGLE_ADS_MCP_OAUTH_SECRET must contain at least 32 characters.",
                code="invalid_mcp_oauth_secret",
            )

        return cls(
            issuer_url=public_origin,
            resource_url=resource_url,
            public_origin=public_origin,
            db_path=Path(
                os.getenv("GOOGLE_ADS_MCP_OAUTH_DB") or DEFAULT_OAUTH_DB_PATH
            ).expanduser(),
            owner_username=owner_username,
            owner_password_hash=owner_password_hash,
            oauth_secret=oauth_secret,
            access_token_ttl_seconds=_positive_int_env(
                "GOOGLE_ADS_MCP_ACCESS_TOKEN_TTL_SECONDS",
                DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
            ),
            auth_code_ttl_seconds=_positive_int_env(
                "GOOGLE_ADS_MCP_AUTH_CODE_TTL_SECONDS",
                DEFAULT_AUTH_CODE_TTL_SECONDS,
            ),
            refresh_token_ttl_seconds=_positive_int_env(
                "GOOGLE_ADS_MCP_REFRESH_TOKEN_TTL_SECONDS",
                DEFAULT_REFRESH_TOKEN_TTL_SECONDS,
            ),
        )

    def auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.issuer_url,
            resource_server_url=self.resource_url,
            required_scopes=list(REQUIRED_MCP_SCOPES),
        )

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.public_origin}{AUTHORIZATION_PATH}"

    @property
    def token_endpoint(self) -> str:
        return f"{self.public_origin}{TOKEN_PATH}"

    @property
    def registration_endpoint(self) -> str:
        return f"{self.public_origin}{REGISTRATION_PATH}"

    @property
    def revocation_endpoint(self) -> str:
        return f"{self.public_origin}{REVOCATION_PATH}"

    @property
    def protected_resource_metadata_url(self) -> str:
        parsed = urlparse(self.resource_url)
        return f"{parsed.scheme}://{parsed.netloc}{self.protected_resource_metadata_path}"

    @property
    def protected_resource_metadata_path(self) -> str:
        parsed = urlparse(self.resource_url)
        resource_path = parsed.path if parsed.path != "/" else ""
        return f"{PROTECTED_RESOURCE_METADATA_PATH}{resource_path}"


@dataclass(frozen=True)
class RegisteredClient:
    client_id: str
    client_secret_hash: str | None
    token_endpoint_auth_method: str
    redirect_uris: list[str]
    scope: str
    grant_types: list[str]
    response_types: list[str]
    client_name: str | None
    metadata: dict[str, Any]
    client_id_issued_at: int
    client_secret_expires_at: int | None

    @property
    def scopes(self) -> list[str]:
        return _scope_list(self.scope)

    def to_oauth_client_information(self) -> OAuthClientInformationFull:
        return OAuthClientInformationFull.model_validate(
            {
                "client_id": self.client_id,
                "redirect_uris": self.redirect_uris,
                "token_endpoint_auth_method": self.token_endpoint_auth_method,
                "grant_types": self.grant_types,
                "response_types": self.response_types,
                "scope": self.scope,
                "client_name": self.client_name,
                "client_id_issued_at": self.client_id_issued_at,
                "client_secret_expires_at": self.client_secret_expires_at,
            }
        )


@dataclass(frozen=True)
class PendingAuthorization:
    request_id: str
    client_id: str
    redirect_uri: str
    scope: str
    state: str | None
    code_challenge: str
    resource: str
    created_at: int
    expires_at: int

    @property
    def scopes(self) -> list[str]:
        return _scope_list(self.scope)


@dataclass(frozen=True)
class AuthorizationCodeRecord:
    code_hash: str
    client_id: str
    redirect_uri: str
    scope: str
    code_challenge: str
    resource: str
    subject: str
    created_at: int
    expires_at: int
    used_at: int | None
    revoked_at: int | None

    @property
    def scopes(self) -> list[str]:
        return _scope_list(self.scope)


@dataclass(frozen=True)
class TokenRecord:
    token_hash: str
    token_type: str
    client_id: str
    scope: str
    resource: str
    subject: str
    created_at: int
    expires_at: int | None
    revoked_at: int | None
    parent_token_hash: str | None

    @property
    def scopes(self) -> list[str]:
        return _scope_list(self.scope)


class SQLiteOAuthStore:
    """SQLite persistence for OAuth clients, grants, codes, sessions, and tokens."""

    def __init__(self, db_path: Path, oauth_secret: str) -> None:
        self.db_path = db_path
        self._secret = oauth_secret.encode()
        self.initialize()

    def initialize(self) -> None:
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _chmod_best_effort(self.db_path.parent, 0o700)

        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_secret_hash TEXT,
                    token_endpoint_auth_method TEXT NOT NULL,
                    redirect_uris_json TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    grant_types_json TEXT NOT NULL,
                    response_types_json TEXT NOT NULL,
                    client_name TEXT,
                    metadata_json TEXT NOT NULL,
                    client_id_issued_at INTEGER NOT NULL,
                    client_secret_expires_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS oauth_pending_authorizations (
                    request_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    state TEXT,
                    code_challenge TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS owner_sessions (
                    session_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS oauth_grants (
                    grant_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS authorization_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER,
                    revoked_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    token_type TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    revoked_at INTEGER,
                    parent_token_hash TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_authorization_codes_client
                    ON authorization_codes(client_id);
                CREATE INDEX IF NOT EXISTS idx_oauth_tokens_client_type
                    ON oauth_tokens(client_id, token_type);
                """
            )

        if str(self.db_path) != ":memory:" and self.db_path.exists():
            _chmod_best_effort(self.db_path, 0o600)

    def save_client(self, client: RegisteredClient) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO oauth_clients (
                    client_id,
                    client_secret_hash,
                    token_endpoint_auth_method,
                    redirect_uris_json,
                    scope,
                    grant_types_json,
                    response_types_json,
                    client_name,
                    metadata_json,
                    client_id_issued_at,
                    client_secret_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client.client_id,
                    client.client_secret_hash,
                    client.token_endpoint_auth_method,
                    json.dumps(client.redirect_uris),
                    client.scope,
                    json.dumps(client.grant_types),
                    json.dumps(client.response_types),
                    client.client_name,
                    json.dumps(client.metadata, sort_keys=True),
                    client.client_id_issued_at,
                    client.client_secret_expires_at,
                ),
            )

    def get_client(self, client_id: str) -> RegisteredClient | None:
        row = self._fetch_one(
            "SELECT * FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        )
        if row is None:
            return None
        return RegisteredClient(
            client_id=row["client_id"],
            client_secret_hash=row["client_secret_hash"],
            token_endpoint_auth_method=row["token_endpoint_auth_method"],
            redirect_uris=json.loads(row["redirect_uris_json"]),
            scope=row["scope"],
            grant_types=json.loads(row["grant_types_json"]),
            response_types=json.loads(row["response_types_json"]),
            client_name=row["client_name"],
            metadata=json.loads(row["metadata_json"]),
            client_id_issued_at=row["client_id_issued_at"],
            client_secret_expires_at=row["client_secret_expires_at"],
        )

    def save_pending_authorization(self, pending: PendingAuthorization) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO oauth_pending_authorizations (
                    request_id,
                    client_id,
                    redirect_uri,
                    scope,
                    state,
                    code_challenge,
                    resource,
                    created_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending.request_id,
                    pending.client_id,
                    pending.redirect_uri,
                    pending.scope,
                    pending.state,
                    pending.code_challenge,
                    pending.resource,
                    pending.created_at,
                    pending.expires_at,
                ),
            )

    def get_pending_authorization(self, request_id: str) -> PendingAuthorization | None:
        row = self._fetch_one(
            "SELECT * FROM oauth_pending_authorizations WHERE request_id = ?",
            (request_id,),
        )
        if row is None or row["expires_at"] < int(time.time()):
            return None
        return PendingAuthorization(
            request_id=row["request_id"],
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            scope=row["scope"],
            state=row["state"],
            code_challenge=row["code_challenge"],
            resource=row["resource"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def delete_pending_authorization(self, request_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM oauth_pending_authorizations WHERE request_id = ?",
                (request_id,),
            )

    def save_owner_session(self, session_token: str, username: str, expires_at: int) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO owner_sessions (
                    session_hash,
                    username,
                    created_at,
                    expires_at,
                    revoked_at
                ) VALUES (?, ?, ?, ?, NULL)
                """,
                (self.token_hash(session_token, "owner_session"), username, now, expires_at),
            )

    def owner_session_subject(self, session_token: str) -> str | None:
        row = self._fetch_one(
            """
            SELECT username FROM owner_sessions
            WHERE session_hash = ? AND revoked_at IS NULL AND expires_at >= ?
            """,
            (self.token_hash(session_token, "owner_session"), int(time.time())),
        )
        return str(row["username"]) if row else None

    def save_grant(self, client_id: str, subject: str, scope: str, resource: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO oauth_grants (
                    grant_id,
                    client_id,
                    subject,
                    scope,
                    resource,
                    created_at,
                    revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (secrets.token_urlsafe(24), client_id, subject, scope, resource, int(time.time())),
            )

    def save_authorization_code(
        self,
        *,
        code: str,
        pending: PendingAuthorization,
        subject: str,
        expires_at: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO authorization_codes (
                    code_hash,
                    client_id,
                    redirect_uri,
                    scope,
                    code_challenge,
                    resource,
                    subject,
                    created_at,
                    expires_at,
                    used_at,
                    revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    self.token_hash(code, "authorization_code"),
                    pending.client_id,
                    pending.redirect_uri,
                    pending.scope,
                    pending.code_challenge,
                    pending.resource,
                    subject,
                    int(time.time()),
                    expires_at,
                ),
            )

    def get_authorization_code(self, code: str) -> AuthorizationCodeRecord | None:
        row = self._fetch_one(
            "SELECT * FROM authorization_codes WHERE code_hash = ?",
            (self.token_hash(code, "authorization_code"),),
        )
        if row is None:
            return None
        return AuthorizationCodeRecord(
            code_hash=row["code_hash"],
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            scope=row["scope"],
            code_challenge=row["code_challenge"],
            resource=row["resource"],
            subject=row["subject"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            used_at=row["used_at"],
            revoked_at=row["revoked_at"],
        )

    def mark_authorization_code_used(self, code_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE authorization_codes SET used_at = ? WHERE code_hash = ?",
                (int(time.time()), code_hash),
            )

    def save_token(
        self,
        *,
        token: str,
        token_type: str,
        client_id: str,
        scope: str,
        resource: str,
        subject: str,
        expires_at: int | None,
        parent_token_hash: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO oauth_tokens (
                    token_hash,
                    token_type,
                    client_id,
                    scope,
                    resource,
                    subject,
                    created_at,
                    expires_at,
                    revoked_at,
                    parent_token_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    self.token_hash(token, token_type),
                    token_type,
                    client_id,
                    scope,
                    resource,
                    subject,
                    int(time.time()),
                    expires_at,
                    parent_token_hash,
                ),
            )

    def get_token(self, token: str, token_type: str | None = None) -> TokenRecord | None:
        token_hashes = (
            [self.token_hash(token, token_type)]
            if token_type
            else [self.token_hash(token, "access"), self.token_hash(token, "refresh")]
        )
        placeholders = ",".join("?" for _ in token_hashes)
        row = self._fetch_one(
            f"SELECT * FROM oauth_tokens WHERE token_hash IN ({placeholders})",
            tuple(token_hashes),
        )
        if row is None:
            return None
        return TokenRecord(
            token_hash=row["token_hash"],
            token_type=row["token_type"],
            client_id=row["client_id"],
            scope=row["scope"],
            resource=row["resource"],
            subject=row["subject"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            parent_token_hash=row["parent_token_hash"],
        )

    def revoke_token_hash(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE oauth_tokens
                SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (int(time.time()), token_hash),
            )

    def revoke_token_value(self, token: str) -> None:
        access_hash = self.token_hash(token, "access")
        refresh_hash = self.token_hash(token, "refresh")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE oauth_tokens
                SET revoked_at = ?
                WHERE token_hash IN (?, ?) AND revoked_at IS NULL
                """,
                (int(time.time()), access_hash, refresh_hash),
            )

    def token_hash(self, token: str, purpose: str) -> str:
        return hmac.new(
            self._secret,
            f"{purpose}:{token}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchone()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class McpOAuthServer(TokenVerifier):
    """OAuth authorization server and token verifier for the personal MCP service."""

    def __init__(
        self,
        settings: McpOAuthSettings,
        *,
        store: SQLiteOAuthStore | None = None,
        password_hasher: PasswordHasher | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SQLiteOAuthStore(settings.db_path, settings.oauth_secret)
        self._password_hasher = password_hasher or PasswordHasher()
        self._logger = logging.getLogger(__name__)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self.store.get_client(client_id)
        return client.to_oauth_client_information() if client else None

    async def verify_token(self, token: str) -> AccessToken | None:
        record = self.store.get_token(token, "access")
        if record is None or not self._token_record_is_active(record):
            return None
        if record.resource != self.settings.resource_url:
            return None
        return AccessToken(
            token=token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
            resource=record.resource,
            subject=record.subject,
            claims={"iss": self.settings.issuer_url},
        )

    def install_routes(self, server: Any) -> None:
        server.custom_route(
            AUTHORIZATION_SERVER_METADATA_PATH,
            methods=["GET", "OPTIONS"],
            include_in_schema=False,
        )(self.authorization_server_metadata)
        for metadata_path in dict.fromkeys(
            (
                PROTECTED_RESOURCE_METADATA_PATH,
                self.settings.protected_resource_metadata_path,
            )
        ):
            server.custom_route(
                metadata_path,
                methods=["GET", "OPTIONS"],
                include_in_schema=False,
            )(self.protected_resource_metadata)
        server.custom_route(
            AUTHORIZATION_PATH,
            methods=["GET", "POST"],
            include_in_schema=False,
        )(self.authorize)
        server.custom_route(
            TOKEN_PATH,
            methods=["POST", "OPTIONS"],
            include_in_schema=False,
        )(self.token)
        server.custom_route(
            REGISTRATION_PATH,
            methods=["POST", "OPTIONS"],
            include_in_schema=False,
        )(self.register)
        server.custom_route(
            REVOCATION_PATH,
            methods=["POST", "OPTIONS"],
            include_in_schema=False,
        )(self.revoke)
        server.custom_route(
            OWNER_LOGIN_PATH,
            methods=["GET", "POST"],
            include_in_schema=False,
        )(self.owner_login)
        server.custom_route(
            OWNER_APPROVE_PATH,
            methods=["GET", "POST"],
            include_in_schema=False,
        )(self.owner_approve)

    async def authorization_server_metadata(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options_response("GET, OPTIONS")
        return _json_response(
            {
                "issuer": self.settings.issuer_url,
                "authorization_endpoint": self.settings.authorization_endpoint,
                "token_endpoint": self.settings.token_endpoint,
                "registration_endpoint": self.settings.registration_endpoint,
                "revocation_endpoint": self.settings.revocation_endpoint,
                "scopes_supported": list(SUPPORTED_OAUTH_SCOPES),
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
                "client_id_metadata_document_supported": False,
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def protected_resource_metadata(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options_response("GET, OPTIONS")
        return _json_response(
            {
                "resource": self.settings.resource_url,
                "authorization_servers": [self.settings.issuer_url],
                "scopes_supported": list(REQUIRED_MCP_SCOPES),
                "bearer_methods_supported": ["header"],
                "resource_name": "Google Ads MCP",
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def register(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options_response("POST, OPTIONS")

        payload: object | None = None
        response_payload: dict[str, Any] | None = None

        def finish(response: Response) -> Response:
            self._log_register_diagnostic(request, response, payload, response_payload)
            return response

        try:
            payload = json.loads((await request.body()).decode() or "{}")
        except json.JSONDecodeError:
            return finish(
                _registration_error("invalid_client_metadata", "Request body must be JSON.")
            )

        if not isinstance(payload, dict):
            return finish(
                _registration_error(
                    "invalid_client_metadata",
                    "Client metadata must be a JSON object.",
                )
            )

        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return finish(
                _registration_error(
                    "invalid_redirect_uri",
                    "redirect_uris must contain at least one exact redirect URI.",
                )
            )

        normalized_redirects: list[str] = []
        for redirect_uri in redirect_uris:
            if not isinstance(redirect_uri, str) or not _is_safe_redirect_uri(redirect_uri):
                return finish(
                    _registration_error(
                        "invalid_redirect_uri",
                        "redirect_uris must use exact HTTPS URLs or localhost HTTP loopback URLs.",
                    )
                )
            normalized_redirects.append(redirect_uri)

        token_endpoint_auth_method = payload.get("token_endpoint_auth_method") or "none"
        if token_endpoint_auth_method not in {"none", "client_secret_post", "client_secret_basic"}:
            return finish(
                _registration_error(
                    "invalid_client_metadata",
                    f"token_endpoint_auth_method '{token_endpoint_auth_method}' is not supported",
                )
            )

        grant_types = payload.get("grant_types") or ["authorization_code", "refresh_token"]
        response_types = payload.get("response_types") or ["code"]
        if not _string_list(grant_types) or not _string_list(response_types):
            return finish(
                _registration_error(
                    "invalid_client_metadata",
                    "grant_types and response_types must be string arrays.",
                )
            )
        if "authorization_code" not in grant_types:
            return finish(
                _registration_error(
                    "invalid_client_metadata",
                    "grant_types must include authorization_code.",
                )
            )
        if any(
            grant_type not in {"authorization_code", "refresh_token"}
            for grant_type in grant_types
        ):
            return finish(
                _registration_error(
                    "invalid_client_metadata",
                    "Only authorization_code and refresh_token grants are supported.",
                )
            )
        if "code" not in response_types:
            return finish(
                _registration_error(
                    "invalid_client_metadata",
                    "response_types must include code.",
                )
            )

        scope = payload.get("scope") or " ".join(SUPPORTED_OAUTH_SCOPES)
        if not isinstance(scope, str):
            return finish(_registration_error("invalid_client_metadata", "scope must be a string."))
        scopes = _scope_list(scope)
        if READ_SCOPE not in scopes:
            return finish(
                _registration_error(
                    "invalid_client_metadata",
                    f"scope must include {READ_SCOPE}.",
                )
            )
        if not set(scopes).issubset(SUPPORTED_OAUTH_SCOPES):
            return finish(
                _registration_error(
                    "invalid_client_metadata",
                    "Requested scopes are not supported.",
                )
            )

        client_secret = None
        client_secret_hash = None
        client_secret_expires_at = None
        if token_endpoint_auth_method != "none":
            client_secret = secrets.token_urlsafe(32)
            client_secret_hash = self.store.token_hash(client_secret, "client_secret")
            client_secret_expires_at = 0

        now = int(time.time())
        client_id = f"mcp-client-{secrets.token_urlsafe(24)}"
        client_name = payload.get("client_name")
        if client_name is not None and not isinstance(client_name, str):
            return finish(
                _registration_error("invalid_client_metadata", "client_name must be a string.")
            )

        client = RegisteredClient(
            client_id=client_id,
            client_secret_hash=client_secret_hash,
            token_endpoint_auth_method=token_endpoint_auth_method,
            redirect_uris=normalized_redirects,
            scope=" ".join(scopes),
            grant_types=list(grant_types),
            response_types=list(response_types),
            client_name=client_name,
            metadata=payload,
            client_id_issued_at=now,
            client_secret_expires_at=client_secret_expires_at,
        )
        self.store.save_client(client)

        response_payload = {
            "client_id": client_id,
            "client_id_issued_at": now,
            "redirect_uris": normalized_redirects,
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "grant_types": list(grant_types),
            "response_types": list(response_types),
            "scope": client.scope,
        }
        if client_name:
            response_payload["client_name"] = client_name
        if client_secret is not None:
            response_payload["client_secret"] = client_secret
            response_payload["client_secret_expires_at"] = client_secret_expires_at

        return finish(_json_response(response_payload, status_code=201, headers=NO_STORE_HEADERS))

    async def authorize(self, request: Request) -> Response:
        params = await _request_params(request)
        client = self.store.get_client(params.get("client_id", ""))
        state = params.get("state")

        validation_error = self._validate_authorization_params(params, client)
        if validation_error:
            response = self._authorization_error_response(
                client=client,
                redirect_uri=params.get("redirect_uri"),
                state=state,
                error=validation_error[0],
                description=validation_error[1],
            )
            self._log_authorize_diagnostic(
                request,
                response,
                params,
                client,
                validation_error=validation_error[0],
                next_step="error",
            )
            return response

        assert client is not None
        requested_scopes = _requested_authorization_scopes(params.get("scope"), client)
        pending = PendingAuthorization(
            request_id=secrets.token_urlsafe(24),
            client_id=client.client_id,
            redirect_uri=params["redirect_uri"],
            scope=" ".join(requested_scopes),
            state=state,
            code_challenge=params["code_challenge"],
            resource=params["resource"],
            created_at=int(time.time()),
            expires_at=int(time.time()) + self.settings.auth_code_ttl_seconds,
        )
        self.store.save_pending_authorization(pending)

        if self._authenticated_owner(request) is not None:
            response = RedirectResponse(
                url=f"{OWNER_APPROVE_PATH}?request={pending.request_id}",
                status_code=302,
                headers=NO_STORE_HEADERS,
            )
            self._log_authorize_diagnostic(
                request,
                response,
                params,
                client,
                validation_error=None,
                next_step="owner_approve",
            )
            return response

        response = RedirectResponse(
            url=f"{OWNER_LOGIN_PATH}?request={pending.request_id}",
            status_code=302,
            headers=NO_STORE_HEADERS,
        )
        self._log_authorize_diagnostic(
            request,
            response,
            params,
            client,
            validation_error=None,
            next_step="owner_login",
        )
        return response

    async def owner_login(self, request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = self.store.get_pending_authorization(request_id)
            if pending is None:
                return _plain_error("Authorization request expired or was not found.")
            if self._authenticated_owner(request) is not None:
                return RedirectResponse(
                    url=f"{OWNER_APPROVE_PATH}?request={pending.request_id}",
                    status_code=302,
                    headers=NO_STORE_HEADERS,
                )
            return self._login_page(pending)

        form = await request.form()
        request_id = _form_string(form, "request")
        pending = self.store.get_pending_authorization(request_id)
        if pending is None:
            return _plain_error("Authorization request expired or was not found.")
        if not _csrf_is_valid(request, form):
            return self._login_page(pending, error="The form expired. Try again.")

        username = _form_string(form, "username")
        password = _form_string(form, "password")
        if not self._owner_credentials_are_valid(username, password):
            return self._login_page(pending, error="Invalid owner credentials.")

        session_token = secrets.token_urlsafe(32)
        self.store.save_owner_session(
            session_token,
            username,
            int(time.time()) + self.settings.owner_session_ttl_seconds,
        )
        response = RedirectResponse(
            url=f"{OWNER_APPROVE_PATH}?request={pending.request_id}",
            status_code=302,
            headers=NO_STORE_HEADERS,
        )
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=self.settings.owner_session_ttl_seconds,
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return response

    async def owner_approve(self, request: Request) -> Response:
        request_id = (
            request.query_params.get("request", "")
            if request.method == "GET"
            else _form_string(await request.form(), "request")
        )
        pending = self.store.get_pending_authorization(request_id)
        if pending is None:
            return _plain_error("Authorization request expired or was not found.")

        subject = self._authenticated_owner(request)
        if subject is None:
            return RedirectResponse(
                url=f"{OWNER_LOGIN_PATH}?request={pending.request_id}",
                status_code=302,
                headers=NO_STORE_HEADERS,
            )

        if request.method == "GET":
            client = self.store.get_client(pending.client_id)
            return self._approval_page(pending, client)

        form = await request.form()
        if not _csrf_is_valid(request, form):
            client = self.store.get_client(pending.client_id)
            return self._approval_page(pending, client, error="The form expired. Try again.")

        decision = _form_string(form, "decision")
        self.store.delete_pending_authorization(pending.request_id)
        if decision != "approve":
            return RedirectResponse(
                url=_redirect_with_params(
                    pending.redirect_uri,
                    error="access_denied",
                    error_description="The owner denied access.",
                    state=pending.state,
                    iss=self.settings.issuer_url,
                ),
                status_code=302,
                headers=NO_STORE_HEADERS,
            )

        code = secrets.token_urlsafe(32)
        self.store.save_grant(pending.client_id, subject, pending.scope, pending.resource)
        self.store.save_authorization_code(
            code=code,
            pending=pending,
            subject=subject,
            expires_at=int(time.time()) + self.settings.auth_code_ttl_seconds,
        )
        return RedirectResponse(
            url=_redirect_with_params(
                pending.redirect_uri,
                code=code,
                state=pending.state,
                iss=self.settings.issuer_url,
            ),
            status_code=302,
            headers=NO_STORE_HEADERS,
        )

    async def token(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options_response("POST, OPTIONS")
        form = await request.form()
        try:
            client = self._authenticate_client(request, form)
        except OAuthClientAuthError as exc:
            return _oauth_error("invalid_client", exc.message, status_code=401)

        grant_type = _form_string(form, "grant_type")
        if grant_type not in client.grant_types:
            return _oauth_error(
                "unsupported_grant_type",
                "Grant type is not registered for this client.",
            )
        if grant_type == "authorization_code":
            return self._authorization_code_token_response(client, form)
        if grant_type == "refresh_token":
            return self._refresh_token_response(client, form)
        return _oauth_error("unsupported_grant_type", "Grant type is not supported.")

    async def revoke(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options_response("POST, OPTIONS")
        form = await request.form()
        try:
            client = self._authenticate_client(request, form)
        except OAuthClientAuthError:
            return Response(status_code=401, headers=NO_STORE_HEADERS)

        token = _form_string(form, "token")
        record = self.store.get_token(token)
        if record is not None and record.client_id == client.client_id:
            self.store.revoke_token_hash(record.token_hash)
        return Response(status_code=200, headers=NO_STORE_HEADERS)

    def issue_test_token(
        self,
        *,
        client_id: str,
        scopes: list[str],
        token_type: str = "access",
        resource: str | None = None,
        expires_at: int | None = None,
    ) -> str:
        token = secrets.token_urlsafe(32)
        self.store.save_token(
            token=token,
            token_type=token_type,
            client_id=client_id,
            scope=" ".join(scopes),
            resource=resource or self.settings.resource_url,
            subject=self.settings.owner_username,
            expires_at=expires_at,
        )
        return token

    def _authorization_code_token_response(
        self,
        client: RegisteredClient,
        form: Mapping[str, Any],
    ) -> Response:
        code = _form_string(form, "code")
        redirect_uri = _form_string(form, "redirect_uri")
        code_verifier = _form_string(form, "code_verifier")
        resource = _form_string(form, "resource")
        if not code or not redirect_uri or not code_verifier or not resource:
            return _oauth_error(
                "invalid_request",
                "code, redirect_uri, code_verifier, and resource are required.",
            )
        if resource != self.settings.resource_url:
            return _oauth_error("invalid_target", "Token resource does not match this MCP server.")

        record = self.store.get_authorization_code(code)
        if record is None or record.client_id != client.client_id:
            return _oauth_error("invalid_grant", "authorization code does not exist.")
        if not self._authorization_code_is_active(record):
            return _oauth_error("invalid_grant", "authorization code is expired, used, or revoked.")
        if redirect_uri != record.redirect_uri:
            return _oauth_error(
                "invalid_request",
                "redirect_uri did not match the authorization request.",
            )
        if resource != record.resource:
            return _oauth_error(
                "invalid_target",
                "resource did not match the authorization request.",
            )
        if _s256_challenge(code_verifier) != record.code_challenge:
            return _oauth_error("invalid_grant", "incorrect code_verifier.")

        self.store.mark_authorization_code_used(record.code_hash)
        return self._issue_token_response(
            client_id=client.client_id,
            scopes=record.scopes,
            resource=record.resource,
            subject=record.subject,
        )

    def _refresh_token_response(
        self,
        client: RegisteredClient,
        form: Mapping[str, Any],
    ) -> Response:
        refresh_token = _form_string(form, "refresh_token")
        resource = _form_string(form, "resource")
        if not refresh_token or not resource:
            return _oauth_error("invalid_request", "refresh_token and resource are required.")
        if resource != self.settings.resource_url:
            return _oauth_error("invalid_target", "Token resource does not match this MCP server.")

        record = self.store.get_token(refresh_token, "refresh")
        if record is None or record.client_id != client.client_id:
            return _oauth_error("invalid_grant", "refresh token does not exist.")
        if not self._token_record_is_active(record):
            return _oauth_error("invalid_grant", "refresh token is expired or revoked.")
        if record.resource != resource:
            return _oauth_error("invalid_target", "resource did not match the refresh token.")

        requested_scope = _form_string(form, "scope")
        scopes = _scope_list(requested_scope) if requested_scope else record.scopes
        if not set(scopes).issubset(record.scopes):
            return _oauth_error("invalid_scope", "Requested scopes exceed the refresh token grant.")

        self.store.revoke_token_hash(record.token_hash)
        return self._issue_token_response(
            client_id=client.client_id,
            scopes=scopes,
            resource=record.resource,
            subject=record.subject,
            parent_token_hash=record.token_hash,
        )

    def _issue_token_response(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str,
        subject: str,
        parent_token_hash: str | None = None,
    ) -> Response:
        access_token = secrets.token_urlsafe(40)
        refresh_token = secrets.token_urlsafe(40)
        now = int(time.time())
        self.store.save_token(
            token=access_token,
            token_type="access",
            client_id=client_id,
            scope=" ".join(scopes),
            resource=resource,
            subject=subject,
            expires_at=now + self.settings.access_token_ttl_seconds,
            parent_token_hash=parent_token_hash,
        )
        self.store.save_token(
            token=refresh_token,
            token_type="refresh",
            client_id=client_id,
            scope=" ".join(scopes),
            resource=resource,
            subject=subject,
            expires_at=now + self.settings.refresh_token_ttl_seconds,
            parent_token_hash=parent_token_hash,
        )
        return _json_response(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": self.settings.access_token_ttl_seconds,
                "scope": " ".join(scopes),
                "refresh_token": refresh_token,
            },
            headers=NO_STORE_HEADERS,
        )

    def _validate_authorization_params(
        self,
        params: Mapping[str, str],
        client: RegisteredClient | None,
    ) -> tuple[str, str] | None:
        if params.get("response_type") != "code":
            return "unsupported_response_type", "Only authorization code flow is supported."
        if client is None:
            return "invalid_request", "Client ID was not found."
        redirect_uri = params.get("redirect_uri")
        if not redirect_uri or redirect_uri not in client.redirect_uris:
            return "invalid_request", "redirect_uri is not registered for this client."
        if params.get("code_challenge_method") != "S256":
            return "invalid_request", "PKCE code_challenge_method must be S256."
        if not params.get("code_challenge"):
            return "invalid_request", "PKCE code_challenge is required."
        if params.get("resource") != self.settings.resource_url:
            return "invalid_target", "resource must identify this MCP server."

        scopes = _requested_authorization_scopes(params.get("scope"), client)
        if READ_SCOPE not in scopes:
            return "invalid_scope", f"{READ_SCOPE} is required."
        if not set(scopes).issubset(client.scopes):
            return "invalid_scope", "Requested scopes exceed the registered client scope."
        return None

    def _authorization_error_response(
        self,
        *,
        client: RegisteredClient | None,
        redirect_uri: str | None,
        state: str | None,
        error: str,
        description: str,
    ) -> Response:
        if client is not None and redirect_uri in client.redirect_uris:
            return RedirectResponse(
                url=_redirect_with_params(
                    redirect_uri or "",
                    error=error,
                    error_description=description,
                    state=state,
                    iss=self.settings.issuer_url,
                ),
                status_code=302,
                headers=NO_STORE_HEADERS,
            )
        return _json_response(
            {
                "error": error,
                "error_description": description,
                "iss": self.settings.issuer_url,
                "state": state,
            },
            status_code=400,
            headers=NO_STORE_HEADERS,
        )

    def _login_page(self, pending: PendingAuthorization, error: str | None = None) -> HTMLResponse:
        csrf = secrets.token_urlsafe(32)
        message = f"<p class=\"error\">{html.escape(error)}</p>" if error else ""
        response = HTMLResponse(
            _html_document(
                "Google Ads MCP owner login",
                f"""
                <h1>Google Ads MCP</h1>
                <p>Sign in as the service owner to review this request.</p>
                {message}
                <form method="post" action="{OWNER_LOGIN_PATH}">
                  <input type="hidden" name="request" value="{html.escape(pending.request_id)}">
                  <input type="hidden" name="csrf_token" value="{html.escape(csrf)}">
                  <label>Username <input name="username" autocomplete="username" required></label>
                  <label>Password
                    <input type="password" name="password" autocomplete="current-password" required>
                  </label>
                  <button type="submit">Continue</button>
                </form>
                """,
            ),
            headers=HTML_SECURITY_HEADERS,
        )
        _set_csrf_cookie(response, csrf)
        return response

    def _approval_page(
        self,
        pending: PendingAuthorization,
        client: RegisteredClient | None,
        error: str | None = None,
    ) -> HTMLResponse:
        csrf = secrets.token_urlsafe(32)
        client_name = client.client_name if client and client.client_name else pending.client_id
        message = f"<p class=\"error\">{html.escape(error)}</p>" if error else ""
        response = HTMLResponse(
            _html_document(
                "Approve Google Ads MCP access",
                f"""
                <h1>Approve Google Ads MCP access</h1>
                <p><strong>{html.escape(client_name)}</strong> is requesting read-only access.</p>
                <dl>
                  <dt>Scope</dt><dd>{html.escape(pending.scope)}</dd>
                  <dt>Resource</dt><dd>{html.escape(pending.resource)}</dd>
                </dl>
                {message}
                <form method="post" action="{OWNER_APPROVE_PATH}">
                  <input type="hidden" name="request" value="{html.escape(pending.request_id)}">
                  <input type="hidden" name="csrf_token" value="{html.escape(csrf)}">
                  <button type="submit" name="decision" value="approve">Approve</button>
                  <button type="submit" name="decision" value="deny">Deny</button>
                </form>
                """,
            ),
            headers=HTML_SECURITY_HEADERS,
        )
        _set_csrf_cookie(response, csrf)
        return response

    def _authenticated_owner(self, request: Request) -> str | None:
        session_token = request.cookies.get(SESSION_COOKIE)
        if not session_token:
            return None
        return self.store.owner_session_subject(session_token)

    def _owner_credentials_are_valid(self, username: str, password: str) -> bool:
        if not hmac.compare_digest(username.encode(), self.settings.owner_username.encode()):
            return False
        try:
            return self._password_hasher.verify(self.settings.owner_password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def _authenticate_client(self, request: Request, form: Mapping[str, Any]) -> RegisteredClient:
        basic_client_id = None
        basic_client_secret = None
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                basic_client_id, basic_client_secret = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError) as exc:
                raise OAuthClientAuthError("Invalid Basic authentication header.") from exc

        client_id = basic_client_id or _form_string(form, "client_id")
        if not client_id:
            raise OAuthClientAuthError("Missing client_id.")
        client = self.store.get_client(client_id)
        if client is None:
            raise OAuthClientAuthError("Invalid client_id.")

        if client.token_endpoint_auth_method == "none":
            return client

        if client.token_endpoint_auth_method == "client_secret_basic":
            if basic_client_id != client.client_id or not basic_client_secret:
                raise OAuthClientAuthError("Client secret is required.")
            candidate = basic_client_secret
        elif client.token_endpoint_auth_method == "client_secret_post":
            candidate = _form_string(form, "client_secret")
            if not candidate:
                raise OAuthClientAuthError("Client secret is required.")
        else:
            raise OAuthClientAuthError("Unsupported client authentication method.")

        if not client.client_secret_hash:
            raise OAuthClientAuthError("Client is missing a secret.")
        candidate_hash = self.store.token_hash(candidate, "client_secret")
        if not hmac.compare_digest(candidate_hash.encode(), client.client_secret_hash.encode()):
            raise OAuthClientAuthError("Invalid client_secret.")
        if client.client_secret_expires_at and client.client_secret_expires_at < int(time.time()):
            raise OAuthClientAuthError("Client secret has expired.")
        return client

    def _authorization_code_is_active(self, record: AuthorizationCodeRecord) -> bool:
        return (
            record.used_at is None
            and record.revoked_at is None
            and record.expires_at >= int(time.time())
        )

    def _token_record_is_active(self, record: TokenRecord) -> bool:
        return (
            record.revoked_at is None
            and (record.expires_at is None or record.expires_at >= int(time.time()))
        )

    def _log_register_diagnostic(
        self,
        request: Request,
        response: Response,
        payload: object | None,
        response_payload: Mapping[str, Any] | None,
    ) -> None:
        if not _oauth_diagnostics_enabled():
            return
        request_fields = _register_request_diagnostic_fields(payload)
        response_fields = _register_response_diagnostic_fields(response_payload)
        self._logger.warning(
            json.dumps(
                {
                    "event": "mcp_oauth_register",
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    **request_fields,
                    **response_fields,
                },
                sort_keys=True,
            )
        )

    def _log_authorize_diagnostic(
        self,
        request: Request,
        response: Response,
        params: Mapping[str, str],
        client: RegisteredClient | None,
        *,
        validation_error: str | None,
        next_step: str,
    ) -> None:
        if not _oauth_diagnostics_enabled():
            return
        self._logger.warning(
            json.dumps(
                {
                    "event": "mcp_oauth_authorize",
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "client_id_present": bool(params.get("client_id")),
                    "client_registered": client is not None,
                    "code_challenge_method": _diagnostic_string(
                        params.get("code_challenge_method")
                    ),
                    "code_challenge_present": bool(params.get("code_challenge")),
                    "next_step": next_step,
                    "redirect_uri": _diagnostic_string(params.get("redirect_uri")),
                    "requested_scopes": _scope_list(params.get("scope", "")),
                    "resource": _diagnostic_string(params.get("resource")),
                    "response_type": _diagnostic_string(params.get("response_type")),
                    "state_present": bool(params.get("state")),
                    "validation_error": validation_error,
                },
                sort_keys=True,
            )
        )


class OAuthClientAuthError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


async def _request_params(request: Request) -> dict[str, str]:
    raw_params = request.query_params if request.method == "GET" else await request.form()
    params: dict[str, str] = {}
    for key, value in raw_params.items():
        if isinstance(value, str):
            params[str(key)] = value
    return params


def _requested_authorization_scopes(scope: str | None, client: RegisteredClient) -> list[str]:
    if scope:
        return _scope_list(scope)
    return [READ_SCOPE] if READ_SCOPE in client.scopes else client.scopes


def _scope_list(scope: str) -> list[str]:
    return [part for part in scope.split() if part]


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _oauth_diagnostics_enabled() -> bool:
    value = os.getenv(GOOGLE_ADS_MCP_HTTP_DIAGNOSTICS_ENV_VAR)
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _register_request_diagnostic_fields(payload: object | None) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "request_client_name": None,
        "request_grant_types": None,
        "request_payload_type": _diagnostic_type(payload),
        "request_redirect_uris": None,
        "request_response_types": None,
        "request_scopes": None,
        "request_software_id": None,
        "request_token_endpoint_auth_method": None,
    }
    if not isinstance(payload, dict):
        return fields

    scope = payload.get("scope")
    fields.update(
        {
            "request_client_name": _diagnostic_string(payload.get("client_name")),
            "request_grant_types": _diagnostic_string_list(payload.get("grant_types")),
            "request_redirect_uris": _diagnostic_string_list(payload.get("redirect_uris")),
            "request_response_types": _diagnostic_string_list(payload.get("response_types")),
            "request_scopes": _scope_list(scope) if isinstance(scope, str) else None,
            "request_software_id": _diagnostic_string(payload.get("software_id")),
            "request_token_endpoint_auth_method": _diagnostic_string(
                payload.get("token_endpoint_auth_method")
            ),
        }
    )
    return fields


def _register_response_diagnostic_fields(
    response_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "response_client_id_issued": False,
        "response_client_secret_issued": False,
        "response_grant_types": None,
        "response_redirect_uris": None,
        "response_response_types": None,
        "response_scopes": None,
        "response_token_endpoint_auth_method": None,
    }
    if response_payload is None:
        return fields

    scope = response_payload.get("scope")
    fields.update(
        {
            "response_client_id_issued": isinstance(response_payload.get("client_id"), str),
            "response_client_secret_issued": isinstance(
                response_payload.get("client_secret"),
                str,
            ),
            "response_grant_types": _diagnostic_string_list(
                response_payload.get("grant_types")
            ),
            "response_redirect_uris": _diagnostic_string_list(
                response_payload.get("redirect_uris")
            ),
            "response_response_types": _diagnostic_string_list(
                response_payload.get("response_types")
            ),
            "response_scopes": _scope_list(scope) if isinstance(scope, str) else None,
            "response_token_endpoint_auth_method": _diagnostic_string(
                response_payload.get("token_endpoint_auth_method")
            ),
        }
    )
    return fields


def _diagnostic_string(value: object, *, max_length: int = 200) -> str | None:
    return value[:max_length] if isinstance(value, str) else None


def _diagnostic_string_list(value: object) -> list[str] | None:
    if not _string_list(value):
        return None
    return [_diagnostic_string(item) or "" for item in value]


def _diagnostic_type(value: object) -> str:
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


def _form_string(form: Mapping[str, Any], name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _is_safe_redirect_uri(value: str) -> bool:
    if "*" in value:
        return False
    parsed = urlparse(value)
    if parsed.fragment or parsed.username or parsed.password:
        return False
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    if parsed.scheme == "http":
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    return False


def _s256_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _redirect_with_params(base_url: str, **params: str | None) -> str:
    parsed = urlparse(base_url)
    query_params = [
        (key, value)
        for key, values in parse_qs(parsed.query).items()
        for value in values
    ]
    query_params.extend((key, value) for key, value in params.items() if value is not None)
    return urlunparse(parsed._replace(query=urlencode(query_params)))


def _json_response(
    payload: dict[str, Any],
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=dict(headers or {}))


def _registration_error(error: str, description: str) -> JSONResponse:
    return _json_response(
        {"error": error, "error_description": description},
        status_code=400,
        headers=NO_STORE_HEADERS,
    )


def _oauth_error(error: str, description: str, *, status_code: int = 400) -> JSONResponse:
    return _json_response(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers=NO_STORE_HEADERS,
    )


def _plain_error(message: str, *, status_code: int = 400) -> Response:
    return Response(message, status_code=status_code, headers=NO_STORE_HEADERS)


def _options_response(methods: str) -> Response:
    return Response(
        status_code=204,
        headers={
            "Allow": methods,
            "Access-Control-Allow-Methods": methods,
            "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version",
        },
    )


def _set_csrf_cookie(response: Response, csrf: str) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=DEFAULT_AUTH_CODE_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def _csrf_is_valid(request: Request, form: Mapping[str, Any]) -> bool:
    cookie_token = request.cookies.get(CSRF_COOKIE)
    form_token = _form_string(form, "csrf_token")
    return bool(
        cookie_token
        and form_token
        and hmac.compare_digest(cookie_token.encode(), form_token.encode())
    )


def _html_document(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      margin: 2rem auto;
      max-width: 34rem;
      line-height: 1.5;
      color: #111;
    }}
    form {{ display: grid; gap: 1rem; }}
    label {{ display: grid; gap: 0.35rem; font-weight: 600; }}
    input {{ font: inherit; padding: 0.6rem; border: 1px solid #777; border-radius: 4px; }}
    button {{
      font: inherit;
      padding: 0.65rem 0.9rem;
      border-radius: 4px;
      border: 1px solid #333;
      background: #111;
      color: #fff;
      cursor: pointer;
    }}
    button[value="deny"] {{ background: #fff; color: #111; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.5rem 1rem; }}
    dt {{ font-weight: 700; }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    .error {{ color: #9f1239; font-weight: 600; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigurationError(
            f"{name} is required when GOOGLE_ADS_MCP_AUTH_MODE=oauth.",
            code="missing_mcp_oauth_configuration",
        )
    return value.strip()


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _chmod_best_effort(path: Path, mode: int) -> None:
    with suppress(OSError):
        path.chmod(mode)
