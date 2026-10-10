"""CLI reads current profile MCP definitions without exposing configuration values."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

import hermes_kubernetes.mcp.updates as mcp_update  # noqa: E402


class McpUpdateCliTests(unittest.TestCase):
    def test_live_config_restores_wrapped_servers_from_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / "state"
            state.mkdir()
            bridge = "/opt/hermes-personal/hermes_kubernetes/mcp/bridge.py"
            config = {
                "mcp_servers": {
                    "known": {
                        "command": "/opt/hermes/.venv/bin/python",
                        "args": [bridge, "known"],
                        "enabled": True,
                    },
                    "new": {"command": "uvx", "args": ["new-mcp"], "enabled": True},
                }
            }
            (home / "config.yaml").write_text(json.dumps(config))
            (state / "hermes-ssh-mcp.json").write_text(
                json.dumps(
                    {
                        "known": {
                            "command": "npx",
                            "args": ["-y", "latest-known-mcp"],
                            "env": {"MODE": "old"},
                        }
                    }
                )
            )

            yaml_stub = types.SimpleNamespace(safe_load=json.loads)
            with patch.dict(sys.modules, {"hermes_yaml": yaml_stub}):
                result = mcp_update.definitions_from(home / "config.yaml", home)

            self.assertEqual(result["known"]["command"], "npx")
            self.assertEqual(result["known"]["args"], ["-y", "latest-known-mcp"])
            self.assertTrue(result["known"]["enabled"])
            self.assertEqual(
                result["new"], {"command": "uvx", "args": ["new-mcp"], "enabled": True}
            )

    def test_cli_defaults_to_live_config_and_explicit_definitions_still_win(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            policy = home / "policy"
            policy.mkdir()
            (policy / "instance.json").write_text(json.dumps({"mcp": {}}))
            definitions = home / "custom-definitions.json"
            definitions.write_text("{}")
            seen = []

            def capture(path, _state_home):
                seen.append(path)
                return {}

            with (
                patch.object(mcp_update, "definitions_from", side_effect=capture),
                patch.object(
                    mcp_update,
                    "execute",
                    return_value={"action": "list", "ok": True, "results": []},
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                args = [
                    "list",
                    "--all",
                    "--state-home",
                    str(home),
                    "--policy",
                    str(policy),
                ]
                self.assertEqual(mcp_update.main(args), 0)
                self.assertEqual(seen[-1], home.resolve() / "config.yaml")
                self.assertEqual(mcp_update.main(args + ["--definitions", str(definitions)]), 0)
                self.assertEqual(seen[-1], definitions)

    def test_all_keeps_per_server_http_and_failure_results(self):
        definitions = {
            "first": {"command": "npx"},
            "provider": {"url": "https://example.invalid"},
            "last": {"command": "uvx"},
        }
        rows = [
            {"name": "first", "status": "updated"},
            {"name": "provider", "status": "skipped-http", "provider_managed": True},
            {"name": "last", "status": "failed", "error": "fixture failure"},
        ]
        with patch.object(mcp_update, "run_request", side_effect=rows):
            result = mcp_update.execute(
                "apply",
                ["--all"],
                definitions,
                "default",
                Path("/tmp/fixture-home"),
                {},
                Path("/tmp/fixture-policy"),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["results"], rows)

    def test_successful_update_invalidates_versioning_cache_only_for_its_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cache = home / "state/versioning/mcp"
            cache.mkdir(parents=True)
            (cache / "default.json").write_text("previous versions")
            (cache / "second.json").write_text("other profile")
            with patch.object(
                mcp_update,
                "run_request",
                return_value={"name": "package", "status": "updated"},
            ):
                result = mcp_update.execute(
                    "apply",
                    ["package"],
                    {"package": {"command": "npx"}},
                    "default",
                    home,
                    {},
                    home,
                )
            self.assertTrue(result["ok"])
            self.assertFalse((cache / "default.json").exists())
            self.assertEqual((cache / "second.json").read_text(), "other profile")


if __name__ == "__main__":
    unittest.main()
