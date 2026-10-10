import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

from hermes_kubernetes.runtime.configuration import route_mcp
from hermes_kubernetes.runtime.health import healthy
from hermes_kubernetes.mcp.bridge import (
    REMOTE,
    build_ssh_command,
    build_mcp_command,
    profile_name,
    settings,
)

spec = importlib.util.spec_from_file_location(
    "snapshot", ROOT / "hermes_kubernetes/backup/snapshot.py"
)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class RoutingTests(unittest.TestCase):
    def test_stdio_secrets_stay_out_of_spawn_arguments_and_http_is_preserved(self):
        config = {
            "model": {"provider": "moa"},
            "mcp_servers": {
                "local": {
                    "command": "npx",
                    "args": ["server", "--api-key", "fixture-secret"],
                    "env": {"PRIVATE_TOKEN": "fixture-secret"},
                    "timeout": 120,
                },
                "remote": {
                    "url": "https://example.test/mcp",
                    "headers": {"Authorization": "fixture-secret"},
                },
            },
        }
        routed, definitions = route_mcp(config)
        self.assertNotIn("fixture-secret", json.dumps(routed["mcp_servers"]["local"]))
        self.assertEqual(definitions["local"], config["mcp_servers"]["local"])
        self.assertEqual(routed["model"], config["model"])
        self.assertEqual(routed["mcp_servers"]["remote"], config["mcp_servers"]["remote"])
        self.assertEqual(route_mcp(routed, definitions), (routed, definitions))
        self.assertEqual(config["mcp_servers"]["local"]["command"], "npx")

    def test_missing_original_definition_fails_instead_of_recursing(self):
        config = {
            "mcp_servers": {
                "x": {
                    "command": "python",
                    "args": [
                        "/opt/hermes-personal/hermes_kubernetes/mcp/bridge.py",
                        "x",
                    ],
                }
            }
        }
        with self.assertRaises(ValueError):
            route_mcp(config)


class ProbeTests(unittest.TestCase):
    def test_stale_or_stopped_gateway_is_not_ready(self):
        now = datetime.now(timezone.utc)
        record = {
            "pid": os.getpid(),
            "gateway_state": "running",
            "updated_at": now.isoformat(),
        }
        self.assertTrue(healthy(record, now))
        record["updated_at"] = (now - timedelta(minutes=5)).isoformat()
        self.assertFalse(healthy(record, now))
        record.update(updated_at=now.isoformat(), gateway_state="stopped")
        self.assertFalse(healthy(record, now))


class StdioBridgeTests(unittest.TestCase):
    def test_new_workspace_connectors_use_the_mounted_executor_and_explicit_targets_win(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            policy = home / "policy"
            policy.mkdir()
            settings = {
                "mcp": {
                    "host": "api-mcp",
                    "workspace_target": {
                        "host": "workspace-mcp",
                        "roots": ["/home/example/projects"],
                    },
                    "servers": {"browser": {"host": "browser-mcp"}},
                }
            }
            (policy / "instance.json").write_text(json.dumps(settings))
            for definition in [
                {"cwd": "/home/example/projects/new"},
                {"args": ["file:/home/example/projects/new"]},
            ]:
                self.assertIn(
                    "mcp@workspace-mcp",
                    build_mcp_command(home, policy, "new", definition),
                )
                self.assertIn(
                    "mcp@browser-mcp",
                    build_mcp_command(home, policy, "browser", definition),
                )
            self.assertIn(
                "mcp@api-mcp",
                build_mcp_command(home, policy, "new", {"args": ["/home/example/projects-other"]}),
            )

    def test_server_targets_keep_api_credentials_separate_from_workspace_and_browser(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            policy = home / "policy"
            policy.mkdir()
            settings = {
                "ssh_user": "terminal-user",
                "mcp": {
                    "host": "api-mcp",
                    "port": 2222,
                    "servers": {
                        "codegraph": {
                            "host": "workspace-mcp",
                            "host_key_alias": "hermes-mcp-codegraph",
                        },
                        "playwright": {
                            "host": "browser-mcp",
                            "port": 2223,
                            "host_key_alias": "hermes-mcp-browser",
                        },
                    },
                },
            }
            path = policy / "instance.json"
            path.write_text(json.dumps(settings))
            api = build_mcp_command(home, policy, "github")
            graph = build_mcp_command(home, policy, "codegraph")
            browser = build_mcp_command(home, policy, "playwright")
            self.assertIn("mcp@api-mcp", api)
            self.assertIn("mcp@workspace-mcp", graph)
            self.assertIn("HostKeyAlias=hermes-mcp-codegraph", graph)
            self.assertIn("mcp@browser-mcp", browser)
            self.assertEqual(browser[browser.index("-p") + 1], "2223")
            settings["mcp"]["servers"]["playwright"]["host_key_alias"] = (
                "bad\nProxyCommand injected"
            )
            path.write_text(json.dumps(settings))
            with self.assertRaises(ValueError):
                build_mcp_command(home, policy, "playwright")
            self.assertIn("mcp@api-mcp", build_mcp_command(home, policy, "github"))

    def test_pod_target_uses_its_own_key_and_survives_native_env_filtering(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            policy = home / "policy"
            policy.mkdir()
            (policy / "instance.json").write_text(
                json.dumps(
                    {
                        "ssh_user": "terminal-user",
                        "mcp": {
                            "host": "hermes-mcp.example.svc.cluster.local",
                            "port": 2222,
                        },
                    }
                )
            )
            with patch.dict(os.environ, {"HOME": str(home), "PATH": os.defpath}, clear=True):
                command = build_mcp_command(home, policy)
            self.assertIn("mcp@hermes-mcp.example.svc.cluster.local", command)
            self.assertEqual(command[-1], "hermes-mcp")
            self.assertEqual(command[command.index("-i") + 1], str(home / ".ssh/mcp/id_ed25519"))
            self.assertIn(f"UserKnownHostsFile={home / '.ssh/mcp/known_hosts'}", command)
            self.assertNotIn("terminal-user@hermes-terminal", command)
            self.assertEqual(profile_name(home / "profiles/analyst", home), "analyst")
            with self.assertRaises(ValueError):
                profile_name(home / "other/analyst", home)

    def test_profile_selection_survives_the_native_environment_filter(self):
        with patch.dict(os.environ, {"HOME": "/root-state", "PATH": os.defpath}, clear=True):
            name, state, ssh, definition = settings(
                [
                    "shared",
                    "--state-home",
                    "/root-state/profiles/analyst",
                    "--ssh-home",
                    "/root-state",
                ]
            )
        self.assertIsNone(definition)
        self.assertEqual(name, "shared")
        self.assertEqual(state, Path("/root-state/profiles/analyst"))
        self.assertEqual(ssh, Path("/root-state"))

    def test_connection_survives_the_native_stdio_environment_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            policy = home / "policy"
            policy.mkdir()
            (policy / "instance.json").write_text(
                json.dumps({"ssh_user": "fixture", "terminal_port": 2222})
            )
            # The native MCP launcher forwards HOME/PATH, but drops TERMINAL_SSH_*.
            with patch.dict(os.environ, {"HOME": str(home), "PATH": os.defpath}, clear=True):
                command = build_ssh_command(home, policy)
            self.assertIn("fixture@hermes-terminal", command)
            self.assertEqual(command[command.index("-p") + 1], "2222")
            self.assertEqual(command[command.index("-i") + 1], str(home / ".ssh/id_ed25519"))
            self.assertIn(f"UserKnownHostsFile={home / '.ssh/known_hosts'}", command)

    def test_transport_preserves_protocol_and_remote_environment(self):
        program = "import os,sys; print(os.environ['TEST_VALUE'],flush=True); [print(line.rstrip(),flush=True) for line in sys.stdin]"
        definition = {
            "command": sys.executable,
            "args": ["-u", "-c", program],
            "cwd": str(ROOT),
            "env": {"TEST_VALUE": "value with spaces; literal $(text)"},
        }
        payload = json.dumps(definition).encode() + b'\n{"jsonrpc":"2.0","id":1}\n'
        result = subprocess.run(
            [sys.executable, "-u", "-c", REMOTE],
            input=payload,
            capture_output=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(
            result.stdout.splitlines(),
            [b"value with spaces; literal $(text)", b'{"jsonrpc":"2.0","id":1}'],
        )

    def test_default_mcp_directory_is_the_native_home_with_both_python_install_paths(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".hermes").mkdir()
            program = (
                "import os,json; print(json.dumps({'cwd':os.getcwd(),'path':os.environ['PATH']}))"
            )
            definition = {"command": sys.executable, "args": ["-c", program]}
            result = subprocess.run(
                [sys.executable, "-u", "-c", REMOTE],
                input=json.dumps(definition).encode() + b"\n",
                capture_output=True,
                env={**os.environ, "HOME": str(home)},
                check=True,
                timeout=10,
            )
            details = json.loads(result.stdout)
            self.assertEqual(Path(details["cwd"]), (home / ".hermes").resolve())
            self.assertIn(str(home / ".hermes/hermes-agent/.venv/bin"), details["path"])
            self.assertIn(str(home / ".hermes/hermes-agent/venv/bin"), details["path"])


class SnapshotTests(unittest.TestCase):
    def test_dangling_symlink_aborts_snapshot_instead_of_silently_losing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "state"
            home.mkdir()
            (home / "personal-file").symlink_to(base / "missing-file")
            with patch.object(snapshot, "require_stopped"):
                with self.assertRaises(RuntimeError):
                    snapshot.snapshot(home, base / "example.tar.gz")

    def test_database_wal_is_included_via_backup_and_runtime_files_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "state"
            home.mkdir()
            (home / "config.yaml").write_text("model: {provider: moa}\n")
            (home / ".env").write_text("TOKEN=fixture\n")
            (home / "gateway.pid").write_text("123")
            (home / "hermes-agent").mkdir()
            (home / "hermes-agent/code.py").write_text("do not migrate")
            db = sqlite3.connect(home / "state.sqlite3")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE memories (text TEXT)")
            db.execute("INSERT INTO memories VALUES ('preserved')")
            db.commit()
            archive = base / "example.tar.gz"
            with patch.object(snapshot, "require_stopped"):
                snapshot.snapshot(home, archive)
            db.close()
            with tarfile.open(archive) as handle:
                names = handle.getnames()
                self.assertIn(".env", names)
                self.assertNotIn("gateway.pid", names)
                self.assertNotIn("state.sqlite3-wal", names)
                self.assertFalse(any(name.startswith("hermes-agent/") for name in names))
                restored = base / "restored"
                handle.extractall(restored, filter="data")
            with closing(sqlite3.connect(restored / "state.sqlite3")) as recovered:
                self.assertEqual(
                    recovered.execute("SELECT text FROM memories").fetchone(),
                    ("preserved",),
                )
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
