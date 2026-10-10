"""Generate an independent private consumer of a published distribution release."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
from hermes_kubernetes.releases.manifest import dump, resolve, validate
from hermes_kubernetes.project.files import ignore_text


def create(directory, manifest, initialize=True):
    validate(manifest)
    directory = Path(directory).absolute()
    if directory.exists():
        raise FileExistsError("Instance destination must be new")
    if any(p.is_symlink() for p in directory.parents):
        raise ValueError("Instance parent must not use symlinks")
    shutil.copytree(ROOT / "examples/instance", directory)
    (directory / "platform.lock.json").write_text(dump(manifest))
    contract = {"schema": 1, "runtime_files": [], "skills": []}
    (directory / "project-files.json").write_text(dump(contract))
    (directory / ".gitignore").write_text(ignore_text(contract))
    deployment = directory
    resources = (
        f"https://github.com/{manifest['repository']}//deploy/kubernetes?ref={manifest['revision']}"
    )
    images = []
    for name, component in [
        ("hermes-personal", "hermes"),
        ("hermes-mcp", "mcp"),
        ("hermes-mcp-codegraph", "mcp-codegraph"),
    ]:
        repository, digest = manifest["images"][component].split("@")
        images.append(f"- name: {name}\n  newName: {repository}\n  digest: {digest}")
    (deployment / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: hermes\nresources:\n- "
        + resources
        + "\nconfigMapGenerator:\n- name: hermes-policy\n  behavior: merge\n  files:\n  - instance.json=instance.json\nimages:\n"
        + "\n".join(images)
        + "\n"
    )
    if initialize:
        subprocess.run(["git", "init", "-q", "-b", "main", str(directory)], check=True)
        subprocess.run(["git", "-C", str(directory), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "-c",
                "user.name=Hermes instance bootstrap",
                "-c",
                "user.email=bootstrap@example.invalid",
                "commit",
                "-qm",
                "Initialize private Hermes instance",
            ],
            check=True,
        )
    return {
        "directory": str(directory),
        "version": manifest["version"],
        "private_repository_required": True,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", required=True, type=Path)
    p.add_argument("--repository", default="jerbao/hermes-kubernetes")
    p.add_argument("--release", default="latest")
    a = p.parse_args()
    print(json.dumps(create(a.directory, resolve(a.repository, a.release))))


if __name__ == "__main__":
    main()
