"""Archive selected terminal data, preserving Git, links and browser state."""

import errno
import hashlib
import io
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
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
FILE_SPOOL_MEMORY_LIMIT = 8 * 1024 * 1024


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


def _read_chunk(stream):
    return stream.read(1024 * 1024)


def _copy_chunks(reader, writer):
    checksum = hashlib.sha256()
    size = 0
    while chunk := _read_chunk(reader):
        writer.write(chunk)
        checksum.update(chunk)
        size += len(chunk)
    return size, checksum.hexdigest()


def _retry_path_change():
    return SnapshotRetryError("Workspace changed during snapshot; retry")


def _stat_version(path, expected):
    try:
        current = path.lstat()
    except FileNotFoundError:
        raise SnapshotRetryError("Workspace paths changed during snapshot; retry") from None
    if version(current) != version(expected):
        raise _retry_path_change()
    return current


def _copy_stable_file(path, destination, expected):
    """Spool one regular file from a single descriptor and reject mid-read changes."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise SnapshotRetryError("Workspace paths changed during snapshot; retry") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SnapshotRetryError("Workspace paths changed during snapshot; retry") from None
        raise

    complete = False
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as reader:
            target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(target_fd, "wb", closefd=True) as writer:
                opened = os.fstat(reader.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or version(opened) != version(expected)
                    or version(_stat_version(path, opened)) != version(opened)
                ):
                    raise _retry_path_change()
                size, checksum = _copy_chunks(reader, writer)
                writer.flush()
                finished = os.fstat(reader.fileno())
                if version(finished) != version(opened) or version(
                    _stat_version(path, opened)
                ) != version(opened):
                    raise _retry_path_change()
        complete = True
        return size, checksum
    finally:
        if not complete:
            destination.unlink(missing_ok=True)


@contextmanager
def _spool_stable_file(path, expected, spill_directory, memory_limit=FILE_SPOOL_MEMORY_LIMIT):
    """Capture a stable regular file without a network-filesystem round trip for small files."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise SnapshotRetryError("Workspace paths changed during snapshot; retry") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SnapshotRetryError("Workspace paths changed during snapshot; retry") from None
        raise

    spool = io.BytesIO()
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as reader:
            opened = os.fstat(reader.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or version(opened) != version(expected)
                or version(_stat_version(path, opened)) != version(opened)
            ):
                raise _retry_path_change()
            checksum = hashlib.sha256()
            size = 0
            while chunk := _read_chunk(reader):
                if isinstance(spool, io.BytesIO) and size + len(chunk) > memory_limit:
                    spilled = tempfile.TemporaryFile(mode="w+b", dir=spill_directory)
                    try:
                        spool.seek(0)
                        shutil.copyfileobj(spool, spilled)
                    except BaseException:
                        spilled.close()
                        raise
                    spool.close()
                    spool = spilled
                spool.write(chunk)
                checksum.update(chunk)
                size += len(chunk)
            spool.flush()
            finished = os.fstat(reader.fileno())
            if version(finished) != version(opened) or version(
                _stat_version(path, opened)
            ) != version(opened):
                raise _retry_path_change()
        spool.seek(0)
        yield spool, size, checksum.hexdigest()
    finally:
        spool.close()


def _capture_sqlite_sources(home, name, expected_database, staging):
    """Copy and verify a database plus its required recovery files as one unit."""
    recovery_names = [name + suffix for suffix in ("-wal", "-journal")]
    sources = {name: expected_database}
    for sidecar in recovery_names:
        path = home / sidecar
        try:
            sources[sidecar] = path.lstat()
        except FileNotFoundError:
            continue
    source_names = [name, *(sidecar for sidecar in recovery_names if sidecar in sources)]
    for source_name in source_names:
        expected = sources[source_name]
        _stat_version(home / source_name, expected)
        if not stat.S_ISREG(expected.st_mode):
            raise ValueError("SQLite recovery data is not a regular file")

    private = Path(staging) / "source.sqlite3"
    try:
        _copy_stable_file(home / name, private, sources[name])
        for sidecar in source_names[1:]:
            target = Path(str(private) + sidecar.removeprefix(name))
            _copy_stable_file(home / sidecar, target, sources[sidecar])
        # Changes between the database and sidecar copies invalidate the pair.
        for source_name in source_names:
            _stat_version(home / source_name, sources[source_name])
        for sidecar in recovery_names:
            try:
                present = (home / sidecar).lstat()
            except FileNotFoundError:
                if sidecar in sources:
                    raise SnapshotRetryError(
                        "SQLite recovery data changed during snapshot; retry"
                    ) from None
            else:
                if sidecar not in sources or version(present) != version(sources[sidecar]):
                    raise SnapshotRetryError("SQLite recovery data changed during snapshot; retry")
        connection = sqlite3.connect(private)
        try:
            connection.execute("PRAGMA schema_version").fetchone()
        finally:
            connection.close()
        output = Path(staging) / "database.sqlite3"
        sqlite_snapshot(private, output)
        return output
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(private) + suffix).unlink(missing_ok=True)


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
                try:
                    current = path.lstat()
                except FileNotFoundError:
                    raise SnapshotRetryError(
                        "Workspace paths changed during snapshot; retry"
                    ) from None
                if stat.S_IFMT(current.st_mode) != stat.S_IFMT(details.st_mode):
                    raise _retry_path_change()
                if stat.S_ISDIR(details.st_mode) and identity(current) != identity(details):
                    raise _retry_path_change()
                capture_details = current
                info = tarfile.TarInfo(name)
                info.mode, info.uid, info.gid = (
                    stat.S_IMODE(capture_details.st_mode),
                    capture_details.st_uid,
                    capture_details.st_gid,
                )
                info.mtime = capture_details.st_mtime
                if stat.S_ISDIR(capture_details.st_mode):
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                    entries.append(record(info))
                elif stat.S_ISLNK(capture_details.st_mode):
                    try:
                        target = os.readlink(path)
                        current_after = path.lstat()
                    except FileNotFoundError:
                        raise SnapshotRetryError(
                            "Workspace paths changed during snapshot; retry"
                        ) from None
                    if version(current_after) != version(capture_details):
                        raise _retry_path_change()
                    info.type, info.linkname = tarfile.SYMTYPE, target
                    archive.addfile(info)
                    entries.append(record(info))
                else:
                    if info.mode & 0o6000:
                        raise ValueError("Workspace contains a privileged executable mode")
                    if name in databases:
                        source = _capture_sqlite_sources(home, name, capture_details, staging)
                        try:
                            info.size = source.stat().st_size
                            checksum = digest(source)
                            with open_read(source) as reader:
                                archive.addfile(info, reader)
                        finally:
                            source.unlink(missing_ok=True)
                    else:
                        with _spool_stable_file(path, capture_details, staging) as (
                            reader,
                            info.size,
                            checksum,
                        ):
                            archive.addfile(info, reader)
                    entries.append(record(info, checksum=checksum, sqlite=name in databases))
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
