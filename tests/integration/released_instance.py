"""Verify an actual public release and independent consumers without model calls."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from hermes_kubernetes.instances.generator import create
from hermes_kubernetes.releases.manifest import resolve
from hermes_kubernetes.releases.registry import public_images


def command(argv, directory, environment):
    result = subprocess.run(
        argv, cwd=directory, env=environment, capture_output=True, text=True, timeout=600
    )
    if result.returncode:
        raise RuntimeError(
            f"Release acceptance command failed ({result.returncode}): {' '.join(argv[:2])}\n"
            + result.stderr[-3000:]
        )
    return result.stdout


def verify(repository, version, core_source=False):
    manifest = resolve(repository, version)
    images = public_images(manifest)
    environment = dict(
        os.environ,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        PYTHONPATH="",
    )
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "HERMES_SOURCE_DIR", "GIT_CONFIG_COUNT"):
        environment.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="hermes-release-acceptance-") as temporary:
        roots = [Path(temporary).resolve() / name for name in ("first", "second")]
        for root in roots:
            create(root, manifest)
            result = json.loads(command([sys.executable, "hermes.py", "check"], root, environment))
            if not result.get("valid"):
                raise ValueError("Generated private consumer failed validation")
            cache = root / ".local/platform" / manifest["revision"]
            head = command(["git", "rev-parse", "HEAD"], cache, environment).strip()
            if head != manifest["revision"]:
                raise ValueError("Consumer fetched the wrong public source revision")
            command(["kubectl", "kustomize", str(root)], root, environment)
            changes = command(["git", "status", "--porcelain"], root, environment).strip()
            if changes:
                raise ValueError("Bootstrap changed the private Git inventory")
        if core_source:
            command([sys.executable, "hermes.py", "source"], roots[0], environment)
            command([sys.executable, "hermes.py", "source-status"], roots[0], environment)
            if (roots[1] / "hermes-agent").exists():
                raise ValueError("Independent instances shared a core checkout")
        (roots[0] / "instance.json").write_text('{"name":"private-first"}\n')
        if json.loads((roots[1] / "instance.json").read_text())["name"] == "private-first":
            raise ValueError("Independent instances shared configuration")
    return {
        "release": manifest["version"],
        "revision": manifest["revision"],
        "anonymous_images": images,
        "independent_consumers": 2,
        "core_source_verified": core_source,
        "model_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="jerbao/hermes-kubernetes")
    parser.add_argument("--release", default="latest")
    parser.add_argument("--core-source", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.repository, args.release, args.core_source)))


if __name__ == "__main__":
    main()
