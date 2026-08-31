"""OAuth helpers for Google Ads local setup and live API calls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from google_ads_function_gateway.config import GoogleAdsSettings
from google_ads_function_gateway.exceptions import ConfigurationError

GOOGLE_ADS_OAUTH_SCOPE = "https://www.googleapis.com/auth/adwords"
LOOPBACK_REDIRECT_HOST = "127.0.0.1"
DEFAULT_LOOPBACK_REDIRECT_PORT = 8080


class OAuthFlow(Protocol):
    def run_local_server(self, **kwargs: Any) -> Any:
        """Run a browser OAuth flow and return Google credentials."""


OAuthFlowFactory = Callable[[dict[str, Any], list[str]], OAuthFlow]


def load_oauth_settings_from_env() -> GoogleAdsSettings:
    """Return centralized settings for OAuth-backed live Google Ads calls."""

    return GoogleAdsSettings.from_env()


def generate_google_ads_refresh_token(
    *,
    settings: GoogleAdsSettings,
    port: int = DEFAULT_LOOPBACK_REDIRECT_PORT,
    open_browser: bool = True,
    flow_factory: OAuthFlowFactory | None = None,
) -> str:
    """Run the local Google OAuth flow and return a refresh token."""

    _require_oauth_client(settings)
    flow_factory = flow_factory or _installed_app_flow_factory
    flow = flow_factory(
        _client_config(settings, port=port),
        [GOOGLE_ADS_OAUTH_SCOPE],
    )
    credentials = flow.run_local_server(
        host=LOOPBACK_REDIRECT_HOST,
        bind_addr=LOOPBACK_REDIRECT_HOST,
        port=port,
        authorization_prompt_message=(
            "Open this URL to authorize Google Ads local setup:\n{url}\n"
        ),
        success_message=(
            "Google Ads OAuth authorization completed. You may close this window."
        ),
        open_browser=open_browser,
        access_type="offline",
        prompt="consent",
    )
    refresh_token = getattr(credentials, "refresh_token", None)
    if not refresh_token:
        raise ConfigurationError(
            "Google did not return a refresh token. Re-run the command and approve consent.",
            code="missing_refresh_token_from_oauth_flow",
        )
    return str(refresh_token)


def _require_oauth_client(settings: GoogleAdsSettings) -> None:
    missing = [
        name
        for name, value in (
            ("GOOGLE_ADS_CLIENT_ID", settings.client_id),
            ("GOOGLE_ADS_CLIENT_SECRET", settings.client_secret),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(
            "Google Ads OAuth client configuration is incomplete.",
            code="missing_oauth_client_configuration",
            context={"missing": missing},
        )


def _client_config(settings: GoogleAdsSettings, *, port: int) -> dict[str, Any]:
    return {
        "web": {
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"http://{LOOPBACK_REDIRECT_HOST}:{port}/"],
        }
    }


def _installed_app_flow_factory(client_config: dict[str, Any], scopes: list[str]) -> OAuthFlow:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise ConfigurationError(
            "google-auth-oauthlib is required to generate a refresh token.",
            code="missing_google_auth_oauthlib_dependency",
        ) from exc

    return InstalledAppFlow.from_client_config(client_config, scopes=scopes)
