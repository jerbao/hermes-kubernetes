"""Parse declarative installer settings without shell evaluation or secret logging."""

import ipaddress
import os
from pathlib import Path
import re
import shlex
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

BUNDLE = Path(__file__).resolve().parents[2]


def dotenv(path):
    values = {}
    for number, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"Invalid environment assignment on line {number}")
        if key in values:
            raise ValueError(f"Duplicate setting: {key}")
        lexer = shlex.shlex(value, posix=True)
        lexer.whitespace_split, lexer.commenters = True, "#"
        tokens = list(lexer)
        if len(tokens) > 1:
            raise ValueError(f"Quote values containing spaces: {key}")
        values[key] = tokens[0] if tokens else ""
    return values


class Settings(dict):
    def __init__(self, root, path=None, environment=None):
        self.root = Path(root).resolve()
        defaults = dotenv(BUNDLE / "examples/instance/.env.example")
        from hermes_kubernetes.installation.files import credential

        selected = Path(path) if path else self.root / ".env"
        if not selected.is_absolute():
            selected = self.root / selected
        self.source = selected
        credential(selected)
        supplied = dotenv(selected)
        if set(supplied) - set(defaults):
            raise ValueError(
                "Unknown installer settings: " + ", ".join(sorted(set(supplied) - set(defaults)))
            )
        super().__init__(defaults | supplied)
        env = os.environ if environment is None else environment
        self.update({key: env[key] for key in defaults if key in env})
        self.validate()

    def write_roots(self):
        return [item for item in self["TERMINAL_WRITE_ROOTS"].split(",") if item]

    def require(self, *keys):
        missing = [key for key in keys if not self[key]]
        if missing:
            raise ValueError("Required settings: " + ", ".join(missing))

    def file(self, key, required=True):
        if not self[key]:
            if required:
                raise ValueError("Required file setting: " + key)
            return None
        path = Path(self[key]).expanduser()
        return path if path.is_absolute() else self.root / path

    def flag(self, key):
        return self[key] == "true"

    def cidrs(self, key):
        return [
            str(ipaddress.ip_network(item.strip(), strict=True))
            for item in self[key].split(",")
            if item.strip()
        ]

    def validate(self):
        self.require("ASSISTANT_NAME", "TERMINAL_HOST", "MODEL", "MODEL_BASE_URL")
        if self["MODEL"] == "replace-with-your-model":
            raise ValueError("Set MODEL to your provider's model ID")
        if len(self["ASSISTANT_NAME"]) > 80 or any(ord(c) < 32 for c in self["ASSISTANT_NAME"]):
            raise ValueError("Invalid ASSISTANT_NAME")
        patterns = {
            "KUBERNETES_NAMESPACE": r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            "TERMINAL_USER": r"[a-z_][a-z0-9_-]{0,30}",
            "TERMINAL_HOST": r"[a-zA-Z0-9._:-]+",
            "STATE_SIZE": r"[1-9][0-9]*(Mi|Gi|Ti)",
            "TERMINAL_WORKSPACE": r"/[a-zA-Z0-9_./-]+",
        }
        for key, pattern in patterns.items():
            if not re.fullmatch(pattern, self[key]) or ".." in self[key].split("/"):
                raise ValueError("Invalid setting: " + key)
        if len(self["TERMINAL_WORKSPACE"].encode()) > 70:
            raise ValueError("TERMINAL_WORKSPACE is too long for the operations socket")
        ZoneInfo(self["TIMEZONE"])
        for key, maximum in [
            ("TERMINAL_PORT", 65535),
            ("TERMINAL_UID", 65535),
            ("TERMINAL_CORES", 256),
            ("TERMINAL_MEMORY_MIB", 4194304),
            ("TERMINAL_DISK_GIB", 65536),
            ("BACKUP_INTERVAL_HOURS", 8760),
            ("BACKUP_KEEP", 1000),
        ]:
            if not self[key].isdigit() or not 1 <= int(self[key]) <= maximum:
                raise ValueError("Invalid positive integer: " + key)
        for key in ("PROXMOX_TLS_VERIFY", "BACKUP_ENABLED"):
            if self[key] not in ("true", "false"):
                raise ValueError("Expected true or false: " + key)
        if self["TERMINAL_KIND"] not in ("existing", "vm", "lxc"):
            raise ValueError("TERMINAL_KIND must be existing, vm or lxc")
        if self["INTERNET_EGRESS"] not in ("external", "restricted"):
            raise ValueError("INTERNET_EGRESS must be external or restricted")
        proxy = urlsplit(self["MODEL_BASE_URL"])
        if (
            proxy.scheme not in ("http", "https")
            or not proxy.hostname
            or proxy.username
            or proxy.password
        ):
            raise ValueError("MODEL_BASE_URL must be an HTTP(S) URL without credentials")
        for key in ("TERMINAL_EGRESS_CIDRS", "MODEL_EGRESS_CIDRS", "API_EGRESS_CIDRS"):
            self.cidrs(key)
        try:
            address = ipaddress.ip_address(self["TERMINAL_HOST"])
        except ValueError:
            if not self.cidrs("TERMINAL_EGRESS_CIDRS"):
                raise ValueError("TERMINAL_EGRESS_CIDRS is required for a terminal hostname")
        else:
            self["TERMINAL_EGRESS_CIDRS"] = (
                self["TERMINAL_EGRESS_CIDRS"] or f"{address}/{address.max_prefixlen}"
            )
        if not self["MODEL_SERVICE_NAMESPACE"] and not self.cidrs("MODEL_EGRESS_CIDRS"):
            raise ValueError("Set MODEL_SERVICE_NAMESPACE or MODEL_EGRESS_CIDRS")
        if self["MODEL_SERVICE_NAMESPACE"] and not re.fullmatch(
            patterns["KUBERNETES_NAMESPACE"], self["MODEL_SERVICE_NAMESPACE"]
        ):
            raise ValueError("Invalid MODEL_SERVICE_NAMESPACE")
        if self["MODEL_SERVICE_NAMESPACE"] and not re.fullmatch(
            r"[A-Za-z0-9_./-]+=[A-Za-z0-9_.-]+", self["MODEL_SERVICE_SELECTOR"]
        ):
            raise ValueError("MODEL_SERVICE_SELECTOR must be one key=value label")
        if self["INSTANCE_REPOSITORY"] and not re.fullmatch(
            r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", self["INSTANCE_REPOSITORY"]
        ):
            raise ValueError("INSTANCE_REPOSITORY must be a GitHub SSH URL ending in .git")
        from hermes_kubernetes.runtime.write_roots import roots

        roots({"write_roots": self.write_roots()})
        for item in self["BACKUP_EXTRA_PATHS"].split(","):
            if item and (
                not re.fullmatch(r"[A-Za-z0-9_./-]+", item)
                or item.startswith("/")
                or ".." in item.split("/")
                or item in (".", ".cache")
            ):
                raise ValueError("BACKUP_EXTRA_PATHS must contain literal relative paths")
        if self["TERMINAL_KIND"] != "existing":
            if self["TERMINAL_PORT"] != "22":
                raise ValueError("Managed guests bootstrap SSH on port 22")
            self.require(
                "PROXMOX_ENDPOINT",
                "PROXMOX_NODE",
                "PROXMOX_GUEST_ID",
                "PROXMOX_ROOT_STORAGE",
                "PROXMOX_SSH_HOST",
                "PROXMOX_SSH_KEY_FILE",
                "PROXMOX_KNOWN_HOSTS_FILE",
                "TERMINAL_IPV4_CIDR",
                "TERMINAL_GATEWAY",
                "WORKSPACE_CEPH_MONITORS",
                "WORKSPACE_CEPH_FILESYSTEM",
                "WORKSPACE_CEPH_SUBVOLUME",
                "WORKSPACE_CEPH_CLIENT",
                "WORKSPACE_CEPH_KEY_FILE",
            )
            if self["TERMINAL_KIND"] == "lxc":
                self.require("PROXMOX_ROOT_PASSWORD_FILE", "PROXMOX_TEMPLATE_STORAGE")
            else:
                self.require("PROXMOX_VM_TEMPLATE_ID")
            if not self["PROXMOX_ROOT_PASSWORD_FILE"] and not self["PROXMOX_API_TOKEN_FILE"]:
                raise ValueError("Provide a Proxmox credential file")
            for key in ("PROXMOX_GUEST_ID", "PROXMOX_VM_TEMPLATE_ID", "PROXMOX_VLAN"):
                if self[key] and (not self[key].isdigit() or int(self[key]) < 1):
                    raise ValueError("Invalid numeric setting: " + key)
            if self["PROXMOX_GUEST_ID"] == self["PROXMOX_VM_TEMPLATE_ID"]:
                raise ValueError("The guest ID must differ from the template ID")
            network = ipaddress.ip_interface(self["TERMINAL_IPV4_CIDR"])
            if str(network.ip) != self["TERMINAL_HOST"]:
                raise ValueError("TERMINAL_HOST must match TERMINAL_IPV4_CIDR")
            for key in (
                "PROXMOX_NODE",
                "PROXMOX_BRIDGE",
                "PROXMOX_SSH_HOST",
                "PROXMOX_SSH_USER",
                "WORKSPACE_CEPH_FILESYSTEM",
                "WORKSPACE_CEPH_CLIENT",
            ):
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", self[key]):
                    raise ValueError("Invalid infrastructure identifier: " + key)
            if (
                self["WORKSPACE_CEPH_CLIENT"] in ("admin", "k3s")
                or not re.fullmatch(r"/[A-Za-z0-9_./-]+", self["WORKSPACE_CEPH_SUBVOLUME"])
                or ".." in self["WORKSPACE_CEPH_SUBVOLUME"].split("/")
            ):
                raise ValueError("Use a dedicated subvolume and limited Ceph client")
            if not re.fullmatch(r"[A-Za-z0-9.:,\[\]-]+", self["WORKSPACE_CEPH_MONITORS"]):
                raise ValueError("Invalid Ceph monitors")
