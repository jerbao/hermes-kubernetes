"""Offline state archive. Run with the installed VM's Python; never log contents."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

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
    "moa-traces",
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


def retained(name):
    parts = Path(name).parts
    if not parts or parts[0] in EXCLUDED:
        return False
    return not any(
        part in {".git", "node_modules", ".venv", "venv", "__pycache__"}
        or part.endswith((".lock", "-wal", "-shm", ".tmp"))
        or ".bak" in part
        or part.startswith(".env.backup")
        for part in parts
    )


def require_stopped():
    for scope, service in (
        (["--user"], "hermes-gateway.service"),
        ([], "hermes-dashboard.service"),
    ):
        result = subprocess.run(
            ["systemctl", *scope, "is-active", service], capture_output=True, text=True
        )
        if result.stdout.strip() in {"active", "activating", "deactivating"}:
            raise RuntimeError(f"Pare {service} antes do snapshot final")


def snapshot(home, output):
    require_stopped()
    if output.is_relative_to(home):
        raise ValueError("The output file must be outside HERMES_HOME")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        # Credentials must be private from the first byte, including on failure.
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with (
            os.fdopen(descriptor, "wb") as stream,
            tarfile.open(fileobj=stream, mode="w:gz") as archive,
        ):
            for directory, folders, names in os.walk(home, followlinks=False):
                parent = Path(directory)
                folders[:] = [
                    name for name in folders if retained(str((parent / name).relative_to(home)))
                ]
                # External symlinks cannot preserve their VM-only targets in a pod.
                for name in list(folders):
                    path = parent / name
                    if path.is_symlink():
                        raise RuntimeError(
                            f"Materialize the linked directory before taking a snapshot: {path.relative_to(home)}"
                        )
                for name in names:
                    path = parent / name
                    relative = path.relative_to(home)
                    if not retained(str(relative)):
                        continue
                    if path.is_symlink():
                        raise RuntimeError(
                            f"Materialize o arquivo vinculado antes do snapshot: {relative}"
                        )
                    if not path.is_file():
                        continue
                    source = path
                    # Several plugins use .sqlite/.sqlite3 or extensionless DBs.
                    # Detect SQLite itself so excluding WAL never loses commits.
                    with path.open("rb") as handle:
                        is_sqlite = handle.read(16) == b"SQLite format 3\x00"
                    if is_sqlite:
                        source = Path(temporary) / relative
                        source.parent.mkdir(parents=True, exist_ok=True)
                        with closing(
                            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
                        ) as original:
                            with closing(sqlite3.connect(source)) as backup:
                                original.backup(backup)
                    archive.add(source, arcname=str(relative), recursive=False)
    output.chmod(0o600)
    print(f"Snapshot created: {output.name}, {output.stat().st_size} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot(args.home.resolve(), args.output.resolve())
