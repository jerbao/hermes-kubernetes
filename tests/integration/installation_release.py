"""Exercise environment setup against an actual pinned release without provisioning."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from hermes_kubernetes.installation.files import write_private
from hermes_kubernetes.instances.generator import create
from hermes_kubernetes.releases.manifest import resolve
from tests.integration.released_instance import command


def verify(repository, version):
    manifest = resolve(repository, version)
    environment = dict(
        os.environ,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        PYTHONPATH="",
    )
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_CONFIG_COUNT"):
        environment.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="hermes-setup-release-") as temporary:
        root = Path(temporary).resolve() / "instance"
        create(root, manifest)
        cli = [sys.executable, "hermes.py", "setup"]
        command(cli + ["init"], root, environment)
        text = (
            (root / ".env")
            .read_text()
            .replace("TERMINAL_HOST=\n", "TERMINAL_HOST=192.0.2.10\n")
            .replace("MODEL=replace-with-your-model", "MODEL=fixture-model")
            .replace("MODEL_API_KEY=\n", "MODEL_API_KEY=fixture-private-key\n")
        )
        write_private(root / ".env", text)
        # A plan must identify the missing SSH key and keep its report secret-free.
        planned = subprocess.run(
            cli + ["plan"], cwd=root, env=environment, capture_output=True, text=True
        )
        report = json.loads(planned.stdout)
        if planned.returncode != 2 or report["ready"] or "fixture-private-key" in planned.stdout:
            raise ValueError("Released installer failed preflight/redaction")
        command(cli + ["configure"], root, environment)
        rendered = command(["kubectl", "kustomize", str(root)], root, environment)
        if "fixture-private-key" in rendered or "initial.env" not in rendered:
            raise ValueError("Released overlay lost credential isolation")
        if command(["git", "status", "--porcelain"], root, environment).strip():
            raise ValueError("Installer left generated desired state uncommitted")
        tracked = command(["git", "ls-files"], root, environment).splitlines()
        if ".env" in tracked or any(name.startswith(".local/") for name in tracked):
            raise ValueError("Private credentials entered the Git inventory")
    return {
        "release": manifest["version"],
        "setup": "init/plan/configure/render passed",
        "anonymous_dependency": True,
        "infrastructure_changes": 0,
        "model_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="jerbao/hermes-kubernetes")
    parser.add_argument("--release", default="latest")
    args = parser.parse_args()
    print(json.dumps(verify(args.repository, args.release)))


if __name__ == "__main__":
    main()
