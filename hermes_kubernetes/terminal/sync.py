"""Fast-forward reviewed code while preserving local work and private HERMES_HOME."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
import subprocess
import sys
from pathlib import Path


from hermes_kubernetes.project.files import allowed, assert_paths, layout, tracked_files
from hermes_kubernetes.source.editable import (
    SourceUpdateError,
    ensure,
    status as source_status,
)
from hermes_kubernetes.terminal.guard import guard


def git(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()


def sync(root, branch="main"):
    try:
        with guard(root / "state/workspace-sync.lock", create=True, blocking=False):
            return sync_locked(root, branch)
    except BlockingIOError:
        return "workspace-busy"


def review_remote(root, local, remote):
    """Check incoming paths even when publishing local commits or merging branches."""
    value = layout(root)
    local_files = tracked_files(root)
    if any(not allowed(name, value) for name in local_files):
        return "protected-paths"
    base = git(root, "merge-base", local, remote)
    paths = set(git(root, "diff", "--name-only", "-z", base, remote).split("\0")) - {""}
    if paths & {"project-files.json", ".gitignore"}:
        return "layout-review-required"
    if "upstream.lock.json" in paths or any(
        name.startswith("patches/") and name.endswith(".patch") for name in paths
    ):
        return "source-review-required"
    remote_files = tracked_files(root, remote)
    if any(not allowed(name, value) for name in remote_files):
        return "protected-paths"
    # Only incoming changed paths can be written by this merge. Traversing
    # every unchanged personalization through Ceph made even a one-file source
    # update take minutes. Git status already rejects dirty local worktrees.
    assert_paths(root, paths & local_files.keys(), value)
    assert_paths(root, paths & remote_files.keys(), value)
    for name in remote_files.keys() - local_files.keys():
        # Git may overwrite ignored files when a remote commit starts tracking
        # them. Refuse even an identical file: it belongs to the current user.
        if (root / name).exists():
            return "existing-files"
        for parent in (root / name).parents:
            if parent == root:
                break
            if parent.exists() and not parent.is_dir():
                return "existing-files"
    return None


def sync_locked(root, branch="main"):
    if git(root, "status", "--porcelain"):
        return "local-changes"
    if git(root, "branch", "--show-current") != branch:
        return "development-branch"
    git(root, "fetch", "origin", branch)
    local = git(root, "rev-parse", "HEAD")
    remote = git(root, "rev-parse", "FETCH_HEAD")
    if local == remote:
        return "current"
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", local, remote],
        capture_output=True,
        timeout=30,
    )
    if ancestor.returncode != 0:
        return "diverged"
    review = review_remote(root, local, remote)
    if review:
        return review
    git(root, "merge", "--ff-only", remote)
    return "updated"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Reconcile native customizations and publish stable source edits",
    )
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[2]
        try:
            with guard(root / "state/workspace-sync.lock", create=True, blocking=False):
                result = sync_locked(root, args.branch)
                versioning = None
                if args.publish and result in {
                    "current",
                    "updated",
                    "local-changes",
                    "diverged",
                }:
                    from hermes_kubernetes.terminal.versioning import (
                        SocketRuntime,
                        reconcile,
                        publish,
                    )

                    versioning = reconcile(
                        root, SocketRuntime(root, root / ".local/runtime/control.sock")
                    )
                    if versioning["status"] == "synchronized":
                        versioning["git"] = publish(root)
                    elif versioning["status"] == "waiting-for-runtime":
                        versioning["git"] = publish(root, code_only=True)
                # Full source verification remains explicit in make source-status/checkout.
                # Do not rescan the complete native tree every minute with no source update.
                source = {"state": "not-checked", "command": "make source-status"}
        except BlockingIOError:
            print(json.dumps({"workspace": "workspace-busy"}))
            raise SystemExit(0)
        print(
            json.dumps(
                {
                    "workspace": result,
                    "editable_source": source,
                    "versioning": versioning,
                }
            )
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"workspace": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1)
    healthy = {"current", "published", "waiting-for-stable-files", "development-branch"}
    if (versioning is None and result not in {"current", "updated"}) or (
        versioning is not None
        and versioning.get("status") != "waiting-for-runtime"
        and (
            versioning.get("status") != "synchronized"
            or versioning.get("git", {}).get("status") not in healthy
        )
    ):
        raise SystemExit(2)
