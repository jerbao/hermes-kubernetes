"""Desktop SSH settings survive bootstrap without inheriting another profile's env."""

import sys
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "runtime"),
    str(Path(os.environ.get("HERMES_SOURCE_DIR", ROOT / "hermes-agent"))),
]
from hermes_kubernetes.runtime.configuration import ssh_workspace_configuration
from hermes_kubernetes.runtime.bootstrap import prepare_configuration
import hermes_yaml as yaml


class DesktopWorkspaceConfigurationTests(unittest.TestCase):
    def test_bootstrap_persists_missing_policy_and_preserves_profile_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profiles/other"
            profile.mkdir(parents=True)
            (root / ".env").write_text("TERMINAL_SSH_USER=do-not-inherit\n")
            (profile / ".env").write_text("TERMINAL_SSH_USER=profile-user\n")
            (profile / "config.yaml").write_text(
                "# retain\nterminal:\n  backend: ssh\n  cwd: ~/project\n  ssh_host: profile-host\n"
            )
            instance = {"ssh_user": "instance-user", "terminal_port": 2222}
            before = (profile / ".env").read_bytes()
            prepare_configuration(profile, root, instance=instance)
            config = yaml.safe_load((profile / "config.yaml").read_text())
            self.assertEqual(
                config["terminal"],
                {
                    "backend": "ssh",
                    "cwd": "~/project",
                    "ssh_host": "profile-host",
                    "ssh_user": "profile-user",
                    "ssh_port": 2222,
                    "ssh_key": str(root / ".ssh/id_ed25519"),
                },
            )
            self.assertIn("# retain", (profile / "config.yaml").read_text())
            self.assertEqual((profile / ".env").read_bytes(), before)
            saved = (profile / "config.yaml").read_bytes()
            prepare_configuration(profile, root, instance=instance)
            self.assertEqual((profile / "config.yaml").read_bytes(), saved)

    def test_default_only_and_explicit_local_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = {"terminal": {}}
            result = ssh_workspace_configuration(config, home, home, {"ssh_user": "fixture"})
            self.assertEqual(result["terminal"]["ssh_host"], "hermes-terminal")
            self.assertEqual(result["terminal"]["ssh_user"], "fixture")
            self.assertEqual(config, {"terminal": {}})
            local = {"terminal": {"backend": "local"}}
            self.assertEqual(ssh_workspace_configuration(local, home, home, {}), local)


if __name__ == "__main__":
    unittest.main()
