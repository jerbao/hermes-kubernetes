"""Provision a dedicated Proxmox guest with an isolated, non-destructive OpenTofu state."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import time

from hermes_kubernetes.installation.files import (
    credential,
    keypair,
    private_directory,
    run,
    write_private,
)


def configuration(s, public_key):
    common = {
        "node_name": s["PROXMOX_NODE"],
        "vm_id": int(s["PROXMOX_GUEST_ID"]),
        "description": "Hermes terminal; managed by hermes-kubernetes; workspace="
        + s["WORKSPACE_CEPH_SUBVOLUME"],
        "started": True,
        "tags": ["hermes-terminal", "opentofu"],
        "delete_unreferenced_disks_on_destroy": False,
        "lifecycle": {"prevent_destroy": True},
    }
    ip = {"ipv4": {"address": s["TERMINAL_IPV4_CIDR"], "gateway": s["TERMINAL_GATEWAY"]}}
    network = {"bridge": s["PROXMOX_BRIDGE"]}
    if s["PROXMOX_VLAN"]:
        network["vlan_id"] = int(s["PROXMOX_VLAN"])
    disk = {"datastore_id": s["PROXMOX_ROOT_STORAGE"], "size": int(s["TERMINAL_DISK_GIB"])}
    root_password = bool(s["PROXMOX_ROOT_PASSWORD_FILE"])
    provider = {"endpoint": s["PROXMOX_ENDPOINT"], "insecure": not s.flag("PROXMOX_TLS_VERIFY")}
    provider.update(
        {"username": "root@pam", "password": "${var.proxmox_credential}"}
        if root_password
        else {"api_token": "${var.proxmox_credential}"}
    )
    resources = {}
    if s["TERMINAL_KIND"] == "lxc":
        template = "ubuntu-26.04-standard_26.04-1_amd64.tar.zst"
        resources["proxmox_download_file"] = {
            "ubuntu": {
                "node_name": s["PROXMOX_NODE"],
                "datastore_id": s["PROXMOX_TEMPLATE_STORAGE"],
                "content_type": "vztmpl",
                "file_name": template,
                "url": "http://download.proxmox.com/images/system/" + template,
                "checksum_algorithm": "sha512",
                "checksum": "d5607f124d01f8ddfebd8e7da34c0022fb2a464e1662ee1e39a13f5ed1bae08a7364c486247fbd9f69f26c710729884ee5047aadb89f33a40066cd5f0d9def88",
                "overwrite": False,
                "overwrite_unmanaged": False,
            }
        }
        resources["proxmox_virtual_environment_container"] = {
            "terminal": common
            | {
                "unprivileged": True,
                "start_on_boot": True,
                "features": {"nesting": True, "keyctl": True, "fuse": True},
                "cpu": {"architecture": "amd64", "cores": int(s["TERMINAL_CORES"])},
                "memory": {"dedicated": int(s["TERMINAL_MEMORY_MIB"]), "swap": 0},
                "disk": disk,
                "network_interface": [network | {"name": "eth0"}],
                "initialization": {
                    "hostname": s["KUBERNETES_NAMESPACE"] + "-terminal",
                    "ip_config": ip,
                    "user_account": {"keys": [public_key]},
                },
                "operating_system": {
                    "type": "ubuntu",
                    "template_file_id": "${proxmox_download_file.ubuntu.id}",
                },
            }
        }
    else:
        resources["proxmox_virtual_environment_vm"] = {
            "terminal": common
            | {
                "name": s["KUBERNETES_NAMESPACE"] + "-terminal",
                "on_boot": True,
                "stop_on_destroy": True,
                "clone": {"vm_id": int(s["PROXMOX_VM_TEMPLATE_ID"]), "full": True},
                "agent": {"enabled": True},
                "cpu": {"type": "x86-64-v2", "cores": int(s["TERMINAL_CORES"])},
                "memory": {"dedicated": int(s["TERMINAL_MEMORY_MIB"])},
                "disk": disk | {"interface": "scsi0"},
                "network_device": [network],
                "initialization": {
                    "datastore_id": s["PROXMOX_ROOT_STORAGE"],
                    "upgrade": False,
                    "ip_config": ip,
                    "user_account": {"username": "hermes-admin", "keys": [public_key]},
                },
            }
        }
    return {
        "terraform": {
            "required_version": ">= 1.6.0",
            "required_providers": {"proxmox": {"source": "bpg/proxmox", "version": "0.115.0"}},
            "backend": {"local": {}},
        },
        "variable": {"proxmox_credential": {"type": "string", "sensitive": True}},
        "provider": {"proxmox": provider},
        "resource": resources,
    }


def hcl(config):
    """Emit HCL blocks, preserving the provider's legacy nested-block defaults."""
    blocks = {
        "lifecycle",
        "features",
        "cpu",
        "memory",
        "disk",
        "network_interface",
        "network_device",
        "initialization",
        "ip_config",
        "ipv4",
        "user_account",
        "operating_system",
        "clone",
        "agent",
    }

    def body(value, depth=1):
        lines = []
        prefix = "  " * depth
        for key, item in value.items():
            if key in blocks:
                for block in item if isinstance(item, list) else [item]:
                    lines.append(prefix + key + " {\n" + body(block, depth + 1) + prefix + "}\n")
            else:
                lines.append(prefix + key + " = " + json.dumps(item) + "\n")
        return "".join(lines)

    output = 'terraform {\n  required_version = ">= 1.6.0"\n  required_providers {\n    proxmox = { source = "bpg/proxmox", version = "0.115.0" }\n  }\n  backend "local" {}\n}\n'
    output += 'variable "proxmox_credential" {\n  type = string\n  sensitive = true\n}\n'
    output += 'provider "proxmox" {\n' + body(config["provider"]["proxmox"]) + "}\n"
    for kind, entries in config["resource"].items():
        for name, value in entries.items():
            output += (
                "resource "
                + json.dumps(kind)
                + " "
                + json.dumps(name)
                + " {\n"
                + body(value)
                + "}\n"
            )
    return output


def environment(s):
    key = (
        "PROXMOX_ROOT_PASSWORD_FILE"
        if s["PROXMOX_ROOT_PASSWORD_FILE"]
        else "PROXMOX_API_TOKEN_FILE"
    )
    return dict(
        os.environ, TF_VAR_proxmox_credential=credential(s.file(key)).strip(), TF_IN_AUTOMATION="1"
    )


def plan(s):
    from hermes_kubernetes.installation.settings import BUNDLE

    directory = private_directory(s.root / ".local/installation/proxmox")
    shutil.copyfile(
        BUNDLE / "dependencies/proxmox/.terraform.lock.hcl", directory / ".terraform.lock.hcl"
    )
    public = keypair(s.root / ".local/credentials/admin_ed25519")
    config = hcl(configuration(s, public))
    write_private(directory / "main.tf", config)
    env = environment(s)
    command = ["tofu", "-chdir=" + str(directory)]
    run(command + ["init", "-input=false", "-no-color", "-lockfile=readonly"], env=env)
    run(command + ["validate", "-no-color"], env=env)
    run(command + ["plan", "-input=false", "-no-color", "-out=install.plan"], env=env)
    plan_path = directory / "install.plan"
    plan_path.chmod(0o600)
    report = json.loads(run(command + ["show", "-json", "install.plan"], env=env))
    changes = report.get("resource_changes", [])
    if any("delete" in change["change"]["actions"] for change in changes):
        raise ValueError(
            "The installer refuses guest deletion or replacement; persistent storage is never managed here"
        )
    stamp = {
        "configuration": hashlib.sha256(config.encode()).hexdigest(),
        "plan": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "resources": [
            {"address": x["address"], "actions": x["change"]["actions"]} for x in changes
        ],
    }
    write_private(directory / "approved-input.json", json.dumps(stamp))
    return stamp["resources"]


def apply(s):
    directory = s.root / ".local/installation/proxmox"
    stamp = json.loads((directory / "approved-input.json").read_text())
    expected = hcl(configuration(s, keypair(s.root / ".local/credentials/admin_ed25519")))
    if (
        hashlib.sha256(expected.encode()).hexdigest() != stamp["configuration"]
        or hashlib.sha256((directory / "install.plan").read_bytes()).hexdigest() != stamp["plan"]
    ):
        raise ValueError("Installation settings or saved plan changed; run setup plan again")
    run(
        ["tofu", "-chdir=" + str(directory), "apply", "-input=false", "-no-color", "install.plan"],
        env=environment(s),
    )
    trust_guest(s)


def trust_guest(s):
    """Read the initial guest host key through the authenticated Proxmox node."""
    from hermes_kubernetes.installation.terminal import ssh_options

    command = (
        ["pct", "exec", s["PROXMOX_GUEST_ID"], "--", "cat", "/etc/ssh/ssh_host_ed25519_key.pub"]
        if s["TERMINAL_KIND"] == "lxc"
        else [
            "qm",
            "guest",
            "exec",
            s["PROXMOX_GUEST_ID"],
            "--",
            "cat",
            "/etc/ssh/ssh_host_ed25519_key.pub",
        ]
    )
    for attempt in range(36):
        try:
            output = run(
                ssh_options(s.file("PROXMOX_SSH_KEY_FILE"), s.file("PROXMOX_KNOWN_HOSTS_FILE"))
                + [s["PROXMOX_SSH_USER"] + "@" + s["PROXMOX_SSH_HOST"], shlex.join(command)]
            )
            if s["TERMINAL_KIND"] == "vm":
                result = json.loads(output)
                if result.get("exitcode") != 0:
                    raise ValueError("Guest agent is not ready")
                output = result["out-data"]
            if not output.startswith("ssh-ed25519 "):
                raise ValueError("Guest did not return an Ed25519 host key")
            host = (
                s["TERMINAL_HOST"]
                if s["TERMINAL_PORT"] == "22"
                else f"[{s['TERMINAL_HOST']}]:{s['TERMINAL_PORT']}"
            )
            private = s.root / ".local/credentials"
            preserved = keypair(private / "host_ed25519")
            write_private(
                private / "bootstrap_known_hosts", f"{host} {output.strip()}\n{host} {preserved}\n"
            )
            write_private(
                private / "known_hosts", f"hermes-terminal {preserved}\n{host} {preserved}\n"
            )
            return
        except (RuntimeError, ValueError, KeyError):
            if attempt == 35:
                raise RuntimeError(
                    "Could not authenticate the new guest host identity through Proxmox"
                ) from None
            time.sleep(5)


def configure_terminal(s):
    from hermes_kubernetes.installation.settings import BUNDLE

    private = private_directory(s.root / ".local/installation")
    keys = s.root / ".local/credentials"
    runtime_key = keypair(keys / "runtime_ed25519")
    inventory = {
        "all": {
            "hosts": {
                "terminal": {
                    "ansible_host": s["TERMINAL_HOST"],
                    "ansible_port": int(s["TERMINAL_PORT"]),
                    "ansible_user": "root" if s["TERMINAL_KIND"] == "lxc" else "hermes-admin",
                    "ansible_ssh_private_key_file": str(keys / "admin_ed25519"),
                    "ansible_ssh_common_args": shlex.join(
                        [
                            "-o",
                            "StrictHostKeyChecking=yes",
                            "-o",
                            "UserKnownHostsFile=" + str(keys / "bootstrap_known_hosts"),
                        ]
                    ),
                    "terminal_user": s["TERMINAL_USER"],
                    "terminal_uid": int(s["TERMINAL_UID"]),
                    "terminal_host_key": str(keys / "host_ed25519"),
                    "terminal_public_key": runtime_key,
                    "ceph_monitors": s["WORKSPACE_CEPH_MONITORS"],
                    "ceph_filesystem": s["WORKSPACE_CEPH_FILESYSTEM"],
                    "ceph_subvolume": s["WORKSPACE_CEPH_SUBVOLUME"],
                    "ceph_client": s["WORKSPACE_CEPH_CLIENT"],
                    "ceph_key_file": str(s.file("WORKSPACE_CEPH_KEY_FILE")),
                }
            }
        }
    }
    write_private(private / "inventory.json", json.dumps(inventory))
    playbook = private / "terminal.yml"
    shutil.copyfile(BUNDLE / "deploy/ansible/terminal.yml", playbook)
    run(
        ["ansible-playbook", "-i", str(private / "inventory.json"), str(playbook)],
        env=dict(os.environ, ANSIBLE_HOST_KEY_CHECKING="True"),
    )
