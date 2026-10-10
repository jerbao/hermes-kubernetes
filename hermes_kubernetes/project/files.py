"""Code inventory for a repository sharing HERMES_HOME with private state."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT_FILES = {
    ".env.example",
    ".dockerignore",
    ".gitignore",
    "AGENTS.md",
    "Makefile",
    "README.md",
    "requirements-dev.txt",
    "upstream.lock.json",
    "project-files.json",
    "pyproject.toml",
    "LICENSE",
    "NOTICE",
    "release.json",
    "hermes.py",
    "platform.lock.json",
    "instance.json",
    "kustomization.yaml",
}
CODE_DIRECTORIES = {
    ".github",
    "patches",
    "config",
    "docs",
    "scripts",
    "tests",
    "customizations",
    "examples",
    "hermes_kubernetes",
    "deploy",
    "containers",
    "dependencies",
    "systemd",
}
PRIVATE_PARTS = {
    ".git",
    ".local",
    ".build",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".env",
    ".codegraph",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
}


def layout(root):
    value = json.loads((Path(root) / "project-files.json").read_text())
    if value.get("schema") != 1:
        raise ValueError("Unsupported project layout")
    if not all(re.fullmatch(r"[a-z_]+\.py", name) for name in value["runtime_files"]):
        raise ValueError("Runtime entries must be Python source filenames")
    if not all(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) for name in value["skills"]):
        raise ValueError("Invalid distribution skill name")
    return value


def allowed(name, value):
    path = PurePosixPath(name)
    parts = path.parts
    if not parts or path.is_absolute() or ".." in parts:
        return False
    if any(
        part in PRIVATE_PARTS
        or part.startswith(".env.")
        and part != ".env.example"
        or part.endswith(
            (
                ".pyc",
                ".pyo",
                ".key",
                ".pem",
                ".db",
                ".sqlite",
                ".sqlite3",
                "-wal",
                "-shm",
            )
        )
        or part.startswith("id_ed25519")
        for part in parts
    ):
        return False
    if len(parts) == 1:
        return name in ROOT_FILES
    if parts[0] in CODE_DIRECTORIES:
        return True
    if parts[0] == "runtime":
        return len(parts) == 2 and parts[1].endswith(".py")
    return parts[0] == "skills" and len(parts) > 2 and parts[1] in value["skills"]


def ignore_text(value):
    lines = [
        "# HERMES_HOME is private by default. Generated from project-files.json.",
        "/*",
    ]
    lines += ["!/" + name for name in sorted(ROOT_FILES)]
    lines += ["!/" + name + "/" for name in sorted(CODE_DIRECTORIES)]
    lines += ["!/runtime/", "/runtime/*"]
    lines += ["!/runtime/*.py"]
    lines += ["!/skills/", "/skills/*"]
    lines += ["!/skills/" + name + "/" for name in value["skills"]]
    lines += ["", "# Private/generated files inside code areas."]
    lines += [
        name + ("/" if name not in {".env", ".DS_Store"} else "") for name in sorted(PRIVATE_PARTS)
    ]
    lines += [
        ".env.*",
        "!.env.example",
        "*.pyc",
        "*.pyo",
        "*.key",
        "id_ed25519*",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        "*-wal",
        "*-shm",
        "*.pem",
        "*.tar",
        "*.tar.gz",
        "auth.json",
        "secrets.yaml",
        "config.runtime.yaml",
    ]
    return "\n".join(lines) + "\n"


def tracked_files(root, revision=None):
    arguments = ["ls-tree", "-r", "-z", revision] if revision else ["ls-files", "--stage", "-z"]
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=True)
    entries = {}
    for entry in result.stdout.decode().split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode = metadata.split()[0]
        if mode not in {"100644", "100755"}:
            raise ValueError("Project files must be regular files, without symlinks or submodules")
        if not revision and metadata.split()[2] != "0":
            raise ValueError("Resolve Git conflicts before using the project")
        entries[name] = mode
    return entries


def assert_paths(root, names, value):
    for name in names:
        if not allowed(name, value):
            raise ValueError(
                "The Git inventory contains a path outside the project layout: " + name
            )
        path = Path(root)
        for part in PurePosixPath(name).parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("Project path contains a symlink: " + name)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    (root / ".gitignore").write_text(ignore_text(layout(root)))
