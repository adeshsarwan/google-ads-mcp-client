"""Local environment-file loading for CLI and tests."""

from __future__ import annotations

from pathlib import Path


def load_local_env(path: str | Path = ".env") -> None:
    """Load local env vars when python-dotenv is installed."""

    env_path = Path(path)
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(env_path, override=False)
