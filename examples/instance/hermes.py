#!/usr/bin/env python3
"""Bootstrap a pinned public dependency; keep personal state in this repository."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def git(directory, *args):
    return subprocess.check_output(
        ["git", "-C", str(directory), *args], stderr=subprocess.PIPE, text=True
    ).strip()


def checkout(root, lock):
    repo, revision = lock.get("repository", ""), lock.get("revision", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(
        r"[0-9a-f]{40}", revision
    ):
        raise ValueError("Invalid pinned distribution")
    cache = root / ".local/platform"
    for path in [cache, *cache.parents]:
        if path.is_symlink():
            raise ValueError("Distribution cache must not use symlinks")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = cache / revision
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix="fetch-", dir=cache) as temporary:
            staging = Path(temporary) / "source"
            staging.mkdir()
            git(staging, "init", "-q")
            git(staging, "remote", "add", "origin", f"https://github.com/{repo}.git")
            git(staging, "fetch", "--depth=1", "origin", revision)
            git(staging, "checkout", "--detach", "-q", revision)
            staging.rename(destination)
    if destination.is_symlink() or (destination / ".git").is_symlink():
        raise ValueError("Aliased dependency checkout")
    if git(destination, "rev-parse", "HEAD") != revision or git(
        destination, "status", "--porcelain", "--untracked-files=all"
    ):
        raise ValueError("Pinned distribution was edited; preserve it and use a clean cache")
    if git(destination, "remote", "get-url", "origin") != f"https://github.com/{repo}.git":
        raise ValueError("Distribution cache belongs to another repository")
    return destination


def main():
    lock = json.loads((ROOT / "platform.lock.json").read_text())
    bundle = checkout(ROOT, lock)
    sys.path.insert(0, str(bundle))
    from hermes_kubernetes.releases.manifest import validate

    validate(lock)
    command = [
        sys.executable,
        str(bundle / "hermes_kubernetes/instances/cli.py"),
        "--root",
        str(ROOT),
        *sys.argv[1:],
    ]
    return subprocess.call(command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit("Hermes dependency failed: " + str(error))
