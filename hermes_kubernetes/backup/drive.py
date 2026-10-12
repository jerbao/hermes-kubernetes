"""Encrypted remote backups with verified publication and explicit recovery."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

from hermes_kubernetes.backup.archive import (
    BACKUP_NAME,
    WORKSPACE_BACKUP_NAME,
    safe_name,
    verify_archive,
)
from hermes_kubernetes.backup.state import MANIFEST, digest
from hermes_kubernetes.backup.cache import bounded_cache

COLLECTION = re.compile(r"[a-z][a-z0-9-]{0,47}")
SNAPSHOT = re.compile(r"hermes-(state|workspace)-\d{8}T\d{6}Z-[0-9a-f]{12}")


class RemoteError(RuntimeError):
    def __init__(self, command, returncode, diagnostic):
        super().__init__(f"Rclone {command} failed with exit code {returncode}")
        self.command, self.returncode, self.diagnostic = command, returncode, diagnostic


_SAFE_FAILURE_CODES = {
    "Workspace changed during snapshot; retry": "workspace_file_changed_during_snapshot",
    "Workspace paths changed during snapshot; retry": "workspace_paths_changed_during_snapshot",
    "SQLite recovery data changed during snapshot; retry": "workspace_sqlite_recovery_changed_during_snapshot",
    "Workspace reconstruction conditions changed during snapshot; retry": "workspace_policy_changed_during_snapshot",
    "Personal file changed during snapshot; retry": "state_file_changed_during_snapshot",
    "SQLite snapshot timed out": "sqlite_snapshot_timed_out",
}


def safe_failure_reason(error):
    """Return a bounded diagnostic code without exposing arbitrary exception text."""
    if isinstance(error, RemoteError):
        return f"rclone_{error.command}_exit_{error.returncode}"
    return _SAFE_FAILURE_CODES.get(str(error), type(error).__name__)


class Remote:
    """Give rclone a writable private config so refreshing OAuth works in pods."""

    def __init__(self, config, *, remote="hermes-backup:", rclone="rclone"):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*:", remote):
            raise ValueError("Use the root of a configured encrypted remote")
        value = configparser.RawConfigParser()
        with Path(config).open() as reader:
            value.read_file(reader)
        section = remote[:-1]
        if (
            value.get(section, "type", fallback="") != "crypt"
            or not value.get(section, "password", fallback="")
            or not value.get(section, "remote", fallback="")
        ):
            raise ValueError("An encrypted rclone remote and its recovery key are required")
        self.temporary = tempfile.TemporaryDirectory(prefix="hermes-drive-config-")
        self.config = Path(self.temporary.name) / "rclone.conf"
        with os.fdopen(
            os.open(self.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as output:
            value.write(output)
        self.remote, self.rclone = remote, rclone

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.temporary.cleanup()

    def path(self, collection, suffix=""):
        if not COLLECTION.fullmatch(collection):
            raise ValueError("Invalid backup collection")
        if suffix:
            safe_name(suffix)
        return self.remote + collection + ("/" + suffix if suffix else "")

    def run(self, command, *args, missing_ok=False):
        result = subprocess.run(
            [
                self.rclone,
                "--config",
                str(self.config),
                "--log-level",
                "ERROR",
                "--retries",
                "3",
                "--low-level-retries",
                "5",
                "--contimeout",
                "30s",
                "--timeout",
                "5m",
                command,
                *map(str, args),
            ],
            capture_output=True,
            timeout=3600,
        )
        if missing_ok and result.returncode == 3:
            return None
        if result.returncode:
            # stderr can contain credentials or personal paths. Never forward it.
            raise RemoteError(command, result.returncode, result.stderr)
        return result.stdout

    def receipt(self, collection, snapshot, *, wait=False):
        if not SNAPSHOT.fullmatch(snapshot):
            raise ValueError("Invalid snapshot identifier")
        content = None
        for attempt in range(6 if wait else 1):
            if attempt:
                time.sleep(2 ** (attempt - 1))
            content = self.run(
                "cat",
                self.path(collection, snapshot + "/receipt.json"),
                "--count",
                "1048576",
                missing_ok=True,
            )
            if content is not None:
                break
        if content is None:
            return None
        value = json.loads(content)
        kind = SNAPSHOT.fullmatch(snapshot).group(1)
        if (
            value.get("schema") != 1
            or value.get("snapshot") != snapshot
            or value.get("collection") != collection
            or value.get("kind") != kind
            or value.get("archive") != snapshot + ".tar.gz"
            or not re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))
            or type(value.get("size")) is not int
            or value["size"] < 1
        ):
            raise ValueError("Invalid remote backup receipt")
        datetime.fromisoformat(value["completed_at"])
        return value

    def list(self, collection):
        content = self.run(
            "lsjson",
            self.path(collection),
            "--dirs-only",
            "--max-depth",
            "1",
            missing_ok=True,
        )
        if content is None:
            return []
        completed = []
        for entry in json.loads(content):
            name = entry["Name"]
            if entry.get("IsDir") and SNAPSHOT.fullmatch(name):
                # An interrupted upload has no receipt and is never recoverable
                # or eligible for retention. Unrecognized objects are untouched.
                value = self.receipt(collection, name)
                if value is not None:
                    completed.append(value)
        return sorted(completed, key=lambda s: (s["completed_at"], s["snapshot"]), reverse=True)

    def upload(self, archive, collection, *, keep=6):
        archive = Path(archive).resolve()
        if keep < 1:
            raise ValueError("Retain at least one completed backup")
        if BACKUP_NAME.fullmatch(archive.name):
            kind = "state"
        elif WORKSPACE_BACKUP_NAME.fullmatch(archive.name):
            kind = "workspace"
        else:
            raise ValueError("Expected a recognized verified backup archive")
        with bounded_cache(archive):
            return self._upload(archive, collection, keep=keep, kind=kind)

    def _upload(self, archive, collection, *, keep, kind):
        verification = verify_archive(archive)
        expected_consistency = {
            "state": "sqlite-per-database",
            "workspace": "stable-files-and-sqlite-per-database",
        }[kind]
        if verification["consistency"] != expected_consistency:
            raise ValueError("Archive kind does not match its contents")
        snapshot = archive.name.removesuffix(".tar.gz")
        self.run("mkdir", self.path(collection))
        names = json.loads(
            self.run("lsjson", self.path(collection), "--dirs-only", "--max-depth", "1")
        )
        if any(e["Name"] == snapshot for e in names):
            raise FileExistsError("Never overwrite an existing remote snapshot")
        status = {
            "schema": 1,
            "snapshot": snapshot,
            "collection": collection,
            "kind": kind,
            "archive": archive.name,
            "sha256": digest(archive),
            "size": archive.stat().st_size,
            **verification,
        }
        target = self.path(collection, snapshot + "/archive.tar.gz")
        self.run("copyto", archive, target, "--immutable")
        with (
            tempfile.TemporaryDirectory(
                prefix=".drive-verification-", dir=archive.parent
            ) as temporary,
            bounded_cache(temporary),
        ):
            downloaded = Path(temporary) / "archive.tar.gz"
            self.run("copyto", target, downloaded)
            downloaded.chmod(0o600)
            if (
                downloaded.stat().st_size != status["size"]
                or digest(downloaded) != status["sha256"]
                or digest(archive) != status["sha256"]
            ):
                raise ValueError("Remote round-trip or local archive changed")
            status["completed_at"] = datetime.now(timezone.utc).isoformat()
            receipt = Path(temporary) / "receipt.json"
            with os.fdopen(
                os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as output:
                json.dump(status, output)
            self.run(
                "copyto",
                receipt,
                self.path(collection, snapshot + "/receipt.json"),
                "--immutable",
            )
        if self.receipt(collection, snapshot, wait=True) != status:
            raise ValueError("Remote completion receipt failed verification")
        # Retention runs only after the new encrypted archive was downloaded,
        # decrypted, checked and committed. Never sync or purge the Drive root.
        for previous in self.list(collection)[keep:]:
            prefix = previous["snapshot"]
            self.run("deletefile", self.path(collection, prefix + "/archive.tar.gz"))
            self.run("deletefile", self.path(collection, prefix + "/receipt.json"))
            self.run("rmdir", self.path(collection, prefix))
        return status

    def download(self, collection, snapshot, destination):
        destination = Path(destination).absolute()
        if destination.exists() or any(p.is_symlink() for p in [destination, *destination.parents]):
            raise FileExistsError("Download requires a new file without symlink ancestors")
        if snapshot == "latest":
            completed = self.list(collection)
            if not completed:
                raise FileNotFoundError("No completed remote backup")
            status = completed[0]
        else:
            status = self.receipt(collection, snapshot, wait=True)
            if status is None:
                raise FileNotFoundError("Snapshot has not completed publication")
        with (
            tempfile.TemporaryDirectory(
                prefix=".drive-download-", dir=destination.parent
            ) as temporary,
            bounded_cache(temporary),
        ):
            archive = Path(temporary) / "archive.tar.gz"
            self.run(
                "copyto",
                self.path(collection, status["snapshot"] + "/archive.tar.gz"),
                archive,
            )
            archive.chmod(0o600)
            if archive.stat().st_size != status["size"] or digest(archive) != status["sha256"]:
                raise ValueError("Downloaded archive does not match its receipt")
            verification = verify_archive(archive)
            if any(status.get(key) != value for key, value in verification.items()):
                raise ValueError("Archive contents do not match their receipt")
            os.link(archive, destination)
        return status


def restore_state(archive, destination, *, sha256):
    """Restore state as the runtime owner; workspace recovery preserves its UID."""
    archive, destination = Path(archive).resolve(), Path(destination).absolute()
    if digest(archive) != sha256:
        raise ValueError("State checksum mismatch")
    if destination.exists() or any(p.is_symlink() for p in [destination, *destination.parents]):
        raise FileExistsError("Restore requires a new destination without symlinks")
    verification = verify_archive(archive, staging_directory=destination.parent)
    if verification["consistency"] != "sqlite-per-database":
        raise ValueError("Expected state archive")
    destination.mkdir(mode=0o700)
    directories, links = [], []
    with tarfile.open(archive, "r|gz") as reader:
        for entry in reader:
            if entry.name == MANIFEST:
                continue
            path = destination.joinpath(*safe_name(entry.name).parts)
            if entry.isdir():
                path.mkdir(mode=0o700)
                directories.append((entry, path))
            elif entry.isfile():
                with os.fdopen(
                    os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    ),
                    "wb",
                ) as output:
                    shutil.copyfileobj(reader.extractfile(entry), output, length=65536)
                path.chmod(entry.mode & 0o777)
                os.utime(path, (entry.mtime, entry.mtime))
            elif entry.issym():
                links.append((entry, path))
            else:
                raise ValueError("Unsafe state entry")
    # Verification permits only relative aliases to regular archived files.
    # Create them last so extraction never writes through a symlink.
    for entry, path in links:
        path.symlink_to(entry.linkname)
    for entry, path in reversed(directories):
        path.chmod(entry.mode & 0o777)
        os.utime(path, (entry.mtime, entry.mtime))
    return verification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=os.environ.get(
            "HERMES_BACKUP_RCLONE_CONFIG",
            str(Path.home() / ".config/hermes-backup/rclone.conf"),
        ),
    )
    parser.add_argument(
        "--remote", default=os.environ.get("HERMES_BACKUP_REMOTE", "hermes-backup:")
    )
    parser.add_argument("--rclone", default="rclone")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ["backup", "upload", "list", "download", "restore"]:
        sub = commands.add_parser(command)
        sub.add_argument("--collection", required=True)
        if command in {"backup", "upload"}:
            sub.add_argument("--keep", type=int, default=6)
        if command == "backup":
            sub.add_argument("--source", type=Path, required=True)
            sub.add_argument("--directory", type=Path, required=True)
            sub.add_argument("--kind", choices=["state", "workspace"], default="state")
            sub.add_argument("--policy", type=Path)
            sub.add_argument("--workspace-lock", type=Path)
        if command == "upload":
            sub.add_argument("--archive", type=Path, required=True)
        if command in {"download", "restore"}:
            sub.add_argument("--snapshot", default="latest")
            sub.add_argument("--destination", type=Path, required=True)
        if command == "restore":
            sub.add_argument("--staging-directory", type=Path, required=True)
    args = parser.parse_args()
    with Remote(args.config, remote=args.remote, rclone=args.rclone) as remote:
        if args.command == "backup":
            from hermes_kubernetes.backup.job import backup

            policy = json.loads(args.policy.read_text()) if args.policy else None
            status = backup(
                args.source,
                args.directory,
                args.keep,
                kind=args.kind,
                policy=policy,
                workspace_lock=args.workspace_lock
                or (
                    os.environ.get("HERMES_WORKSPACE_LOCK") or None
                    if args.kind == "workspace"
                    else None
                ),
            )
            result = remote.upload(
                args.directory / status["archive"], args.collection, keep=args.keep
            )
        elif args.command == "upload":
            result = remote.upload(args.archive, args.collection, keep=args.keep)
        elif args.command == "list":
            result = remote.list(args.collection)
        elif args.command == "download":
            result = remote.download(args.collection, args.snapshot, args.destination)
        else:
            if args.destination.exists() or args.destination.is_symlink():
                raise FileExistsError("Existing restore destinations are never overwritten")
            with tempfile.TemporaryDirectory(
                prefix="hermes-drive-restore-", dir=args.staging_directory
            ) as temporary:
                archive = Path(temporary) / "archive.tar.gz"
                status = remote.download(args.collection, args.snapshot, archive)
                if status["kind"] == "workspace":
                    from hermes_kubernetes.backup.restore import restore
                else:
                    restore = restore_state
                result = {
                    **status,
                    "restored": restore(archive, args.destination, sha256=status["sha256"]),
                }
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit("Encrypted backup failed: " + safe_failure_reason(error))
