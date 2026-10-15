"""Commands for a private instance consuming this distribution as a dependency."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

BUNDLE = Path(__file__).resolve().parents[2]


def synchronize(root, publish_changes=False):
    from hermes_kubernetes.terminal.sync import sync
    from hermes_kubernetes.terminal.versioning import (
        SocketRuntime,
        reconcile,
        publish,
    )
    from hermes_kubernetes.terminal.guard import guard

    # sync() has its own flock. Release it before native reconciliation.
    state = sync(root)
    if state not in {"current", "updated", "local-changes", "diverged"}:
        raise ValueError("Private workspace requires review: " + state)
    with guard(root / "state/workspace-sync.lock", create=True, blocking=False):
        publication = None
        if publish_changes and state in {"local-changes", "diverged"}:
            # These edits already belong to the private repository. Preserve and
            # merge them before requiring runtime/image compatibility; otherwise
            # a dirty checkout prevents adopting the very release capture needs.
            # publish() still checks paths, secrets, stability and merge conflicts.
            publication = publish(root)
            if publication["status"] not in {"current", "published"}:
                print(json.dumps({**publication, "workspace": state, "phase": "publication"}))
                return 0 if publication["status"] == "waiting-for-stable-files" else 2
        pinned = json.loads((root / "platform.lock.json").read_text())["revision"]
        actual = subprocess.check_output(
            ["git", "-C", str(BUNDLE), "rev-parse", "HEAD"], text=True
        ).strip()
        if pinned != actual:
            # The next invocation loads the newly adopted, immutable bundle.
            # Never reconcile the new instance using this process's old code.
            print(json.dumps({"status": "reload-distribution", "git": publication}))
            return 0
        backend = SocketRuntime(root, root / ".local/runtime/control.sock", bundle=BUNDLE)
        result = reconcile(root, backend)
        if publish_changes and result["status"] == "synchronized":
            result["git"] = publish(root)
        elif result["status"] == "waiting-for-runtime":
            # Never apply projections based on a different active release.
            result["git"] = publication or {"status": "current"}
        print(json.dumps(result))
        if result["status"] != "synchronized":
            return 2
        if publish_changes and result["git"]["status"] not in {
            "current",
            "published",
            "waiting-for-stable-files",
        }:
            return 2
        return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("runtime").add_argument("arguments", nargs=argparse.REMAINDER)
    sync = commands.add_parser("sync")
    sync.add_argument("--publish", action="store_true")
    commands.add_parser("sync-status", help="Report user timer and last workspace sync state")
    backup = commands.add_parser("backup")
    backup.add_argument("--directory", required=True, type=Path)
    backup.add_argument("--keep", type=int, default=6)
    commands.add_parser("source")
    commands.add_parser("source-status")
    update = commands.add_parser("update")
    update.add_argument("--version", default="latest")
    update.add_argument("--check-only", action="store_true")
    commands.add_parser("check")
    identity = commands.add_parser("git-identity")
    identity.add_argument("--check", action="store_true")
    identity.add_argument("--coauthor-name", default="Hermes")
    identity.add_argument("--coauthor-email")
    setup = commands.add_parser("setup")
    setup.add_argument(
        "action", choices=["init", "plan", "configure", "install", "drive-auth", "connect"]
    )
    setup.add_argument("--env-file", type=Path)
    setup.add_argument("--infra", action="store_true")
    a = p.parse_args()
    root = a.root.resolve()
    if a.command == "git-identity":
        from hermes_kubernetes.terminal.git_identity import configure, report

        print(json.dumps(report() if a.check else configure(a.coauthor_name, a.coauthor_email)))
        return 0
    if a.command == "setup":
        from hermes_kubernetes.installation.cli import dispatch

        result = dispatch(root, a.action, env_file=a.env_file, infra=a.infra)
        if isinstance(result, int):
            return result
        print(json.dumps(result, indent=2))
        return 0 if result.get("ready", True) else 2
    if a.command == "runtime":
        environment = dict(os.environ, HERMES_HOME=str(root))
        return subprocess.call(
            [
                sys.executable,
                str(BUNDLE / "hermes_kubernetes/operations/client.py"),
                *a.arguments,
            ],
            env=environment,
        )
    if a.command == "backup":
        environment = dict(os.environ, HERMES_HOME=str(root))
        return subprocess.call(
            [
                sys.executable,
                str(BUNDLE / "hermes_kubernetes/backup/remote.py"),
                "--directory",
                str(a.directory),
                "--keep",
                str(a.keep),
            ],
            env=environment,
        )
    if a.command == "sync":
        return synchronize(root, a.publish)
    if a.command == "sync-status":
        from hermes_kubernetes.terminal.sync_status import report

        result = report()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if a.command == "source":
        from hermes_kubernetes.source.editable import ensure

        print(json.dumps(ensure(BUNDLE, root / "hermes-agent")))
        return 0
    if a.command == "source-status":
        from hermes_kubernetes.source.editable import status

        report = status(BUNDLE, root / "hermes-agent")
        print(json.dumps(report))
        return 0 if report["state"] == "current" else 2
    if a.command == "update":
        from hermes_kubernetes.instances.updates import update

        report = update(root, a.version, write=not a.check_only)
        print(json.dumps(report))
        return 0
    from hermes_kubernetes.releases.manifest import validate
    from hermes_kubernetes.checks.customizations import projection

    validate(json.loads((root / "platform.lock.json").read_text()))
    files = projection(root)
    print(json.dumps({"valid": True, "personal_files": len(files), "model_calls": 0}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit("Instance command failed: " + str(error))
