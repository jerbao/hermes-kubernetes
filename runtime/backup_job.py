"""Write verified, retained state or workspace backups to a separate filesystem."""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid

from backup_archive import retain_backups, verify_archive
from state_snapshot import build_snapshot, digest
from workspace_guard import guard
from backup_cache import bounded_cache


def backup(source, directory, keep=6, *, kind='state', policy=None, workspace_lock=None):
    source, directory = Path(source).resolve(), Path(directory).absolute()
    if kind not in {'state', 'workspace'} or keep < 1 or not source.is_dir():
        raise ValueError('Expected a source directory and positive retention')
    if policy is not None and kind != 'workspace':
        raise ValueError('Selection policies apply only to workspace backups')
    if workspace_lock is not None and kind != 'workspace':
        raise ValueError('Workspace coordination applies only to workspace backups')
    if directory.resolve().is_relative_to(source):
        raise ValueError('Backups must be outside the source filesystem path')
    if any(path.is_symlink() for path in [directory, *directory.parents]):
        raise ValueError('Backup directory must not contain symlinks')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid():
        raise ValueError('Backup directory belongs to another user')
    directory.chmod(0o700)
    descriptor = os.open(directory / 'backup.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as lock, \
            (guard(workspace_lock) if workspace_lock is not None else nullcontext()):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        name = f'hermes-{kind}-{stamp}-{uuid.uuid4().hex[:12]}.tar.gz'
        with tempfile.TemporaryDirectory(prefix='.receiving-', dir=directory) as temporary, bounded_cache(temporary, read_roots=(source,)):
            archive = Path(temporary) / name
            if kind == 'workspace':
                from workspace_snapshot import build_snapshot as build_workspace
                build_workspace(source, archive, policy=policy)
            else:
                build_snapshot(source, archive)
            verification = verify_archive(archive)
            status = {'archive': name, 'completed_at': datetime.now(timezone.utc).isoformat(),
                      'sha256': digest(archive), 'size': archive.stat().st_size, **verification}
            os.link(archive, directory / name)
        with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as handle:
            json.dump(status, handle)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_status = Path(handle.name)
        temporary_status.replace(directory / 'latest.json')
        retain_backups(directory, keep, kind=kind)
        return status


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--keep', type=int, default=6)
    parser.add_argument('--kind', choices=['state', 'workspace'], default='state')
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--workspace-lock', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--report', type=Path)
    arguments = parser.parse_args()
    try:
        policy = json.loads(arguments.policy.read_text()) if arguments.policy else None
        if arguments.dry_run:
            if arguments.kind != 'workspace' or policy is None or arguments.report is None:
                raise ValueError('Workspace dry-run requires a policy and private report path')
            source, report_path = arguments.source.resolve(), arguments.report.absolute()
            if report_path.resolve().is_relative_to(source) \
                    or any(p.is_symlink() for p in report_path.parents):
                raise ValueError('Keep the report outside the source and symlinked directories')
            from workspace_snapshot import preview
            report = preview(source, policy)
            with os.fdopen(os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as output:
                json.dump(report, output, indent=2)
            print(json.dumps({k: report[k] for k in ['ready', 'files', 'bytes', 'selection_sha256', 'consistency']}))
            raise SystemExit(0 if report['ready'] else 1)
        if arguments.directory is None:
            raise ValueError('Backup requires a destination directory')
        print(json.dumps(backup(arguments.source, arguments.directory, arguments.keep,
                               kind=arguments.kind, policy=policy, workspace_lock=arguments.workspace_lock or
                               (os.environ.get('HERMES_WORKSPACE_LOCK') or None if arguments.kind == 'workspace' else None))))
    except Exception as error:
        raise SystemExit('Private backup failed: ' + type(error).__name__)
