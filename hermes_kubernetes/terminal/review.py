"""Review a controller upgrade while preserving unpublished project edits."""

import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

from hermes_kubernetes.project.files import allowed, assert_paths
from hermes_kubernetes.terminal.layout import review
from hermes_kubernetes.state.skills import merge
from hermes_kubernetes.state.versioning import (
    CREDENTIAL,
    atomic,
    check_content,
    dump,
    environment_secrets,
)


def git(root, *args, missing=False):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        timeout=600 if args[0] == "status" else 90,
    )
    if result.returncode and not missing:
        raise ValueError("Workspace Git operation failed: " + args[0])
    return None if result.returncode else result.stdout


def upgrade(root, revision):
    root = Path(root).resolve()
    if (
        git(root, "ls-files", "--unmerged")
        or git(root, "branch", "--show-current").strip() != b"main"
    ):
        raise ValueError("Resolve the existing branch/index before upgrading the controller")
    old = git(root, "rev-parse", "HEAD").decode().strip()
    target = git(root, "rev-parse", revision).decode().strip()
    git(root, "merge-base", "--is-ancestor", old, target)
    contract = json.loads(git(root, "show", target + ":project-files.json"))
    names = set(
        (
            git(root, "diff", "--name-only", "-z", "HEAD")
            + git(root, "ls-files", "--others", "--exclude-standard", "-z")
        )
        .decode()
        .split("\0")
    ) - {""}
    if any(not allowed(name, contract) for name in names):
        raise ValueError("Unclassified local project edit")
    assert_paths(root, names, contract)
    objects = (
        git(root, "rev-list", "--objects", target, "--", *sorted(names)).decode().splitlines()
        if names
        else []
    )

    def canonical(name):
        parts = Path(name).parts
        return (
            "/".join(parts[2:])
            if len(parts) > 3 and parts[0] == "customizations" and parts[2] == "skills"
            else name
        )

    # rev-list emits a shared blob only once, possibly under another profile's path.
    published = {
        (sha, canonical(name)) for row in objects if " " in row for sha, name in [row.split(" ", 1)]
    }
    originals, results, conflicts = {}, {}, []
    secrets = environment_secrets(root)
    for name in sorted(names):
        path = root / name
        actual = path.read_bytes() if path.is_file() else None
        base = git(root, "show", old + ":" + name, missing=True)
        desired = git(root, "show", target + ":" + name, missing=True)
        mode = path.stat().st_mode & 0o777 if path.is_file() else 0o600
        originals[name] = {
            "data": base64.b64encode(actual).decode() if actual is not None else None,
            "mode": mode,
        }
        # Manually propagated copies of already published files are not new edits.
        blob = (
            hashlib.sha1(b"blob " + str(len(actual)).encode() + b"\0" + actual).hexdigest()
            if actual is not None
            else None
        )
        known = (blob, canonical(name)) in published
        if not known and actual is not None and "/skills/" in "/" + name and name.endswith(".md"):
            known = any(
                actual.rstrip(b"\r\n") == git(root, "cat-file", "blob", oid).rstrip(b"\r\n")
                for oid, path in published
                if path == canonical(name)
            )
        if known:
            results[name] = desired
            continue
        # Public examples from previously tracked documentation are safe to normalize.
        if actual is not None:
            for example in CREDENTIAL.findall((base or b"").decode(errors="ignore")):
                actual = actual.replace(example.encode(), b"<CREDENTIAL>")
                if base:
                    base = base.replace(example.encode(), b"<CREDENTIAL>")
        parts = Path(name).parts
        if (
            desired is None
            and len(parts) > 4
            and parts[0] == "customizations"
            and parts[2] == "skills"
            and parts[3] in contract["skills"]
        ):
            template = "/".join(parts[2:])
            base = git(root, "show", old + ":" + template, missing=True)
            desired = git(root, "show", target + ":" + template, missing=True)
            try:
                merged = merge(base, desired, actual)
                results[name] = None if merged == desired else merged
            except ValueError:
                conflicts.append(name)
        else:
            try:
                results[name] = merge(base, desired, actual)
            except ValueError:
                conflicts.append(name)
        if results.get(name) is not None:
            check_content(results[name], secrets)
    if conflicts:
        return {"status": "conflict", "paths": conflicts, "written_files": 0}
    # Private history contains only unpublished Git edits, never the native home or databases.
    history = root / ".local/versioning/workspace-review" / (target + ".json.gz")
    if names:
        atomic(
            history,
            gzip.compress(dump({"previous": old, "target": target, "files": originals}), mtime=0),
        )
    try:
        for name in names:
            path = root / name
            expected = (
                base64.b64decode(originals[name]["data"])
                if originals[name]["data"] is not None
                else None
            )
            if (path.read_bytes() if path.is_file() else None) != expected:
                raise ValueError("Concurrent project edit")
        # Review the complete merged plan directly. Reverting to the old files
        # first would temporarily republish old skill instructions on the workspace.
        report = review(
            root,
            target,
            expected_edits={
                name: base64.b64decode(item["data"]) if item["data"] is not None else None
                for name, item in originals.items()
            },
        )
        for name, data in results.items():
            path = root / name
            if data == (path.read_bytes() if path.is_file() else None):
                continue
            if data is None:
                (root / name).unlink(missing_ok=True)
            else:
                atomic(root / name, data, originals[name]["mode"])
    except BaseException:
        if git(root, "rev-parse", "HEAD").decode().strip() == old:
            for name, item in originals.items():
                if item["data"] is None:
                    (root / name).unlink(missing_ok=True)
                else:
                    atomic(root / name, base64.b64decode(item["data"]), item["mode"])
        raise
    return {
        "status": "upgraded",
        "review": report,
        "preserved_local_files": len(names),
        "pending_files": len(git(root, "status", "--porcelain").decode().splitlines()),
    }
