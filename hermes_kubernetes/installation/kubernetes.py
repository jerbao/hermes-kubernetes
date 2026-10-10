"""Apply non-secret desired state and stream credentials directly to the API server."""

import base64
import configparser
import hashlib
import json
from pathlib import Path
import secrets
import yaml

from hermes_kubernetes.installation.files import credential, run, write_private
from hermes_kubernetes.installation.terminal import runtime_identity


def command(s):
    return ["kubectl"] + (["--context", s["KUBERNETES_CONTEXT"]] if s["KUBERNETES_CONTEXT"] else [])


def secret(s, name, data):
    resource = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "namespace": s["KUBERNETES_NAMESPACE"]},
        "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()},
    }
    # Server-side apply avoids kubectl's last-applied annotation duplicating credential values.
    run(
        command(s) + ["apply", "--server-side", "--field-manager=hermes-installer", "-f", "-"],
        input=json.dumps(resource),
    )
    return hashlib.sha256(json.dumps(resource["data"], sort_keys=True).encode()).hexdigest()


def runtime_environment(s):
    values = {}
    if s["RUNTIME_ENV_FILE"]:
        from hermes_kubernetes.installation.settings import dotenv

        if s.file("RUNTIME_ENV_FILE").samefile(s.source):
            raise ValueError("RUNTIME_ENV_FILE must differ from the installer environment")
        credential(s.file("RUNTIME_ENV_FILE"))
        values = dotenv(s.file("RUNTIME_ENV_FILE"))
    if s["MODEL_API_KEY"]:
        values["CLI_PROXY_API_KEY"] = s["MODEL_API_KEY"]
    if not values.get("CLI_PROXY_API_KEY"):
        raise ValueError("MODEL_API_KEY or CLI_PROXY_API_KEY in RUNTIME_ENV_FILE is required")
    # Native dotenv readers support JSON double-quoted strings without interpolation here.
    return "".join(key + "=" + json.dumps(value) + "\n" for key, value in values.items())


def dashboard_token(s):
    if s["DASHBOARD_TOKEN"]:
        return s["DASHBOARD_TOKEN"]
    path = s.root / ".local/credentials/dashboard-token"
    if not path.exists():
        write_private(path, secrets.token_urlsafe(48))
    return credential(path).strip()


def drive_configuration(s):
    path = s.file("DRIVE_RCLONE_CONFIG_FILE")
    text = credential(path)
    value = configparser.RawConfigParser()
    value.read_string(text)
    if (
        value.get("hermes-backup", "type", fallback="") != "crypt"
        or not value.get("hermes-backup", "password", fallback="")
        or not value.get("hermes-drive", "token", fallback="")
    ):
        raise ValueError("Authorize an encrypted Drive remote with setup drive-auth first")
    return text


def apply(s):
    # Read every credential before mutating the cluster.
    environment = runtime_environment(s)
    key, known_hosts = runtime_identity(s)
    ssh_key, trusted_hosts = credential(key), Path(known_hosts).read_text()
    if "hermes-terminal " not in trusted_hosts:
        # Match the configured actual host using OpenSSH's own known-hosts lookup.
        host = (
            s["TERMINAL_HOST"]
            if s["TERMINAL_PORT"] == "22"
            else f"[{s['TERMINAL_HOST']}]:{s['TERMINAL_PORT']}"
        )
        found = run(["ssh-keygen", "-F", host, "-f", str(known_hosts)])
        entries = [line.split() for line in found.splitlines() if line and not line.startswith("#")]
        if not entries:
            raise ValueError("known_hosts does not pin the terminal host")
        trusted_hosts += "".join(
            "hermes-terminal " + " ".join(entry[1:]) + "\n" for entry in entries
        )
    token = dashboard_token(s)
    drive = drive_configuration(s) if s.flag("BACKUP_ENABLED") else None
    render = run(command(s) + ["kustomize", str(s.root)])
    resources = list(yaml.safe_load_all(render))
    # Ensure namespace exists before Secrets. Do not deploy workload before its credentials.
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": s["KUBERNETES_NAMESPACE"]},
    }
    run(command(s) + ["apply", "-f", "-"], input=json.dumps(namespace))
    hashes = [
        secret(s, "hermes-runtime-env", {".env": environment}),
        secret(s, "hermes-ssh", {"id_ed25519": ssh_key, "known_hosts": trusted_hosts}),
        secret(s, "hermes-dashboard", {"HERMES_DASHBOARD_SESSION_TOKEN": token}),
    ]
    if drive is not None:
        hashes.append(secret(s, "hermes-drive", {"rclone.conf": drive}))
    # Secret changes roll all consumers once, including envFrom's session token.
    generation = hashlib.sha256("".join(hashes).encode()).hexdigest()
    for resource in resources:
        if (
            resource
            and resource["kind"] == "Deployment"
            and resource["metadata"]["name"] == "hermes"
        ):
            resource["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
                "hermes-kubernetes/credentials-generation"
            ] = generation
    run(
        command(s) + ["apply", "-f", "-"],
        input="\n---\n".join(json.dumps(item) for item in resources if item),
    )
    run(
        command(s)
        + [
            "-n",
            s["KUBERNETES_NAMESPACE"],
            "rollout",
            "status",
            "deployment/hermes",
            "--timeout=600s",
        ]
    )
    return {
        "namespace": s["KUBERNETES_NAMESPACE"],
        "service": "hermes-dashboard:9119",
        "dashboard_token_file": str(s.root / ".local/credentials/dashboard-token")
        if not s["DASHBOARD_TOKEN"]
        else "DASHBOARD_TOKEN in installer environment",
    }
