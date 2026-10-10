"""Download and verify private off-PVC backups; never import them into Git."""
import argparse
import fcntl
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from operations_protocol import Client, file_hash

from backup_archive import verify_archive, retain_backups


def backup(socket_path, directory, keep=6):
    if keep < 1:
        raise ValueError("Retain at least one verified backup")
    directory = Path(directory).absolute()
    workspace = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).resolve()
    if directory.resolve().is_relative_to(workspace):
        raise ValueError("Backup directory must be outside HERMES_HOME")
    if any(p.is_symlink() for p in [directory, *directory.parents]):
        raise ValueError("Backup directory must not contain symlinks")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid():
        raise ValueError("Backup directory belongs to another user")
    directory.chmod(0o700)
    descriptor = os.open(directory / "backup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"hermes-state-{stamp}-{uuid.uuid4().hex[:12]}.tar.gz"
        with tempfile.TemporaryDirectory(prefix=".receiving-", dir=directory) as temporary:
            archive = Path(temporary) / name
            transfer = Client(socket_path, timeout=900).download(archive, "snapshot")
            verification = verify_archive(archive)
            # Transfer checks its hash; verification checks every member and DB.
            if file_hash(archive) != transfer["sha256"]:
                raise ValueError("Archive changed during verification")
            os.link(archive, directory / name)
        status = {"archive": name, "completed_at": datetime.now(timezone.utc).isoformat(),
                  "sha256": transfer["sha256"], "size": transfer["size"], **verification}
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as handle:
            json.dump(status, handle)
            temporary_status = Path(handle.name)
        temporary_status.replace(directory / "latest.json")
        retain_backups(directory, keep)
        return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--socket", type=Path, default=Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / ".local/runtime/control.sock")
    parser.add_argument("--keep", type=int, default=6)
    arguments = parser.parse_args()
    try:
        print(json.dumps(backup(arguments.socket, arguments.directory, arguments.keep)))
    except Exception as error:
        print(f"Runtime backup failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1)
