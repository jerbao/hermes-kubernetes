"""Validate generated OpenTofu provider schemas and Ansible syntax without provisioning."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from hermes_kubernetes.installation.proxmox import configuration, hcl
from hermes_kubernetes.installation.settings import BUNDLE
from tests.installation.test_setup import InstallationTest


def main():
    fixture = InstallationTest()
    fixture.setUp()
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-iac-") as temporary:
            directory = Path(temporary).resolve()
            shutil.copyfile(
                BUNDLE / "dependencies/proxmox/.terraform.lock.hcl",
                directory / ".terraform.lock.hcl",
            )
            os.chmod(directory, 0o700)
            for kind in ("vm", "lxc"):
                settings = fixture.managed_settings(kind)
                (directory / "main.tf").write_text(
                    hcl(configuration(settings, "ssh-ed25519 example"))
                )
                for action in (
                    ["init", "-backend=false", "-input=false", "-lockfile=readonly"],
                    ["validate", "-no-color"],
                ):
                    result = subprocess.run(
                        ["tofu", "-chdir=" + str(directory), *action],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode:
                        raise RuntimeError(
                            result.stdout + result.stderr
                        )  # Synthetic fixture only; no credentials.
            inventory = directory / "inventory.json"
            inventory.write_text(
                json.dumps({"all": {"hosts": {"terminal": {"ansible_host": "192.0.2.9"}}}})
            )
            subprocess.run(
                [
                    "ansible-playbook",
                    "--syntax-check",
                    "-i",
                    str(inventory),
                    str(BUNDLE / "deploy/ansible/terminal.yml"),
                ],
                check=True,
            )
        print(
            json.dumps(
                {
                    "opentofu_schemas": ["vm", "lxc"],
                    "ansible_syntax": "valid",
                    "infrastructure_changes": 0,
                    "model_calls": 0,
                }
            )
        )
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
