"""Select checks from changed inputs; personal edits do not rebuild the runtime image."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import fnmatch
import json
from pathlib import Path
import subprocess

IMAGE = [
    "patches/**",
    "containers/hermes.Dockerfile",
    "Makefile",
    ".dockerignore",
    ".gitignore",
    "project-files.json",
    "hermes_kubernetes/**",
    "containers/**",
    "dependencies/**",
    "config/**",
    "skills/**",
    "scripts/**",
    "tests/**",
    "requirements-dev.txt",
    "upstream.lock.json",
]
OPERATIONS = {
    "config/retired-automation.json",
    "scripts/audit_versioning.py",
    "hermes_kubernetes/project/layout.py",
    "hermes_kubernetes/project/review.py",
    "scripts/activate_versioning.py",
    "scripts/remote_versioning.py",
    "scripts/watch_publication.py",
    "hermes_kubernetes/checks/customizations.py",
    "hermes_kubernetes/checks/native_lock.py",
    "hermes_kubernetes/source/features.py",
    "hermes_kubernetes/checks/project.py",
    "hermes_kubernetes/checks/versioning.py",
    "hermes_kubernetes/project/ci_scope.py",
}


def scope(names):
    image = any(
        not name.startswith("hermes_kubernetes/terminal/")
        and name not in OPERATIONS
        and not name.startswith("tests/")
        and fnmatch.fnmatch(name, pattern)
        for name in names
        for pattern in IMAGE
    )
    return {
        "image": image,
        "project": any(
            not name.startswith(("customizations/", "docs/", "mcp/")) and name != "README.md"
            for name in names
        ),
        "customizations": any(name.startswith("customizations/") for name in names),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base")
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    if not a.base or set(a.base) == {"0"}:
        names = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    else:
        names = (
            subprocess.check_output(["git", "diff", "--name-only", "-z", a.base, "HEAD"])
            .decode()
            .split("\0")
        )
    result = scope([n for n in names if n])
    print(json.dumps(result))
    if a.output:
        with a.output.open("a") as out:
            for name, value in result.items():
                out.write(f"{name}={str(value).lower()}\n")


if __name__ == "__main__":
    main()
