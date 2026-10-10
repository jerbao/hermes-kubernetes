"""Generate non-secret Kubernetes desired state from one installation environment."""

import json
import yaml
from urllib.parse import urlsplit

from hermes_kubernetes.releases.manifest import validate


def documents(settings, manifest):
    validate(manifest)
    s = settings
    instance = {
        "name": s["ASSISTANT_NAME"],
        "network": "cluster",
        "terminal_transport": "direct",
        "terminal_host": s["TERMINAL_HOST"],
        "terminal_port": int(s["TERMINAL_PORT"]),
        "ssh_user": s["TERMINAL_USER"],
        "workspace": s["TERMINAL_WORKSPACE"],
        "model": s["MODEL"],
        "timezone": s["TIMEZONE"],
        "proxy": {"base_url": s["MODEL_BASE_URL"]},
    }
    base = (
        f"https://github.com/{manifest['repository']}//deploy/kubernetes?ref={manifest['revision']}"
    )
    patch = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "hermes"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "env": [{"name": "TZ", "value": s["TIMEZONE"]}],
                            **(
                                {"envFrom": [{"secretRef": {"name": "hermes-dashboard"}}]}
                                if name == "dashboard"
                                else {}
                            ),
                        }
                        for name in ("gateway", "dashboard", "diagnostics", "operations")
                    ]
                }
            }
        },
    }
    patch["spec"]["template"]["spec"]["volumes"] = [
        {
            "name": "policy",
            "projected": {
                "defaultMode": 288,
                "sources": [
                    {"configMap": {"name": "hermes-policy"}},
                    {
                        "secret": {
                            "name": "hermes-runtime-env",
                            "items": [{"key": ".env", "path": "initial.env"}],
                        }
                    },
                ],
            },
        }
    ]
    if s.flag("BACKUP_ENABLED"):
        patch["spec"]["template"]["spec"]["containers"].append(
            {
                "name": "backup",
                "image": "hermes-personal:initial",
                "command": [
                    "/opt/hermes/.venv/bin/python",
                    "-m",
                    "hermes_kubernetes.installation.backups",
                ],
                "env": [
                    {"name": key, "value": s[key]}
                    for key in ("BACKUP_INTERVAL_HOURS", "BACKUP_KEEP", "BACKUP_EXTRA_PATHS")
                ],
                "volumeMounts": [
                    {"name": "state", "mountPath": "/opt/data", "subPath": "home"},
                    {"name": "policy", "mountPath": "/etc/hermes-policy", "readOnly": True},
                    {"name": "tmp", "mountPath": "/tmp"},
                    {"name": "backup-config", "mountPath": "/etc/hermes-backup", "readOnly": True},
                ],
                "resources": {
                    "requests": {"cpu": "10m", "memory": "64Mi"},
                    "limits": {"cpu": "1", "memory": "512Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
            }
        )
        patch["spec"]["template"]["spec"]["volumes"] += [
            {"name": "backup-config", "secret": {"secretName": "hermes-drive", "defaultMode": 288}}
        ]
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "hermes-state"},
        "spec": {"resources": {"requests": {"storage": s["STATE_SIZE"]}}},
    }
    if s["STATE_STORAGE_CLASS"]:
        pvc["spec"]["storageClassName"] = s["STATE_STORAGE_CLASS"]
    egress = [
        {
            "to": [{"ipBlock": {"cidr": cidr}} for cidr in s.cidrs("TERMINAL_EGRESS_CIDRS")],
            "ports": [{"protocol": "TCP", "port": int(s["TERMINAL_PORT"])}],
        }
    ]
    proxy = urlsplit(s["MODEL_BASE_URL"])
    model_targets = [{"ipBlock": {"cidr": cidr}} for cidr in s.cidrs("MODEL_EGRESS_CIDRS")]
    if s["MODEL_SERVICE_NAMESPACE"]:
        key, value = s["MODEL_SERVICE_SELECTOR"].split("=", 1)
        model_targets.append(
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": s["MODEL_SERVICE_NAMESPACE"]}
                },
                "podSelector": {"matchLabels": {key: value}},
            }
        )
    egress.append(
        {
            "to": model_targets,
            "ports": [
                {"protocol": "TCP", "port": proxy.port or (443 if proxy.scheme == "https" else 80)}
            ],
        }
    )
    api = [{"ipBlock": {"cidr": cidr}} for cidr in s.cidrs("API_EGRESS_CIDRS")]
    if s["INTERNET_EGRESS"] == "external":
        # Explicit opt-in: public Internet only. Private infrastructure is allowlisted separately.
        api += [
            {
                "ipBlock": {
                    "cidr": "0.0.0.0/0",
                    "except": [
                        "0.0.0.0/8",
                        "10.0.0.0/8",
                        "100.64.0.0/10",
                        "127.0.0.0/8",
                        "169.254.0.0/16",
                        "172.16.0.0/12",
                        "192.168.0.0/16",
                        "224.0.0.0/4",
                        "240.0.0.0/4",
                    ],
                }
            },
            {
                "ipBlock": {
                    "cidr": "::/0",
                    "except": ["::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8"],
                }
            },
        ]
    if api:
        egress.append(
            {"to": api, "ports": [{"protocol": "TCP", "port": port} for port in (80, 443)]}
        )
    network = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "hermes-configured-egress"},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "hermes"}},
            "policyTypes": ["Egress"],
            "egress": egress,
        },
    }
    overlay = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": s["KUBERNETES_NAMESPACE"],
        "resources": [base, "config/installation/egress.json"],
        "configMapGenerator": [
            {
                "name": "hermes-policy",
                "behavior": "merge",
                "files": [
                    "instance.json=instance.json",
                    "SOUL.md=config/installation/SOUL.md",
                    "config.yaml=config/installation/policy.yaml",
                ],
            }
        ],
        "patches": [
            {"path": "config/installation/deployment.json"},
            {"path": "config/installation/pvc.json"},
            {
                "target": {"kind": "Namespace", "name": "hermes"},
                "patch": json.dumps(
                    [
                        {
                            "op": "replace",
                            "path": "/metadata/name",
                            "value": s["KUBERNETES_NAMESPACE"],
                        }
                    ]
                ),
            },
        ],
        "images": [
            {
                "name": name,
                "newName": manifest["images"][component].split("@")[0],
                "digest": manifest["images"][component].split("@")[1],
            }
            for name, component in [
                ("hermes-personal", "hermes"),
                ("hermes-mcp", "mcp"),
                ("hermes-mcp-codegraph", "mcp-codegraph"),
            ]
        ],
    }
    from hermes_kubernetes.installation.settings import BUNDLE

    policy = yaml.safe_load((BUNDLE / "deploy/kubernetes/policy.yaml").read_text())
    policy["terminal"]["cwd"] = s["TERMINAL_WORKSPACE"]
    return {
        "instance.json": json.dumps(instance, indent=2) + "\n",
        "config/installation/policy.yaml": yaml.safe_dump(policy, sort_keys=False),
        "kustomization.yaml": json.dumps(overlay, indent=2) + "\n",
        "config/installation/deployment.json": json.dumps(patch, indent=2) + "\n",
        "config/installation/pvc.json": json.dumps(pvc, indent=2) + "\n",
        "config/installation/egress.json": json.dumps(network, indent=2) + "\n",
        "config/installation/SOUL.md": f"Your name is {s['ASSISTANT_NAME']}.\n",
    }


def render(settings):
    root = settings.root
    manifest = json.loads((root / "platform.lock.json").read_text())
    result = documents(settings, manifest)
    stamp = root / ".local/installation/rendered.json"
    previous = json.loads(stamp.read_text()) if stamp.exists() else {}
    # Preserve manual overlays: first adoption accepts only the generator's minimal overlay.
    if not previous:
        overlay = yaml.safe_load((root / "kustomization.yaml").read_text())
        if (
            set(overlay)
            - {"apiVersion", "kind", "namespace", "resources", "configMapGenerator", "images"}
            or len(overlay.get("resources", [])) != 1
        ) and overlay != yaml.safe_load(result["kustomization.yaml"]):
            raise ValueError(
                "Existing overlay has customizations; adopt the generated installer in a new instance"
            )
    for name, content in result.items():
        path = root / name
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Generated configuration must not use symlinks")
        if path.exists():
            actual = path.read_text()
            baseline = previous.get(name)
            equivalent = actual in (baseline, content)
            if name == "kustomization.yaml" and baseline is not None:
                current, old, desired = map(yaml.safe_load, (actual, baseline, content))
                if (
                    current.get("images") == desired["images"]
                    and current.get("resources") == desired["resources"]
                ):
                    old["images"], old["resources"] = current["images"], current["resources"]
                equivalent = equivalent or current == old or current == desired
            protected = name in previous or name.startswith("config/installation/")
            if protected and not equivalent:
                raise ValueError("Preserve the manual edit before configuring: " + name)
    for name, content in result.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    from hermes_kubernetes.installation.files import write_private

    write_private(stamp, json.dumps(result))
    return sorted(result)
