"""Prove native→Git→native on a unique owned skill, then publish its removal."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import base64
import io
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

root = Path(__file__).resolve().parents[2]
from hermes_kubernetes.operations.protocol import Client
from hermes_kubernetes.terminal.versioning import (
    SocketRuntime,
    reconcile,
    publish,
    digest,
    atomic,
    dump,
)
from hermes_kubernetes.terminal.guard import guard
from hermes_kubernetes.terminal.sync import sync_locked


def resume_proof(directory, client):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(directory), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    names = git(
        "ls-files",
        "--",
        "customizations/default/skills/versioning-acceptance-*/SKILL.md",
    ).splitlines()
    if len(names) != 1:
        raise RuntimeError("Resume requires exactly one tracked owned fixture")
    projected = names[0]
    name = Path(projected).parent.name
    if not re.fullmatch(r"versioning-acceptance-[0-9a-f]{32}", name):
        raise RuntimeError("Unexpected fixture name")
    revisions = git("log", "--format=%H", "--", projected).splitlines()
    creation = peer = None
    for revision in revisions:
        value = subprocess.run(
            ["git", "-C", str(directory), "show", revision + ":" + projected],
            text=True,
            capture_output=True,
        )
        if value.returncode or name not in value.stdout:
            continue
        if "Native edit." in value.stdout:
            creation = revision
        if "Git edit." in value.stdout:
            peer = revision
    if not creation or not peer:
        raise RuntimeError("Git does not contain both sides of this owned proof")
    if git("ls-remote", "origin", "refs/heads/main").split()[0] != git("rev-parse", "HEAD"):
        raise RuntimeError("Receive current GitHub main before resuming")
    expected = git("show", "HEAD:" + projected) + "\n"
    relative = "skills/" + name + "/SKILL.md"
    base = (
        'import json,os;from pathlib import Path;p=Path(os.environ["HERMES_HOME"])/'
        + repr(relative)
        + "\n"
    )

    def native(code):
        out, err = io.BytesIO(), io.BytesIO()
        result = client.execute(
            ["/opt/hermes/.venv/bin/python", "-c", base + code], out, err, timeout=60
        )
        if result["returncode"] or result["timed_out"]:
            raise RuntimeError("Owned proof recovery failed; fixture retained")
        return json.loads(out.getvalue())

    checked = native(
        'print(json.dumps({"sha256":__import__("hashlib").sha256(p.read_bytes()).hexdigest()}))'
    )
    if checked["sha256"] != digest(expected.encode()):
        raise RuntimeError("Native fixture differs from its published Git edit; preserve both")
    report = {
        "fixture": name,
        "passed": True,
        "recovered_after_transport_rollout": True,
        "native_to_git": creation,
        "git_to_native": peer,
    }
    atomic(
        directory / ".local/versioning/live-proof.json",
        dump({**report, "cleanup_pending": True}),
    )
    native(
        "assert p.read_bytes()=="
        + repr(expected.encode())
        + ';p.unlink();p.parent.rmdir();print(json.dumps({"removed":True}))'
    )
    reconciled = reconcile(
        directory, SocketRuntime(directory, directory / ".local/runtime/control.sock")
    )
    if reconciled["status"] != "synchronized":
        raise RuntimeError("Preserve cleanup reconciliation conflict")
    report["cleanup"] = publish(directory, quiet_seconds=0)
    if report["cleanup"]["status"] not in {"published", "current"}:
        raise RuntimeError("Cleanup publication remains pending")
    atomic(directory / ".local/versioning/live-proof.json", dump(report))
    print(json.dumps(report))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=root)
    p.add_argument(
        "--resume",
        action="store_true",
        help="Verify and clean the already published owned fixture after a transport interruption",
    )
    a = p.parse_args()
    directory = a.root.resolve()
    client = Client(str(directory / ".local/runtime/control.sock"), timeout=300)
    if a.resume:
        with guard(directory / "state/workspace-sync.lock", create=True, blocking=False):
            resume_proof(directory, client)
        return
    name = "versioning-acceptance-" + uuid.uuid4().hex
    relative = "skills/" + name + "/SKILL.md"
    projected = directory / "customizations/default" / relative
    report = {"fixture": name, "passed": False}
    before = (
        "---\nname: "
        + name
        + "\ndescription: Owned versioning acceptance fixture.\n---\n\nNative edit.\n"
    )

    def native(code):
        out, err = io.BytesIO(), io.BytesIO()
        result = client.execute(["/opt/hermes/.venv/bin/python", "-c", code], out, err, timeout=240)
        if result["returncode"] or result["timed_out"]:
            atomic(directory / ".local/versioning/live-proof-error.log", err.getvalue())
            raise RuntimeError("Owned fixture operation failed")
        return json.loads(out.getvalue())

    def roundtrip():
        result = reconcile(
            directory,
            SocketRuntime(directory, directory / ".local/runtime/control.sock"),
        )
        if result["status"] != "synchronized":
            raise RuntimeError("Reconciliation did not complete")
        result["git"] = publish(directory, quiet_seconds=0)
        if result["git"]["status"] not in {"published", "current"}:
            raise RuntimeError("Publication did not complete")
        return result

    base = (
        'import json,os;from pathlib import Path;p=Path(os.environ["HERMES_HOME"])/'
        + repr(relative)
        + "\n"
    )
    with guard(directory / "state/workspace-sync.lock", create=True, blocking=False):
        if subprocess.check_output(
            ["git", "-C", str(directory), "status", "--porcelain"], text=True
        ).strip():
            raise RuntimeError("Use a clean project for the live proof")
        try:
            native(
                base
                + "assert not p.exists();p.parent.mkdir();p.write_text("
                + repr(before)
                + ');p.chmod(0o600);print(json.dumps({"created":True}))'
            )
            report["native_to_git"] = roundtrip()["git"]
            atomic(directory / ".local/versioning/live-proof.json", dump(report))
            if projected.read_text() != before:
                raise AssertionError("Native edit missing in Git projection")
            revision = subprocess.check_output(
                ["git", "-C", str(directory), "rev-parse", "HEAD"], text=True
            ).strip()
            remote = subprocess.check_output(
                ["git", "-C", str(directory), "ls-remote", "origin", "refs/heads/main"],
                text=True,
            ).split()[0]
            if revision != remote:
                raise AssertionError("Native edit was not published to GitHub")
            changed = before.replace("Native edit.", "Git edit.")
            # Publish from an independent bare clone, then exercise the terminal's real
            # fetch/fast-forward path. Shared objects avoid duplicating the whole checkout.
            with tempfile.TemporaryDirectory(
                prefix="git-peer-", dir=directory / ".local/versioning"
            ) as temporary:
                peer = Path(temporary) / "peer.git"
                subprocess.run(
                    [
                        "git",
                        "clone",
                        "--bare",
                        "--shared",
                        "--quiet",
                        str(directory),
                        str(peer),
                    ],
                    check=True,
                )

                def peer_git(*arguments, input=None):
                    return subprocess.check_output(
                        ["git", "-C", str(peer), *arguments],
                        input=input,
                        text=True,
                        stderr=subprocess.PIPE,
                    ).strip()

                origin = subprocess.check_output(
                    ["git", "-C", str(directory), "remote", "get-url", "origin"],
                    text=True,
                ).strip()
                peer_git("remote", "set-url", "origin", origin)
                for key in ["user.name", "user.email"]:
                    peer_git(
                        "config",
                        key,
                        subprocess.check_output(
                            ["git", "-C", str(directory), "config", key], text=True
                        ).strip(),
                    )
                blob = peer_git("hash-object", "-w", "--stdin", input=changed)
                peer_git("read-tree", "HEAD")
                peer_git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    blob,
                    projected.relative_to(directory).as_posix(),
                )
                tree = peer_git("write-tree")
                commit = peer_git(
                    "commit-tree",
                    tree,
                    "-p",
                    "HEAD",
                    "-m",
                    "test: verifica retorno da skill do Git ao runtime",
                )
                peer_git("push", "origin", commit + ":refs/heads/main")
            report["git_peer"] = commit
            atomic(directory / ".local/versioning/live-proof.json", dump(report))
            if sync_locked(directory) != "updated":
                raise AssertionError("Published GitHub edit was not received by fast-forward")
            report["git_to_native"] = roundtrip()["to_runtime"]
            checked = native(
                base
                + 'print(json.dumps({"sha256":__import__("hashlib").sha256(p.read_bytes()).hexdigest()}))'
            )
            if checked["sha256"] != digest(changed.encode()):
                raise AssertionError("Git edit did not reach native skill")
            report["passed"] = True
        finally:
            # Only this UUID-owned file is removed, with content validation before deletion.
            native(
                base
                + "\nif p.exists():\n text=p.read_text();assert "
                + repr(name)
                + ' in text;p.unlink();p.parent.rmdir()\nprint(json.dumps({"removed":True}))'
            )
            report["cleanup"] = roundtrip()["git"]
            atomic(directory / ".local/versioning/live-proof.json", dump(report))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
