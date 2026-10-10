"""Configure, plan and install an independent Hermes instance from a private .env."""

import argparse
import json
from pathlib import Path
import shutil

from hermes_kubernetes.installation.files import credential, run
from hermes_kubernetes.installation.settings import BUNDLE, Settings


def initialize(root):
    root = Path(root).resolve()
    if not (root / "platform.lock.json").is_file():
        raise ValueError("Generate a private instance first")
    path = root / ".env"
    if path.exists() or path.is_symlink():
        raise FileExistsError("Keep the existing installer .env; it will not be replaced")
    from hermes_kubernetes.installation.files import write_private

    write_private(path, (BUNDLE / "examples/instance/.env.example").read_text())
    return {"created": ".env", "next": "Fill in .env, then run: python3 hermes.py setup plan"}


def preflight(s):
    from hermes_kubernetes.installation.kubernetes import drive_configuration, runtime_environment

    tools = ["git", "ssh", "ssh-keygen", "kubectl"]
    if s["TERMINAL_KIND"] != "existing":
        tools += ["tofu", "ansible-playbook"]
        for key in ("PROXMOX_SSH_KEY_FILE", "WORKSPACE_CEPH_KEY_FILE"):
            credential(s.file(key))
        if not s.file("PROXMOX_KNOWN_HOSTS_FILE").is_file():
            raise ValueError("Pin the Proxmox node SSH host key first")
        from hermes_kubernetes.installation.proxmox import environment

        environment(s)
    else:
        credential(s.file("TERMINAL_SSH_KEY_FILE"))
        if not s.file("TERMINAL_KNOWN_HOSTS_FILE").is_file():
            raise ValueError("Pin the terminal SSH host key first")
    if s["INSTANCE_REPOSITORY"]:
        credential(s.file("GIT_SSH_KEY_FILE"))
        if not s.file("GIT_KNOWN_HOSTS_FILE").is_file():
            raise ValueError("GIT_KNOWN_HOSTS_FILE is required")
    missing = [tool for tool in tools if not shutil.which(tool)]
    if missing:
        raise ValueError("Install required tools: " + ", ".join(missing))
    runtime_environment(s)
    if s.flag("BACKUP_ENABLED"):
        if s["INTERNET_EGRESS"] != "external" and not s["API_EGRESS_CIDRS"]:
            raise ValueError(
                "Drive backups require explicit HTTPS egress; set INTERNET_EGRESS=external or API_EGRESS_CIDRS"
            )
        drive_configuration(s)


def configure(s):
    from hermes_kubernetes.installation.render import render

    if run(["git", "-C", str(s.root), "diff", "--cached", "--name-only"]).strip():
        raise ValueError("Commit or unstage existing staged edits before configuring")
    names = render(s)
    run(["git", "-C", str(s.root), "add", "--", *names])
    if run(["git", "-C", str(s.root), "diff", "--cached", "--name-only"]).strip():
        run(
            [
                "git",
                "-C",
                str(s.root),
                "-c",
                "user.name=Hermes installer",
                "-c",
                "user.email=installer@example.invalid",
                "commit",
                "-qm",
                "Configure private Hermes installation",
            ]
        )
    return {"configured_files": names, "credentials_committed": False}


def plan(s, infra=False):
    from hermes_kubernetes.installation.render import documents

    manifest = json.loads((s.root / "platform.lock.json").read_text())
    documents(s, manifest)
    result = {
        "namespace": s["KUBERNETES_NAMESPACE"],
        "assistant": s["ASSISTANT_NAME"],
        "terminal": {
            "kind": s["TERMINAL_KIND"],
            "host": s["TERMINAL_HOST"],
            "workspace": s["TERMINAL_WORKSPACE"],
            "cpu": "x86-64-v2" if s["TERMINAL_KIND"] == "vm" else "host",
        },
        "encrypted_drive_backups": s.flag("BACKUP_ENABLED"),
        "internet_egress": s["INTERNET_EGRESS"],
        "changes": [
            "Generate and commit non-secret instance manifests",
            *(
                [
                    "Create/update a dedicated Proxmox guest using OpenTofu",
                    "Mount durable CephFS home and configure guest using Ansible",
                ]
                if s["TERMINAL_KIND"] != "existing"
                else []
            ),
            "Install the private Git project on the terminal",
            "Apply Kubernetes Secrets and manifests",
        ],
        "model_calls": 0,
    }
    try:
        preflight(s)
        result["ready"] = True
    except (OSError, ValueError) as error:
        result["ready"] = False
        result["required_action"] = str(error)
    if infra:
        if not result["ready"] or s["TERMINAL_KIND"] == "existing":
            raise ValueError(
                "Complete prerequisites and select a managed terminal before setup plan --infra"
            )
        from hermes_kubernetes.installation.proxmox import plan as infrastructure_plan

        result["infrastructure"] = infrastructure_plan(s)
    return result


def install(s):
    from hermes_kubernetes.installation import kubernetes, terminal

    preflight(s)
    version = json.loads((s.root / "platform.lock.json").read_text())["version"]
    if tuple(map(int, version[1:].split("."))) < (0, 2, 0):
        raise ValueError(
            "The installer requires distribution v0.2.0 or newer; generate or update the instance to that release"
        )
    configure(s)
    if s["INSTANCE_REPOSITORY"]:
        # Ensure the new generated commit is available before enabling its sync timer.
        origin = run(["git", "-C", str(s.root), "remote", "get-url", "origin"]).strip()
        if origin != s["INSTANCE_REPOSITORY"]:
            raise ValueError("The local origin must match INSTANCE_REPOSITORY")
        run(["git", "-C", str(s.root), "push", "origin", "main"])
    if s["TERMINAL_KIND"] != "existing":
        from hermes_kubernetes.installation import proxmox

        proxmox.plan(s)
        proxmox.apply(s)
        proxmox.configure_terminal(s)
    terminal.install(s)
    return {"status": "installed", **kubernetes.apply(s), "model_calls": 0}


def drive_auth(s):
    from hermes_kubernetes.backup.oauth import configure as authorize
    from hermes_kubernetes.backup.tools import install as install_rclone

    s.require("DRIVE_OAUTH_CLIENT_FILE")
    binary = s.root / ".local/installation/bin/rclone"
    if not binary.exists():
        install_rclone(BUNDLE / "config/rclone.lock.json", binary)
    authorize(
        s.file("DRIVE_RCLONE_CONFIG_FILE"),
        oauth_client=s.file("DRIVE_OAUTH_CLIENT_FILE"),
        rclone=str(binary),
    )
    return {
        "authorized": True,
        "recovery_configuration": str(s.file("DRIVE_RCLONE_CONFIG_FILE")),
        "next": "Save an independent copy of this encryption recovery configuration; set BACKUP_ENABLED=true",
    }


def dispatch(root, action, *, env_file=None, infra=False):
    if action == "init":
        return initialize(root)
    s = Settings(root, env_file)
    if action == "plan":
        return plan(s, infra)
    if action == "configure":
        return configure(s)
    if action == "install":
        return install(s)
    if action == "drive-auth":
        return drive_auth(s)
    if action == "connect":
        from hermes_kubernetes.installation.kubernetes import command
        import subprocess

        return subprocess.call(
            command(s)
            + [
                "-n",
                s["KUBERNETES_NAMESPACE"],
                "port-forward",
                "--address=127.0.0.1",
                "service/hermes-dashboard",
                "9119:9119",
            ]
        )
    raise ValueError("Unknown setup action")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "action", choices=["init", "plan", "configure", "install", "drive-auth", "connect"]
    )
    parser.add_argument(
        "--infra", action="store_true", help="Create a private OpenTofu plan without applying it"
    )
    args = parser.parse_args()
    result = dispatch(args.root, args.action, env_file=args.env_file, infra=args.infra)
    if isinstance(result, int):
        return result
    print(json.dumps(result, indent=2))
    return 0 if result.get("ready", True) else 2
