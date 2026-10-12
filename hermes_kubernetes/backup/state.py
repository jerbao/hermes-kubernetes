"""Private, streaming state archives with consistent backups of every SQLite."""

import hashlib
import json
import os
import posixpath
import sqlite3
import stat
import tarfile
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from hermes_kubernetes.backup.cache import open_read
from hermes_kubernetes.backup.errors import SnapshotRetryError

EXCLUDED = {
    "hermes-agent",
    "node",
    "pkg",
    "runtime",
    "tools",
    "bin",
    "sandboxes",
    "cache",
    "logs",
    "backups",
    "state-snapshots",
    "image_cache",
    "audio_cache",
    "browser-profile",
    "bot-desktop",
    "lsp",
    "desktop",
    "installs",
    "gateway.sock",
    "gateway.pid",
    "gateway.lock",
    "gateway_state.json",
    "processes.json",
    "spawn-ledger.json",
    "serve_restart_pending",
    ".hermes-update-in-progress",
    ".skills_prompt_snapshot.json",
}
TRANSIENT_LOCAL = {
    "runtime",
    "diagnostics",
    "backups",
    "validation-runtime-interface",
    "functional-validation",
}
MANIFEST = "__hermes_snapshot_manifest__.json"


def retained(name):
    parts = Path(name).parts
    if not parts or parts[0] in EXCLUDED:
        return False
    if len(parts) > 2 and parts[0] == "profiles" and parts[2] in EXCLUDED:
        return False
    if len(parts) > 1 and parts[0] == ".local" and parts[1] in TRANSIENT_LOCAL:
        return False
    return not any(
        part in {".git", "node_modules", ".venv", "venv", "__pycache__"}
        or part.endswith((".lock", "-wal", "-shm", ".tmp"))
        or ".bak" in part
        or part.startswith(".env.backup")
        for part in parts
    )


def digest(path):
    checksum = hashlib.sha256()
    with open_read(path) as stream:
        while chunk := stream.read(65536):
            checksum.update(chunk)
    return checksum.hexdigest()


def validate_state_links(links, files):
    """State aliases may point only to a real file included in this archive."""
    for entry in links:
        target = entry["target"]
        if not target or "\x00" in target or target.startswith("/"):
            raise ValueError("State alias must be relative")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(entry["path"]), target))
        if resolved not in files:
            raise ValueError("State alias must point directly to an archived regular file")


def sqlite_snapshot(path, output):
    """Fold committed WAL pages into a private database without stopping its writer."""
    began = time.monotonic()

    def progress(status, remaining, total):
        if time.monotonic() - began > 120:
            raise SnapshotRetryError("SQLite snapshot timed out")

    with (
        closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(output)) as backup,
    ):
        original.execute("PRAGMA cache_size=-2048")
        original.backup(backup, pages=256, progress=progress, sleep=0.05)
    # Backup copies the original pages sequentially. Release them only after
    # SQLite has finished, never while its random integrity reads are active.
    with open_read(path):
        pass
    output.chmod(path.stat().st_mode & 0o777)


def build_snapshot(home, output, *, filter_func=None):
    include = filter_func or retained
    home, output = Path(home).resolve(), Path(output).absolute()
    if output.resolve().is_relative_to(home):
        raise ValueError("Snapshot output must be outside HERMES_HOME")
    if output.exists() or output.is_symlink():
        raise FileExistsError("Snapshot output already exists")
    if (home / MANIFEST).exists():
        raise ValueError("Snapshot manifest name collides with a personal file")
    started = datetime.now(timezone.utc).isoformat()
    entries, links = [], []
    descriptor, name = tempfile.mkstemp(prefix=".hermes-archive-", dir=output.parent)
    temporary_archive = Path(name)
    try:
        with (
            tempfile.TemporaryDirectory(prefix="hermes-databases-", dir=output.parent) as temporary,
            os.fdopen(descriptor, "wb") as stream,
            tarfile.open(fileobj=stream, mode="w:gz", compresslevel=6) as archive,
        ):
            for directory, folders, names in os.walk(home, followlinks=False):
                parent = Path(directory)
                folders[:] = sorted(
                    n for n in folders if include(str((parent / n).relative_to(home)))
                )
                # Bootstrap recreates this runtime alias after restore. Its entire
                # target is already archived through the canonical state root.
                if (
                    parent == home
                    and (home / ".hermes").is_symlink()
                    and os.readlink(home / ".hermes") == "."
                ):
                    folders[:] = [n for n in folders if n != ".hermes"]
                for name in folders:
                    if (parent / name).is_symlink():
                        raise RuntimeError(
                            "Materialize retained directory symlinks before snapshot"
                        )
                # Keep empty personal directories as well as their contents.
                if parent != home:
                    archive.add(parent, arcname=str(parent.relative_to(home)), recursive=False)
                for name in sorted(names):
                    path = parent / name
                    relative = str(path.relative_to(home))
                    if not include(relative):
                        continue
                    if stat.S_ISSOCK(path.lstat().st_mode):
                        continue
                    if path.is_symlink():
                        links.append({"path": relative, "target": os.readlink(path)})
                        archive.add(path, arcname=relative, recursive=False)
                        continue
                    if not path.is_file():
                        raise RuntimeError("Retained state contains a non-regular file")
                    before = path.stat()
                    with path.open("rb") as handle:
                        is_sqlite = handle.read(16) == b"SQLite format 3\x00"
                    source = path
                    if is_sqlite:
                        source = Path(temporary) / "database.sqlite3"
                        sqlite_snapshot(path, source)
                        source.chmod(before.st_mode & 0o777)
                    entry = {
                        "path": relative,
                        "size": source.stat().st_size,
                        "sha256": digest(source),
                        "sqlite": is_sqlite,
                    }
                    with open_read(source) as reader:
                        archive.addfile(archive.gettarinfo(source, arcname=relative), reader)
                    if is_sqlite:
                        source.unlink()
                    else:
                        try:
                            after = path.stat()
                        except FileNotFoundError:
                            raise SnapshotRetryError(
                                "Personal file changed during snapshot; retry"
                            ) from None
                        if (
                            before.st_ino,
                            before.st_mtime_ns,
                            before.st_ctime_ns,
                            before.st_size,
                        ) != (
                            after.st_ino,
                            after.st_mtime_ns,
                            after.st_ctime_ns,
                            after.st_size,
                        ):
                            raise SnapshotRetryError("Personal file changed during snapshot; retry")
                    entries.append(entry)
            try:
                validate_state_links(links, {entry["path"] for entry in entries})
            except ValueError as error:
                raise RuntimeError(
                    "Materialize unsupported state aliases before snapshot"
                ) from error
            manifest = {
                "schema": 1,
                "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "consistency": "sqlite-per-database",
                "files": entries,
            }
            if links:
                manifest["links"] = links
            manifest_path = Path(temporary) / MANIFEST
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
            manifest_path.chmod(0o600)
            archive.add(manifest_path, arcname=MANIFEST, recursive=False)
        # Archive and SQLite staging are private from creation. Publish only a
        # completed archive, without replacing an existing file.
        os.link(temporary_archive, output)
        return {
            "files": len(entries),
            "databases": sum(e["sqlite"] for e in entries),
            "size": output.stat().st_size,
            "consistency": "sqlite-per-database",
        }
    finally:
        temporary_archive.unlink(missing_ok=True)
