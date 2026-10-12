"""Archive selected terminal data, preserving Git, links and browser state."""

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tarfile
import tempfile

from hermes_kubernetes.backup.state import MANIFEST, digest, sqlite_snapshot
from hermes_kubernetes.backup.errors import SnapshotRetryError
from hermes_kubernetes.backup.policy import (
    normalize,
    review,
    selected,
    checksum as policy_checksum,
)
from hermes_kubernetes.backup.cache import open_read

# No inferred filename or dependency filters. Only an explicit policy can
# omit a subtree; Git, personal .bak/.lock files and profiles remain selectable.
BROWSER_LOCKS = {
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "DevToolsActivePort",
}


def catalogue(home, policy=None):
    entries, skipped = {}, 0

    def visit(parent):
        nonlocal skipped
        try:
            children = sorted(parent.iterdir())
        except FileNotFoundError:
            raise SnapshotRetryError("Workspace paths changed during snapshot; retry") from None
        for path in children:
            relative = path.relative_to(home).as_posix()
            try:
                details = path.lstat()
            except FileNotFoundError:
                raise SnapshotRetryError("Workspace paths changed during snapshot; retry") from None
            if policy is not None and not selected(relative, stat.S_ISDIR(details.st_mode), policy):
                continue
            if path.name == ".snap" and stat.S_ISDIR(details.st_mode):
                skipped += 1
                continue
            # A browser lock is transient only next to its recognizable profile.
            if path.name in BROWSER_LOCKS and (parent / "Local State").is_file():
                skipped += 1
                continue
            if stat.S_ISSOCK(details.st_mode):
                skipped += 1
                continue
            if not (
                stat.S_ISREG(details.st_mode)
                or stat.S_ISDIR(details.st_mode)
                or stat.S_ISLNK(details.st_mode)
            ):
                raise ValueError("Workspace contains an unsupported special file")
            entries[relative] = details
            if stat.S_ISDIR(details.st_mode):
                visit(path)

    visit(home)
    return entries, skipped


def identity(details):
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def version(details):
    return (
        *identity(details),
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def record(info, *, checksum=None, sqlite=False):
    result = {
        "path": info.name,
        "type": "file" if info.isfile() else "directory" if info.isdir() else "symlink",
        "mode": info.mode,
        "uid": info.uid,
        "gid": info.gid,
        "mtime": info.mtime,
    }
    if info.isfile():
        result.update(size=info.size, sha256=checksum, sqlite=sqlite)
    elif info.issym():
        result["target"] = info.linkname
    return result


def preview(home, policy):
    home, policy = Path(home).resolve(), normalize(policy)
    if not home.is_dir():
        raise ValueError("Expected a workspace source directory")
    entries, skipped = catalogue(home, policy)
    return {
        **review(home, entries, policy),
        "policy": policy,
        "skipped_transient": skipped,
        "consistency": "inventory-only",
    }


def build_snapshot(home, output, *, policy=None):
    home, output = Path(home).resolve(), Path(output).absolute()
    if not home.is_dir() or output.resolve().is_relative_to(home):
        raise ValueError("Expected a home and output outside that home")
    if output.exists() or output.is_symlink():
        raise FileExistsError("Snapshot output already exists")
    policy = normalize(policy) if policy is not None else None
    before, skipped = catalogue(home, policy)
    if policy is not None and not review(home, before, policy)["ready"]:
        raise ValueError("Workspace selection needs review; inspect the private dry-run report")
    if MANIFEST in before:
        raise ValueError("Snapshot manifest collides with a personal file")
    databases = set()
    for name, details in before.items():
        if stat.S_ISREG(details.st_mode):
            try:
                with (home / name).open("rb") as handle:
                    if handle.read(16) == b"SQLite format 3\x00":
                        databases.add(name)
            except FileNotFoundError:
                raise SnapshotRetryError("Workspace changed during snapshot; retry") from None
    # Only sidecars of identified SQLite are omitted. A personal file whose
    # name happens to end in -wal/-shm remains a normal archived file.
    sidecars = {name + suffix for name in databases for suffix in ("-wal", "-shm", "-journal")}
    # Selection must never silently omit recovery data of an included database.
    for name in sidecars:
        path = home / name
        if (path.exists() or path.is_symlink()) and name not in before:
            raise ValueError("Workspace selection omitted SQLite recovery data")
    started = datetime.now(timezone.utc).isoformat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".hermes-workspace-", dir=output.parent)
    temporary_archive = Path(temporary_name)
    entries = []
    try:
        with (
            tempfile.TemporaryDirectory(
                prefix="hermes-workspace-db-", dir=output.parent
            ) as staging,
            os.fdopen(descriptor, "wb") as stream,
            tarfile.open(fileobj=stream, mode="w:gz", compresslevel=6) as archive,
        ):
            for name, details in before.items():
                if name in sidecars:
                    continue
                path = home / name
                check = identity if stat.S_ISDIR(details.st_mode) else version
                try:
                    current = path.lstat()
                except FileNotFoundError:
                    raise SnapshotRetryError(
                        "Workspace paths changed during snapshot; retry"
                    ) from None
                if check(current) != check(details) and name not in databases:
                    raise SnapshotRetryError("Workspace changed during snapshot; retry")
                info = tarfile.TarInfo(name)
                info.mode, info.uid, info.gid = (
                    stat.S_IMODE(details.st_mode),
                    details.st_uid,
                    details.st_gid,
                )
                info.mtime = details.st_mtime
                if stat.S_ISDIR(details.st_mode):
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                    entries.append(record(info))
                elif stat.S_ISLNK(details.st_mode):
                    info.type, info.linkname = tarfile.SYMTYPE, os.readlink(path)
                    archive.addfile(info)
                    entries.append(record(info))
                else:
                    if info.mode & 0o6000:
                        raise ValueError("Workspace contains a privileged executable mode")
                    source = path
                    if name in databases:
                        # Chrome can hold exclusive locks even on idle caches.
                        # Copy the database and recovery data privately, then
                        # let SQLite recover/normalize only those copies. No
                        # SQLite connection, WAL lock or write touches the home.
                        private = Path(staging) / "source.sqlite3"
                        recovery = [private]
                        try:
                            shutil.copyfile(path, private)
                            private.chmod(0o600)
                            for suffix in ("-wal", "-journal"):
                                sidecar = home / (name + suffix)
                                if name + suffix in before:
                                    if not stat.S_ISREG(before[name + suffix].st_mode):
                                        raise ValueError(
                                            "SQLite recovery data is not a regular file"
                                        )
                                    copied = Path(str(private) + suffix)
                                    try:
                                        shutil.copyfile(sidecar, copied)
                                    except FileNotFoundError:
                                        raise SnapshotRetryError(
                                            "SQLite recovery data changed during snapshot; retry"
                                        ) from None
                                    copied.chmod(0o600)
                                    recovery.append(copied)
                            # A hot rollback journal needs a writable *private*
                            # connection before the read-only backup operation.
                            connection = sqlite3.connect(private)
                            try:
                                connection.execute("PRAGMA schema_version").fetchone()
                            finally:
                                connection.close()
                            source = Path(staging) / "database.sqlite3"
                            sqlite_snapshot(private, source)
                        finally:
                            for copied in [*recovery, Path(str(private) + "-shm")]:
                                copied.unlink(missing_ok=True)
                    info.size = source.stat().st_size
                    checksum = digest(source)
                    try:
                        with open_read(source) as reader:
                            archive.addfile(info, reader)
                    except FileNotFoundError:
                        raise SnapshotRetryError(
                            "Workspace changed during snapshot; retry"
                        ) from None
                    entries.append(record(info, checksum=checksum, sqlite=name in databases))
                    if source != path:
                        source.unlink()
            try:
                after, _ = catalogue(home, policy)
            except FileNotFoundError:
                raise SnapshotRetryError("Workspace paths changed during snapshot; retry") from None
            if set(after) - sidecars != set(before) - sidecars:
                raise SnapshotRetryError("Workspace paths changed during snapshot; retry")
            for name in set(before) - sidecars:
                check = identity if stat.S_ISDIR(before[name].st_mode) else version
                if check(before[name]) != check(after[name]):
                    raise SnapshotRetryError("Workspace changed during snapshot; retry")
            # WAL/journal are folded into the archive but their source versions
            # must stay unchanged too. SHM is only a regenerable lock index.
            for name in sidecars:
                if name.endswith("-shm"):
                    continue
                if (
                    (name in before) != (name in after)
                    or name in before
                    and version(before[name]) != version(after[name])
                ):
                    raise SnapshotRetryError("SQLite recovery data changed during snapshot; retry")
            if policy is not None and not review(home, after, policy)["ready"]:
                raise SnapshotRetryError(
                    "Workspace reconstruction conditions changed during snapshot; retry"
                )
            manifest = {
                "schema": 2,
                "kind": "workspace",
                "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "consistency": "stable-files-and-sqlite-per-database",
                "skipped_transient": skipped + len(set(before) & sidecars),
                "entries": entries,
            }
            if policy is not None:
                manifest.update(selection=policy, selection_sha256=policy_checksum(policy))
            payload = json.dumps(manifest, ensure_ascii=False).encode()
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mode = len(payload), 0o600
            archive.addfile(info, io.BytesIO(payload))
        os.link(temporary_archive, output)
        return {
            "files": sum(e["type"] == "file" for e in entries),
            "databases": len(databases),
            "symlinks": sum(e["type"] == "symlink" for e in entries),
            "consistency": manifest["consistency"],
            "size": output.stat().st_size,
        }
    finally:
        temporary_archive.unlink(missing_ok=True)
