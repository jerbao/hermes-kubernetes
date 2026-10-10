"""Prepare a private release update atomically; never touch active runtime state."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
from hermes_kubernetes.releases.manifest import dump, resolve, validate
from hermes_kubernetes.state.versioning import atomic
from hermes_kubernetes.terminal.guard import guard


def dependency(root, manifest):
    spec = importlib.util.spec_from_file_location(
        "instance_bootstrap", ROOT / "examples/instance/hermes.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.checkout(root, manifest)


from hermes_kubernetes.instances.skills import reconcile_projection


def update(root, version="latest", write=True, resolver=resolve, fetch=dependency):
    import yaml

    root = Path(root).resolve()
    lock_path = root / "platform.lock.json"
    old_bytes = lock_path.read_bytes()
    old = validate(json.loads(old_bytes))
    new = validate(resolver(old["repository"], version), old["repository"])
    if new == old:
        return {"status": "current", "version": old["version"], "model_calls": 0}
    numeric = lambda value: tuple(map(int, value["version"][1:].split(".")))
    if numeric(new) <= numeric(old):
        raise ValueError("Refusing a downgrade or mutable release")
    old_bundle, new_bundle = fetch(root, old), fetch(root, new)
    report, changes = reconcile_projection(root, old_bundle, new_bundle)
    diagnostics = root / ".local/update-report.json"
    atomic(diagnostics, dump(report).encode())
    if not report["ready"]:
        raise ValueError("Managed skill conflict; inspect private .local/update-report.json")
    deployment = root / "kustomization.yaml"
    original = deployment.read_bytes()
    overlay = yaml.safe_load(original)
    old_resource = (
        f"https://github.com/{old['repository']}//deploy/kubernetes?ref={old['revision']}"
    )
    if old_resource not in overlay.get("resources", []):
        raise ValueError("Private deployment base requires manual review")
    overlay["resources"] = [
        resource.replace(old_resource, old_resource.replace(old["revision"], new["revision"]))
        for resource in overlay["resources"]
    ]
    aliases = {
        "hermes-personal": "hermes",
        "hermes-mcp": "mcp",
        "hermes-mcp-codegraph": "mcp-codegraph",
    }
    present = {item["name"] for item in overlay.get("images", [])}
    if not set(aliases) <= present:
        raise ValueError("Private deployment is missing component image pins")
    for image in overlay["images"]:
        if image["name"] in aliases:
            image["newName"], image["digest"] = new["images"][aliases[image["name"]]].split("@")
            image.pop("newTag", None)
    changes["platform.lock.json"] = (old_bytes, (dump(new).encode(), 0o600))
    changes["kustomization.yaml"] = (
        original,
        (yaml.safe_dump(overlay, sort_keys=False).encode(), 0o600),
    )
    if write:
        with guard(root / "state/workspace-sync.lock", create=True, blocking=False):
            for name, (before, value) in changes.items():
                path = root / name
                if any(p.is_symlink() for p in [path, *path.parents]):
                    raise ValueError("Aliased update destination")
                if (path.read_bytes() if path.exists() else None) != before:
                    raise ValueError("Concurrent private edit: " + name)
            applied = []
            try:
                for name, (before, value) in changes.items():
                    path = root / name
                    if value is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic(path, value[0], value[1])
                    applied.append(name)
            except BaseException:
                for name in reversed(applied):
                    before = changes[name][0]
                    path = root / name
                    if before is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic(path, before)
                raise
    return {
        "status": "updated" if write else "ready",
        "version": new["version"],
        "files": len(changes),
        "skill_review": report,
        "model_calls": 0,
    }
