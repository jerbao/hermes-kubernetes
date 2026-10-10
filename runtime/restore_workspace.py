"""Restore a verified workspace into a new directory, creating links last."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tarfile

from backup_archive import safe_name, verify_archive
from state_snapshot import MANIFEST, digest


def restore(archive_path, destination, *, sha256):
    archive_path, destination = Path(archive_path).resolve(), Path(destination).absolute()
    if not sha256 or digest(archive_path) != sha256:
        raise ValueError('Archive checksum does not match the selected backup')
    if any(path.is_symlink() for path in [destination, *destination.parents]):
        raise ValueError('Restore destination must not contain symlinks')
    if destination.exists():
        raise FileExistsError('Restore requires a new destination; existing homes are never overwritten')
    verification = verify_archive(archive_path, staging_directory=destination.parent)
    if verification['consistency'] != 'stable-files-and-sqlite-per-database':
        raise ValueError('Expected a workspace archive')
    # Check ownership before extracting anything. Non-root recovery must run as
    # the original owner; heterogeneous ownership requires an operator restore.
    with tarfile.open(archive_path, 'r|gz') as archive:
        for entry in archive:
            if entry.name != MANIFEST and os.geteuid() != 0 \
                    and (entry.uid != os.geteuid() or entry.gid not in [os.getegid(), *os.getgroups()]):
                raise PermissionError('Restore as the original owner or an operator to preserve ownership')
    destination.mkdir(mode=0o700)
    deferred = []
    try:
        with tarfile.open(archive_path, 'r|gz') as archive:
            for entry in archive:
                if entry.name == MANIFEST:
                    continue
                path = destination.joinpath(*safe_name(entry.name).parts)
                if entry.issym():
                    deferred.append((entry, path))
                elif entry.isdir():
                    path.mkdir(mode=0o700)
                    deferred.append((entry, path))
                elif entry.isfile():
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, 'wb') as output, archive.extractfile(entry) as reader:
                        shutil.copyfileobj(reader, output, length=65536)
                    apply_metadata(entry, path)
        # Parent symlinks were refused by verification; absolute and external
        # aliases can be restored faithfully without writing through their targets.
        for entry, path in deferred:
            if entry.issym():
                os.symlink(entry.linkname, path)
                apply_metadata(entry, path)
        for entry, path in reversed(deferred):
            if entry.isdir():
                apply_metadata(entry, path)
        return verification
    except Exception:
        # Keep evidence of partial recovery instead of deleting user-owned paths.
        # Another attempt must use a fresh destination.
        raise


def apply_metadata(entry, path):
    if os.geteuid() == 0:
        os.chown(path, entry.uid, entry.gid, follow_symlinks=False)
    elif entry.gid != os.getegid():
        os.chown(path, -1, entry.gid, follow_symlinks=False)
    if not entry.issym():
        path.chmod(entry.mode)
    os.utime(path, (entry.mtime, entry.mtime), follow_symlinks=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(restore(args.archive, args.destination, sha256=args.sha256)))
    except Exception as error:
        raise SystemExit('Workspace restore failed: ' + type(error).__name__)
