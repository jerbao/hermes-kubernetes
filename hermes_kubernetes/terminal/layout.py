"""Review a layout transition while retaining the native source and all user data."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hermes_kubernetes.project.files import (
    allowed,
    assert_paths,
    ignore_text,
    tracked_files,
)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True).stdout


def review(root, revision, check=False, expected_edits=None):
    root = root.resolve()
    if expected_edits is not None:
        pending = set(
            (
                git(root, "diff", "--name-only", "-z", "HEAD")
                + git(root, "ls-files", "--others", "--exclude-standard", "-z")
            )
            .decode()
            .split("\0")
        ) - {""}
        if pending - set(expected_edits):
            raise ValueError("New project edits appeared during review")
        for name, data in expected_edits.items():
            path = root / name
            if path.is_symlink() or (path.read_bytes() if path.is_file() else None) != data:
                raise ValueError("Concurrent project edit during review")
    elif git(root, "status", "--porcelain"):
        raise ValueError("Commit or preserve local project edits before reviewing the layout")
    if git(root, "branch", "--show-current").decode().strip() != "main":
        raise ValueError("Layout adoption requires the main branch")
    old = git(root, "rev-parse", "HEAD").decode().strip()
    target = git(root, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
    git(root, "merge-base", "--is-ancestor", old, target)
    previous, following = tracked_files(root), tracked_files(root, target)
    value = json.loads(git(root, "show", target + ":project-files.json"))
    if git(root, "show", target + ":.gitignore").decode() != ignore_text(value):
        raise ValueError("Target ignore policy does not match the project layout")
    assert_paths(root, following, value)
    removed = previous.keys() - following.keys()
    retained = {name for name in removed if name.startswith("hermes-agent/")}
    retired_projections = removed - retained
    if any(
        not name.startswith("customizations/") or not allowed(name, value)
        for name in retired_projections
    ):
        raise ValueError(
            "Only native source or reviewed customization projections may leave the inventory"
        )
    assert_paths(root, retired_projections, value)
    changes = set(git(root, "diff", "--name-only", "-z", old, target).decode().split("\0")) - {""}
    writes = sorted(changes & following.keys())
    for name in writes:
        path = root / name
        if not allowed(name, value):
            raise ValueError("Private path in target layout")
        if name not in previous and path.exists() and name not in (expected_edits or {}):
            raise ValueError("New project file collides with an existing user file")
        for parent in path.parents:
            if parent == root:
                break
            if parent.exists() and not parent.is_dir():
                raise ValueError("A parent directory collides with an existing file")
    report = {
        "retained_native_files": len(retained),
        "retired_projection_files": len(retired_projections),
        "project_files_updated": len(writes),
        "revision": target,
        "check": check,
    }
    if check:
        return report
    # Only project files and index/HEAD change. Native files, .git, venv and state
    # are never removed or overwritten during removal from the parent inventory.
    backups, created_dirs = {}, []
    index = root / ".git/index"
    previous_index = index.read_bytes()
    try:
        for name in sorted(retired_projections):
            path = root / name
            backups[name] = (path.read_bytes(), path.stat().st_mode & 0o777)
            path.unlink()  # Only Git projections, never the installed skills/<name>.
        for name in writes:
            path = root / name
            backups[name] = (
                (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
            )
            missing = [parent for parent in path.parents if not parent.exists()]
            for parent in reversed(missing):
                parent.mkdir()
                created_dirs.append(parent)
            payload = git(root, "show", target + ":" + name)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                handle.write(payload)
                pending = Path(handle.name)
            pending.chmod(0o755 if following[name] == "100755" else 0o644)
            os.replace(pending, path)
        # Keep cached stat information for unchanged files. read-tree would
        # invalidate the whole index and force thousands of Ceph reads on status.
        entries = {}
        for row in git(root, "ls-tree", "-r", "-z", target).split(b"\0"):
            if not row:
                continue
            metadata, name = row.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            entries[name.decode()] = (mode, oid)
        updates = []
        for name in sorted(changes | set(expected_edits or {})):
            mode, oid = entries.get(name, (b"0", b"0" * 40))
            updates.append(mode + b" " + oid + b"\t" + name.encode() + b"\0")
        if updates:
            subprocess.run(
                ["git", "-C", str(root), "update-index", "-z", "--index-info"],
                input=b"".join(updates),
                capture_output=True,
                check=True,
                timeout=90,
            )
        git(
            root,
            "update-ref",
            "-m",
            "reviewed layout; retained native checkout",
            "HEAD",
            target,
            old,
        )
    except BaseException:
        index.write_bytes(previous_index)
        for name, backup in reversed(list(backups.items())):
            path = root / name
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(backup[0])
                path.chmod(backup[1])
        for directory in reversed(created_dirs):
            directory.rmdir()
        raise
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument(
        "--revision", required=True, help="Explicit, fetched project commit to review"
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(json.dumps(review(args.home, args.revision, args.check)))
