from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from google_ads_function_gateway.env import find_project_root, load_local_env


class EnvLoadingTests(unittest.TestCase):
    def test_find_project_root_from_source_path(self) -> None:
        root = Path(__file__).resolve().parents[2]

        self.assertEqual(
            find_project_root(root / "src" / "google_ads_function_gateway" / "env.py"),
            root,
        )

    def test_load_local_env_uses_project_root_from_nested_cwd(self) -> None:
        env_key = "GOOGLE_ADS_FUNCTION_GATEWAY_ENV_TEST_VALUE"
        original_cwd = Path.cwd()
        original_value = os.environ.pop(env_key, None)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_dir = root / "src" / "google_ads_function_gateway"
            package_dir.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\nname = \"test\"\n")
            (root / ".env").write_text(f"{env_key}=from-project-root\n")
            nested = root / "nested" / "child"
            nested.mkdir(parents=True)

            try:
                os.chdir(nested)
                loaded_path = load_local_env()
            finally:
                os.chdir(original_cwd)

        try:
            self.assertEqual(loaded_path, (root / ".env").resolve())
            self.assertEqual(os.environ.get(env_key), "from-project-root")
        finally:
            if original_value is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = original_value


if __name__ == "__main__":
    unittest.main()
