"""Exercise configuration, isolation, credential transport and offline Git bootstrap."""

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

from hermes_kubernetes.installation.cli import configure, initialize, plan
from hermes_kubernetes.installation.environment import reconcile
from hermes_kubernetes.installation.files import credential, write_private
from hermes_kubernetes.installation.kubernetes import apply as deploy
from hermes_kubernetes.installation.proxmox import configuration
from hermes_kubernetes.installation.render import documents, render
from hermes_kubernetes.installation.settings import BUNDLE, Settings, dotenv
from hermes_kubernetes.installation.terminal import RECEIVE
from hermes_kubernetes.instances.generator import create
from tests.instances.test_distribution import manifest


class InstallationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        if (
            self._testMethodName
            == "test_offline_terminal_preserves_git_history_native_data_and_fast_forwards"
        ):
            count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
            git_config = {
                "GIT_CONFIG_COUNT": str(count + 2),
                f"GIT_CONFIG_KEY_{count}": "maintenance.auto",
                f"GIT_CONFIG_VALUE_{count}": "false",
                f"GIT_CONFIG_KEY_{count + 1}": "gc.auto",
                f"GIT_CONFIG_VALUE_{count + 1}": "0",
            }
            self.git_config_patch = patch.dict(os.environ, git_config)
            self.git_config_patch.start()
            self.addCleanup(self.git_config_patch.stop)
        self.root = Path(self.temp.name).resolve() / "instance"
        create(self.root, manifest("v0.2.0"))

    def settings(self, **overrides):
        values = {
            "TERMINAL_HOST": "192.0.2.9",
            "MODEL": "example-model",
            "MODEL_API_KEY": "example-secret-not-for-logs",
            "KUBERNETES_NAMESPACE": "assistant-one",
        } | overrides
        write_private(
            self.root / ".env",
            "\n".join(key + "=" + json.dumps(value) for key, value in values.items()),
        )
        return Settings(self.root, environment={})

    def test_environment_is_literal_private_and_not_versioned(self):
        s = self.settings(
            ASSISTANT_NAME="My Assistant", MODEL_API_KEY="literal$(touch /tmp/nope)${HOME}"
        )
        self.assertEqual(s["MODEL_API_KEY"], "literal$(touch /tmp/nope)${HOME}")
        self.assertEqual(s["TERMINAL_EGRESS_CIDRS"], "192.0.2.9/32")
        self.assertEqual(credential(self.root / ".env"), (self.root / ".env").read_text())
        self.assertEqual(
            subprocess.run(["git", "-C", str(self.root), "check-ignore", "-q", ".env"]).returncode,
            0,
        )
        self.assertIn(
            ".env.example",
            subprocess.check_output(["git", "-C", str(self.root), "ls-files"], text=True),
        )
        with self.assertRaises(FileExistsError):
            initialize(self.root)
        self.assertEqual(
            (BUNDLE / ".env.example").read_bytes(),
            (BUNDLE / "examples/instance/.env.example").read_bytes(),
        )

    def test_bad_settings_fail_before_commands(self):
        for value in [
            {"TERMINAL_HOST": "terminal.example.net"},
            {"TERMINAL_PORT": "0"},
            {"KUBERNETES_NAMESPACE": "Not_A_Namespace"},
            {"MODEL_BASE_URL": "https://user:secret@example.net"},
            {"BACKUP_EXTRA_PATHS": "../other"},
            {"UNRECOGNIZED_SETTING": "x"},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.settings(**value)
        path = self.root / ".env"
        path.write_text("MODEL=x\nMODEL=y\n")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            dotenv(path)

    def test_plan_redacts_secrets_and_performs_no_mutation(self):
        s = self.settings()
        before = subprocess.check_output(["git", "-C", str(self.root), "status", "--porcelain"])
        result = plan(s)
        self.assertNotIn(s["MODEL_API_KEY"], json.dumps(result))
        self.assertFalse(result["ready"])
        self.assertEqual(
            before, subprocess.check_output(["git", "-C", str(self.root), "status", "--porcelain"])
        )
        self.assertEqual(result["model_calls"], 0)

    def test_configuration_is_idempotent_committed_and_preserves_edits(self):
        s = self.settings()
        configure(s)
        first = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"])
        configure(s)
        self.assertEqual(
            first, subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"])
        )
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(self.root), "status", "--porcelain"]), b""
        )
        self.assertNotIn(s["MODEL_API_KEY"], (self.root / "kustomization.yaml").read_text())
        edited = self.root / "config/installation/SOUL.md"
        edited.write_text("My own persona\n")
        before = (self.root / "instance.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "manual edit"):
            render(self.settings(ASSISTANT_NAME="New Name"))
        self.assertEqual(before, (self.root / "instance.json").read_bytes())

    def test_release_pin_update_keeps_environment_generation_compatible(self):
        s = self.settings()
        configure(s)
        candidate = manifest("v0.3.0", "c" * 40)
        (self.root / "platform.lock.json").write_text(json.dumps(candidate))
        path = self.root / "kustomization.yaml"
        overlay = yaml.safe_load(path.read_text())
        overlay["resources"][0] = overlay["resources"][0].replace("a" * 40, "c" * 40)
        path.write_text(yaml.safe_dump(overlay, sort_keys=False))
        render(s)
        self.assertIn("c" * 40, path.read_text())

    def test_runtime_file_cannot_be_the_installer_environment(self):
        from hermes_kubernetes.installation.kubernetes import runtime_environment

        s = self.settings(RUNTIME_ENV_FILE=".env")
        with self.assertRaisesRegex(ValueError, "must differ"):
            runtime_environment(s)

    def test_rendered_overlay_builds_and_preserves_credential_scope(self):
        s = self.settings(BACKUP_ENABLED="true", INTERNET_EGRESS="external")
        render(s)
        overlay = yaml.safe_load((self.root / "kustomization.yaml").read_text())
        # Use the actual local base, avoiding GitHub/network during regression tests.
        shutil.copytree(BUNDLE / "deploy/kubernetes", self.root / ".local/base")
        overlay["resources"][0] = ".local/base"
        (self.root / "kustomization.yaml").write_text(json.dumps(overlay))
        result = subprocess.check_output(["kubectl", "kustomize", str(self.root)], text=True)
        resources = list(yaml.safe_load_all(result))
        self.assertNotIn("example-secret-not-for-logs", result)
        namespace = next(x for x in resources if x["kind"] == "Namespace")
        self.assertEqual(namespace["metadata"]["name"], "assistant-one")
        deployment = next(x for x in resources if x["kind"] == "Deployment")
        spec = deployment["spec"]["template"]["spec"]
        policy = next(x for x in spec["volumes"] if x["name"] == "policy")
        self.assertEqual(
            policy["projected"]["sources"][1]["secret"]["items"][0]["path"], "initial.env"
        )
        containers = {x["name"]: x for x in spec["containers"]}
        self.assertIn("backup", containers)
        self.assertIn("envFrom", containers["dashboard"])
        self.assertNotIn("envFrom", containers["gateway"])
        network = next(x for x in resources if x["metadata"]["name"] == "hermes-configured-egress")
        self.assertIn("192.0.2.9/32", json.dumps(network))
        self.assertIn("10.0.0.0/8", json.dumps(network))
        self.assertIn("cli-proxy-api", json.dumps(network))

    def test_secrets_are_sent_via_stdin_and_never_in_command_arguments(self):
        key, hosts = self.root / ".local/key", self.root / ".local/known_hosts"
        write_private(key, "private-key-example")
        write_private(hosts, "hermes-terminal ssh-ed25519 public-example\n")
        s = self.settings(TERMINAL_SSH_KEY_FILE=str(key), TERMINAL_KNOWN_HOSTS_FILE=str(hosts))
        render(s)
        overlay = yaml.safe_load((self.root / "kustomization.yaml").read_text())
        shutil.copytree(BUNDLE / "deploy/kubernetes", self.root / ".local/base")
        overlay["resources"][0] = ".local/base"
        (self.root / "kustomization.yaml").write_text(json.dumps(overlay))
        built = subprocess.check_output(["kubectl", "kustomize", str(self.root)], text=True)
        calls = []

        def fake(argv, **kwargs):
            calls.append((argv, kwargs.get("input")))
            return built if "kustomize" in argv else ""

        with patch("hermes_kubernetes.installation.kubernetes.run", side_effect=fake):
            report = deploy(s)
        self.assertNotIn(s["MODEL_API_KEY"], json.dumps(report))
        self.assertNotIn(s["MODEL_API_KEY"], json.dumps([c[0] for c in calls]))
        secrets = [json.loads(data) for argv, data in calls if "--server-side" in argv]
        self.assertEqual(
            {x["metadata"]["name"] for x in secrets},
            {"hermes-runtime-env", "hermes-ssh", "hermes-dashboard"},
        )
        self.assertFalse(list(self.root.rglob("secrets.yaml")))

    def managed_settings(self, kind):
        return self.settings(
            TERMINAL_KIND=kind,
            PROXMOX_ENDPOINT="https://proxmox.example.net:8006/",
            PROXMOX_NODE="node-one",
            PROXMOX_GUEST_ID="555",
            PROXMOX_VM_TEMPLATE_ID="9000",
            PROXMOX_ROOT_STORAGE="guest-disks",
            PROXMOX_TEMPLATE_STORAGE="templates",
            PROXMOX_ROOT_PASSWORD_FILE=".local/root-password",
            PROXMOX_SSH_HOST="node-one.example.net",
            PROXMOX_SSH_KEY_FILE=".local/proxmox-key",
            PROXMOX_KNOWN_HOSTS_FILE=".local/proxmox-hosts",
            TERMINAL_IPV4_CIDR="192.0.2.9/24",
            TERMINAL_GATEWAY="192.0.2.1",
            WORKSPACE_CEPH_MONITORS="192.0.2.20:6789",
            WORKSPACE_CEPH_FILESYSTEM="cephfs",
            WORKSPACE_CEPH_SUBVOLUME="/volumes/assistant",
            WORKSPACE_CEPH_CLIENT="assistant",
            WORKSPACE_CEPH_KEY_FILE=".local/ceph-key",
        )

    def test_provisioning_keeps_state_separate_pins_provider_and_vm_cpu_v2(self):
        for kind in ("vm", "lxc"):
            s = self.managed_settings(kind)
            config = configuration(s, "ssh-ed25519 example")
            self.assertEqual(
                config["terraform"]["required_providers"]["proxmox"]["version"], "0.115.0"
            )
            self.assertNotIn("password-example", json.dumps(config))
            name = (
                "proxmox_virtual_environment_vm"
                if kind == "vm"
                else "proxmox_virtual_environment_container"
            )
            guest = config["resource"][name]["terminal"]
            self.assertTrue(guest["lifecycle"]["prevent_destroy"])
            if kind == "vm":
                self.assertEqual(guest["cpu"]["type"], "x86-64-v2")
            else:
                self.assertTrue(guest["unprivileged"])
                self.assertTrue(guest["features"]["fuse"])
                self.assertEqual(
                    len(config["resource"]["proxmox_download_file"]["ubuntu"]["checksum"]), 128
                )

    def test_native_env_is_independent_and_gui_edits_are_preserved(self):
        home, policy = self.root / ".local/home", self.root / ".local/policy"
        write_private(policy / "initial.env", 'CLI_PROXY_API_KEY="first"\n')
        scope = types.ModuleType("agent.secret_scope")
        scope.load_env_file = lambda path: dotenv(path) if Path(path).exists() else {}
        agent = types.ModuleType("agent")
        with patch.dict(sys.modules, {"agent": agent, "agent.secret_scope": scope}):
            reconcile(home, policy)
            profile = home / "profiles/other/.env"
            write_private(profile, (home / ".env").read_text())
            write_private(home / ".env", 'CLI_PROXY_API_KEY="gui-edit"\n')
            reconcile(home, policy)
            self.assertEqual(dotenv(home / ".env")["CLI_PROXY_API_KEY"], "gui-edit")
            write_private(policy / "initial.env", 'CLI_PROXY_API_KEY="new-installer-value"\n')
            with self.assertRaisesRegex(ValueError, "conflict"):
                reconcile(home, policy)
            self.assertEqual(dotenv(profile)["CLI_PROXY_API_KEY"], "first")
            self.assertNotEqual((home / ".env").stat().st_ino, profile.stat().st_ino)

    def test_offline_terminal_preserves_git_history_native_data_and_fast_forwards(self):
        target = self.root / ".local/terminal-home"
        bundle = self.root / ".local/initial.bundle"
        bundle.parent.mkdir(exist_ok=True)
        subprocess.run(
            ["git", "-C", str(self.root), "bundle", "create", str(bundle), "main"],
            check=True,
            capture_output=True,
        )

        def receive():
            with bundle.open("rb") as stream:
                # Disable detached Git maintenance during this fixture.
                return subprocess.run(
                    [sys.executable, "-c", RECEIVE, str(target)],
                    stdin=stream,
                    capture_output=True,
                )

        self.assertEqual(receive().returncode, 0)
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"]),
            subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"]),
        )
        write_private(target / ".env", "MY_NATIVE_SECRET=preserved\n")
        config = self.root / "instance.json"
        value = json.loads(config.read_text())
        value["name"] = "Changed"
        config.write_text(json.dumps(value))
        subprocess.run(["git", "-C", str(self.root), "add", "instance.json"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "Update",
            ],
            check=True,
        )
        bundle.unlink()
        subprocess.run(
            ["git", "-C", str(self.root), "bundle", "create", str(bundle), "main"],
            check=True,
            capture_output=True,
        )
        self.assertEqual(receive().returncode, 0)
        self.assertEqual(dotenv(target / ".env")["MY_NATIVE_SECRET"], "preserved")
        self.assertEqual(json.loads((target / "instance.json").read_text())["name"], "Changed")
        (target / "instance.json").write_text("local manual change")
        self.assertNotEqual(receive().returncode, 0)


if __name__ == "__main__":
    unittest.main()
