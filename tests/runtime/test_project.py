import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(os.environ.get("HERMES_SOURCE_DIR", ROOT / "hermes-agent")))]
from hermes_kubernetes.source.prepare import prepare as prepare_source
from hermes_kubernetes.source.tree import fingerprint
from hermes_kubernetes.runtime.bootstrap import (
    prepare,
    prepare_configuration,
    ssh_configuration,
    ensure_home_alias,
)
from hermes_kubernetes.terminal.diagnostics import (
    collect,
    REMOTE as RECEIVE_DIAGNOSTICS,
)

spec = importlib.util.spec_from_file_location(
    "workspace_sync", ROOT / "hermes_kubernetes/terminal/sync.py"
)
workspace_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace_sync)


class BootstrapTests(unittest.TestCase):
    def test_pod_credentials_are_separate_and_missing_credentials_abort_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home, bundle, policy, ssh, source, mcp = [
                base / n for n in ("home", "bundle", "policy", "ssh", "source", "mcp")
            ]
            for path in (bundle / "config", policy, ssh, source, mcp):
                path.mkdir(parents=True)
            (bundle / "config/hermes.yaml").write_bytes((ROOT / "config/hermes.yaml").read_bytes())
            (policy / "instance.json").write_text(
                json.dumps(
                    {
                        "name": "fixture",
                        "network": "cluster",
                        "terminal_host": "192.0.2.116",
                        "model": "selected",
                        "timezone": "UTC",
                        "mcp": {"host": "hermes-mcp.example.svc.cluster.local"},
                    }
                )
            )
            (source / "source-provenance.json").write_text("{}")
            for name in ("id_ed25519", "known_hosts"):
                (ssh / name).write_text("terminal-" + name)
            with self.assertRaises(FileNotFoundError):
                prepare(home, bundle, policy, ssh, source, mcp)
            for name in ("id_ed25519", "known_hosts"):
                (mcp / name).write_text("mcp-" + name)
            prepare(home, bundle, policy, ssh, source, mcp)
            self.assertEqual((home / ".ssh/id_ed25519").read_text(), "terminal-id_ed25519")
            self.assertEqual((home / ".ssh/mcp/id_ed25519").read_text(), "mcp-id_ed25519")
            self.assertEqual((home / ".ssh/mcp/id_ed25519").stat().st_mode & 0o777, 0o600)

    def test_same_named_profile_mcp_keeps_its_credentials_and_comments_across_boots(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            profile = home / "profiles/analyst"
            profile.mkdir(parents=True)
            (home / "config.yaml").write_text(
                "# personal note\nmcp_servers:\n  shared:\n    command: root-command\n    env: {KEY: root-key}\nmodel: {provider: moa}\n"
            )
            (profile / "config.yaml").write_text(
                "# profile note\nmcp_servers:\n  shared:\n    command: profile-command\n    env: {KEY: profile-key}\nmodel: {provider: profile-provider}\n"
            )
            root_definitions = prepare_configuration(home, home)
            definitions = prepare_configuration(profile, home, root_definitions)
            self.assertEqual(definitions["shared"]["env"]["KEY"], "profile-key")
            self.assertEqual(root_definitions["shared"]["env"]["KEY"], "root-key")
            routed = yaml.safe_load((profile / "config.yaml").read_text())
            self.assertEqual(routed["model"]["provider"], "profile-provider")
            self.assertEqual(routed["mcp_servers"]["shared"]["command"], "profile-command")
            self.assertEqual(routed["mcp_servers"]["shared"]["env"]["KEY"], "profile-key")
            self.assertIn("# profile note", (profile / "config.yaml").read_text())
            self.assertEqual(prepare_configuration(profile, home, root_definitions), definitions)

    def test_cloned_default_bridge_imports_only_the_explicitly_inherited_definition(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.yaml").write_text("mcp_servers: {shared: {command: root-command}}\n")
            definitions = prepare_configuration(root, root)
            profile = root / "profiles/clone"
            profile.mkdir(parents=True)
            (profile / "config.yaml").write_text(
                "mcp_servers:\n  shared:\n    command: python\n    args: [/opt/hermes-personal/hermes_kubernetes/mcp/bridge.py, shared]\n    enabled: false\n"
            )
            copied = prepare_configuration(profile, root, definitions)
            self.assertEqual(copied["shared"]["command"], "root-command")
            self.assertFalse(copied["shared"]["enabled"])
            self.assertEqual(prepare_configuration(profile, root, definitions), copied)

    def test_home_alias_preserves_legacy_profile_and_credential_lookups(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "profiles/analyst").mkdir(parents=True)
            (home / "profiles/analyst/SOUL.md").write_text("preserved profile")
            (home / "config.yaml").write_text("preserved config")
            with patch.object(Path, "home", return_value=home):
                ensure_home_alias(home)
                ensure_home_alias(home)
                self.assertEqual(
                    (Path.home() / ".hermes/profiles/analyst/SOUL.md").read_text(),
                    "preserved profile",
                )
                self.assertEqual(
                    (Path.home() / ".hermes/config.yaml").resolve(),
                    (home / "config.yaml").resolve(),
                )
            self.assertTrue((home / ".hermes").is_symlink())

    def test_home_alias_refuses_an_existing_owned_path_and_is_not_added_to_a_native_home(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / ".hermes"
            home.mkdir()
            with patch.object(Path, "home", return_value=base):
                ensure_home_alias(home)
                self.assertFalse((home / ".hermes").exists())
            (home / ".hermes").mkdir()
            (home / ".hermes/personal").write_text("preserve")
            with (
                patch.object(Path, "home", return_value=home),
                self.assertRaises(ValueError),
            ):
                ensure_home_alias(home)
            self.assertEqual((home / ".hermes/personal").read_text(), "preserve")

    def test_empty_state_initializes_without_an_import_and_keeps_preferences(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home, bundle, policy, ssh, source = [
                base / name for name in ("home", "bundle", "policy", "ssh", "source")
            ]
            for path in (bundle / "config", policy, ssh, source):
                path.mkdir(parents=True)
            (bundle / "config/hermes.yaml").write_bytes((ROOT / "config/hermes.yaml").read_bytes())
            proxy_url = "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317/v1"
            (policy / "instance.json").write_text(
                json.dumps(
                    {
                        "name": "fixture",
                        "network": "cluster",
                        "terminal_host": "192.0.2.116",
                        "model": "selected",
                        "timezone": "UTC",
                        "proxy": {
                            "mode": "external",
                            "namespace": "cli-proxy-api",
                            "base_url": proxy_url,
                        },
                    }
                )
            )
            for name in ("id_ed25519", "known_hosts"):
                (ssh / name).write_text("fixture")
            (source / "source-provenance.json").write_text(
                json.dumps({"project_revision": "fixture", "source_tree_sha256": "fixture"})
            )
            prepare(home, bundle, policy, ssh, source)
            config = yaml.safe_load((home / "config.yaml").read_text())
            self.assertEqual(config["model"]["default"], "selected")
            self.assertEqual(config["model"]["base_url"], proxy_url)
            config["personal_preference"] = "kept"
            (home / "config.yaml").write_text(yaml.safe_dump(config))
            prepare(home, bundle, policy, ssh, source)
            self.assertEqual(
                yaml.safe_load((home / "config.yaml").read_text())["personal_preference"],
                "kept",
            )
            self.assertEqual((home / ".ssh/id_ed25519").stat().st_mode & 0o777, 0o600)

    def test_dedicated_transport_uses_socks_and_rejects_an_injected_target(self):
        config = ssh_configuration(
            {"network": "dedicated", "terminal_host": "vm.friend.ts.net"},
            Path("/state"),
        )
        self.assertIn(
            "ProxyCommand /usr/bin/ncat --proxy-type socks5 --proxy 127.0.0.1:1055 %h %p",
            config,
        )
        with self.assertRaises(ValueError):
            ssh_configuration(
                {"network": "dedicated", "terminal_host": "vm\nProxyCommand bad"},
                Path("/state"),
            )


class WorkspaceTests(unittest.TestCase):
    def test_remote_updates_apply_but_local_edits_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, publisher, workspace = [
                root / name for name in ("remote", "publisher", "workspace")
            ]

            def git(path, *args):
                return subprocess.run(
                    ["git", "-C", str(path), *args],
                    capture_output=True,
                    text=True,
                    check=True,
                )

            remote.mkdir()
            git(remote, "init", "--bare")
            publisher.mkdir()
            git(publisher, "init", "-b", "main")
            git(publisher, "config", "user.name", "fixture")
            git(publisher, "config", "user.email", "fixture@example.test")
            (publisher / "README.md").write_text("original")
            (publisher / "project-files.json").write_bytes(
                (ROOT / "project-files.json").read_bytes()
            )
            (publisher / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
            git(publisher, "add", ".")
            git(publisher, "commit", "-m", "initial")
            git(publisher, "remote", "add", "origin", str(remote))
            git(publisher, "push", "-u", "origin", "main")
            subprocess.run(
                ["git", "clone", "-b", "main", str(remote), str(workspace)],
                capture_output=True,
                check=True,
            )
            (publisher / "README.md").write_text("published")
            git(publisher, "commit", "-am", "update")
            git(publisher, "push")
            self.assertEqual(workspace_sync.sync(workspace), "updated")
            self.assertEqual((workspace / "README.md").read_text(), "published")
            (workspace / "README.md").write_text("local-work")
            self.assertEqual(workspace_sync.sync(workspace), "local-changes")
            self.assertEqual((workspace / "README.md").read_text(), "local-work")
            git(workspace, "config", "user.name", "fixture")
            git(workspace, "config", "user.email", "fixture@example.test")
            git(workspace, "commit", "-am", "local-commit")
            local_head = git(workspace, "rev-parse", "HEAD").stdout.strip()
            (publisher / "README.md").write_text("new-remote-work")
            git(publisher, "commit", "-am", "remote-commit")
            git(publisher, "push")
            self.assertEqual(workspace_sync.sync(workspace), "diverged")
            self.assertEqual(git(workspace, "rev-parse", "HEAD").stdout.strip(), local_head)
            self.assertEqual((workspace / "README.md").read_text(), "local-work")


class DiagnosticsTests(unittest.TestCase):
    def test_runtime_metadata_and_logs_arrive_privately_without_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, target = root / "state", root / "workspace/.local/diagnostics"
            (home / "state").mkdir(parents=True)
            (home / "logs").mkdir()
            (home / "state/hermes-provenance.json").write_text('{"project_revision":"fixture"}')
            (home / "logs/gateway.log").write_text("runtime-log\n")
            (home / ".env").write_text("PRIVATE_TOKEN=not-in-diagnostics")
            (home / "logs/symlink.log").symlink_to(home / ".env")
            payload = collect(home)
            subprocess.run(
                [sys.executable, "-c", RECEIVE_DIAGNOSTICS, str(target)],
                input=payload,
                capture_output=True,
                check=True,
            )
            self.assertEqual((target / "gateway.log").read_text(), "runtime-log\n")
            self.assertEqual(
                json.loads((target / "provenance.json").read_text())["project_revision"],
                "fixture",
            )
            self.assertEqual({p.name for p in target.iterdir()}, {"gateway.log", "provenance.json"})
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            self.assertEqual((target / "gateway.log").stat().st_mode & 0o777, 0o600)
