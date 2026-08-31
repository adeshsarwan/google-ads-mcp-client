"""Credential-safe structured logging helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any


SENSITIVE_KEY_PARTS = (
    "authorization",
    "client_secret",
    "credential",
    "developer_token",
    "password",
    "refresh_token",
    "secret",
    "token",
)


class StructuredLogger:
    """Small wrapper around stdlib logging that redacts secret-looking fields."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("google_ads_function_gateway")

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, fields)

    def _log(self, level: int, event: str, fields: dict[str, Any]) -> None:
        payload = {"event": event, **redact(fields)}
        self._logger.log(level, json.dumps(payload, sort_keys=True, default=str))


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in SENSITIVE_KEY_PARTS):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact(item)
        return redacted
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value
