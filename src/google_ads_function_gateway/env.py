"""Local environment-file loading for CLI, MCP, and tests."""

from __future__ import annotations

from pathlib import Path


def find_project_root(start: str | Path | None = None) -> Path:
    """Find the local project root from a path inside the checkout."""

    search_points = [Path(start or Path.cwd()).resolve()]
    if start is None:
        search_points.append(Path(__file__).resolve())

    for point in search_points:
        current = point.parent if point.is_file() else point
        for candidate in (current, *current.parents):
            if (candidate / "pyproject.toml").exists() and (
                candidate / "src" / "google_ads_function_gateway"
            ).exists():
                return candidate
    return Path.cwd().resolve()


def load_local_env(path: str | Path | None = None) -> Path | None:
    """Load the ignored local project ``.env`` file when python-dotenv is installed."""

    env_path = Path(path) if path is not None else find_project_root() / ".env"
    if not env_path.exists():
        return None

    try:
        from dotenv import load_dotenv
    except ImportError:
        return None

    load_dotenv(env_path, override=False)
    return env_path
