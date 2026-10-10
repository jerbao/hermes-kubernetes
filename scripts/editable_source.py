"""Keep editable code aligned with the lock without destroying work or a live runtime."""
import datetime
import fcntl
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from source_tree import fingerprint, is_source_path, source_files
from upstream_source import checkout, commit_locked_patches, ensure_release_tag, git, has_locked_base, inputs_digest, locked_tree, record_checkout, specification


class SourceUpdateError(ValueError):
    pass


def running_processes(source):
    """Only inspect command paths; never read process environments or print arguments."""
    source = source.resolve()
    result = subprocess.run(['ps', '-axww', '-o', 'pid=,ppid=,command='],
                            capture_output=True, text=True, check=True, timeout=15)
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3:
            rows[int(fields[0])] = (int(fields[1]), fields[2])
    ancestors, pid = set(), os.getpid()
    while pid and pid not in ancestors:
        ancestors.add(pid)
        pid = rows.get(pid, (0, ''))[0]
    active = []
    for pid, (_, command) in rows.items():
        if pid in ancestors:
            continue
        try:
            arguments = shlex.split(command)
        except ValueError:
            arguments = command.split()
        referenced = any(arg == str(source) or arg.startswith(str(source) + '/') for arg in arguments)
        proc_cwd = Path(f'/proc/{pid}/cwd')
        try:
            referenced = referenced or proc_cwd.resolve(strict=True).is_relative_to(source)
        except (OSError, RuntimeError):
            pass
        if referenced:
            active.append(pid)
    return active


def matches_lock(root, source):
    """Reconstruct the expected patch tree using a disposable index, not the real one."""
    tree = locked_tree(root, source)
    changed = git(source, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', tree).decode().split('\0')
    if any(name and is_source_path(name) for name in changed):
        return False
    expected = {name for name in git(source, 'ls-tree', '-r', '--name-only', '-z', tree).decode().split('\0')
                if name and is_source_path(name)}
    return {str(name) for name in source_files(source)} == expected


def status(root, source=None):
    root = Path(root).resolve()
    source = Path(source or root / 'hermes-agent').absolute()
    value = specification(root)
    report = {'source': str(source), 'expected_revision': value['revision'],
              'expected_release': value.get('release', {}).get('tag'),
              'source_inputs_sha256': inputs_digest(root), 'state': 'missing'}
    if source.is_symlink():
        raise SourceUpdateError('Source must be an independent directory, not a symlink')
    if not source.exists():
        return report
    if not (source / '.git').is_dir() or Path(git(source, 'rev-parse', '--show-toplevel').decode().strip()).resolve() != source.resolve():
        raise SourceUpdateError('Source must have its own independent Git repository')
    head = git(source, 'rev-parse', 'HEAD').decode().strip()
    record_path = source / '.git/hermes-personal-source.json'
    record = json.loads(record_path.read_text()) if record_path.exists() else None
    if record is not None and (not isinstance(record, dict) or record.get('schema') != 1):
        raise SourceUpdateError('Unsupported editable-source metadata; source was preserved')
    actual = fingerprint(source)
    report.update(actual_revision=head, actual_tree_sha256=actual,
                  actual_release=value.get('release', {}).get('tag') if head == value['revision'] else (record or {}).get('upstream_release'),
                  recorded_inputs_sha256=record.get('source_inputs_sha256') if record else None,
                  recorded_revision=(record.get('derived_revision') or record.get('upstream_revision')) if record else None,
                  recorded_tree_sha256=record.get('source_tree_sha256') if record else None,
                  branch=git(source, 'branch', '--show-current').decode().strip(), managed=bool(record))
    base_matches = has_locked_base(root, source)
    tree_matches = base_matches and matches_lock(root, source) and fingerprint(source) == actual
    report['source_tree_matches_lock'] = tree_matches
    derived = bool(record and record.get('derived_revision') == head)
    if derived:
        changes = git(source, 'diff', '--name-only', '-z', 'HEAD') + git(source, 'diff', '--cached', '--name-only', '-z', 'HEAD')
        derived = not any(name and is_source_path(name) for name in changes.decode().split('\0'))
    if not base_matches:
        report['state'] = 'outdated'
    elif tree_matches:
        report['state'] = 'current' if head == value['revision'] or derived else 'local-edits'
    elif record and record.get('source_inputs_sha256') == report['source_inputs_sha256']:
        report['state'] = 'local-edits'
    else:
        report['state'] = 'patches-outdated'
    if value.get('release'):
        tag = subprocess.run(['git', '-C', str(source), 'rev-parse', '--verify',
                              'refs/tags/' + value['release']['tag'] + '^{commit}'], capture_output=True, timeout=30)
        report['release_tag_matches'] = tag.returncode == 0 and tag.stdout.decode().strip() == value['revision']
        if report['state'] == 'current' and not report['release_tag_matches']:
            report['state'] = 'release-tag-missing' if tag.returncode else 'release-tag-mismatch'
    return report


def install_candidate(candidate, source, archive):
    if archive is None:
        candidate.rename(source)
        return
    # Keep the source directory inode: Kubernetes subPath mounts used by
    # Codegraph would otherwise remain attached to the archived directory.
    archive.mkdir(mode=0o700)
    retained, installed = [], []
    try:
        for child in list(source.iterdir()):
            child.rename(archive / child.name)
            retained.append(child.name)
        for child in list(candidate.iterdir()):
            child.rename(source / child.name)
            installed.append(child.name)
    except BaseException:
        for name in reversed(installed):
            (source / name).rename(candidate / name)
        for name in reversed(retained):
            (archive / name).rename(source / name)
        archive.rmdir()
        raise


def ensure(root, source=None, *, allow_local_edits=False, preserve_existing=False):
    root = Path(root).resolve()
    source = Path(source or root / 'hermes-agent').absolute()
    if source.resolve() == root or root.is_relative_to(source.resolve()):
        raise SourceUpdateError('Source cannot contain the project root')
    private = root / '.local'
    if private.is_symlink():
        raise SourceUpdateError('Private source metadata must not be a symlink')
    private.mkdir(exist_ok=True, mode=0o700)
    with (private / 'source-update.lock').open('a') as guard:
        os.chmod(guard.name, 0o600)
        fcntl.flock(guard, fcntl.LOCK_EX)
        before = status(root, source)
        if source.exists() and has_locked_base(root, source) and before.get('expected_release'):
            try:
                ensure_release_tag(root, source)
            except ValueError as error:
                raise SourceUpdateError(str(error)) from error
            before = status(root, source)
        if before['state'] == 'current':
            try:
                derived = commit_locked_patches(root, source)
            except ValueError as error:
                raise SourceUpdateError(str(error)) from error
            record_checkout(root, source, before['actual_tree_sha256'], derived_revision=derived)
            return status(root, source) if derived else before
        if before['state'] == 'local-edits' and allow_local_edits:
            if before['source_tree_matches_lock']:
                record_checkout(root, source, before['actual_tree_sha256'])
            return before
        if source.exists():
            active = running_processes(source)
            if active:
                raise SourceUpdateError(f"Source is in use by PIDs {active}; prepare a separate --output directory. "
                                        f"Expected {before['expected_revision']}; installed {before['actual_revision']}")
            reproducible = (before['managed'] and not before['branch']
                            and before['actual_revision'] == before['recorded_revision']
                            and before['actual_tree_sha256'] == before['recorded_tree_sha256'])
            if not reproducible and not preserve_existing:
                raise SourceUpdateError(f"Source {before['state']}: expected {before['expected_revision']}, "
                                        f"installed {before['actual_revision']}. Local work or an unmanaged checkout "
                                        'is preserved. Use a separate --output, or explicitly --preserve-existing to archive it before switching.')
        source.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.hermes-source-', dir=source.parent) as temporary:
            candidate = Path(temporary) / 'checkout'
            checkout(root, candidate)
            if inputs_digest(root) != before['source_inputs_sha256']:
                raise SourceUpdateError('The lock or patches changed during preparation; no source was switched')
            archive = None
            if source.exists():
                if (fingerprint(source) != before['actual_tree_sha256']
                        or git(source, 'rev-parse', 'HEAD').decode().strip() != before['actual_revision']
                        or running_processes(source)):
                    raise SourceUpdateError('Source changed or entered use during preparation; no source was switched')
                history = private / 'source-history'
                if history.is_symlink():
                    raise SourceUpdateError('Source history must not be a symlink')
                history.mkdir(exist_ok=True, mode=0o700)
                archive = history / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
                                     + '-' + before['actual_revision'][:12])
            install_candidate(candidate, source, archive)
        after = status(root, source)
        if after['state'] != 'current':
            raise SourceUpdateError('Installed source changed during validation; previous files are retained and execution was blocked')
        after.update(action='updated' if archive else 'created', previous_source=str(archive) if archive else None)
        return after
