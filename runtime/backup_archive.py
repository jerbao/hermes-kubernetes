"""Verify private archives and retain only recognized backups owned by the caller."""
import hashlib
import json
import os
import re
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from state_snapshot import MANIFEST, validate_state_links
from backup_cache import bounded_cache

BACKUP_NAME = re.compile(r"hermes-state-\d{8}T\d{6}Z-[0-9a-f]{12}\.tar\.gz")
WORKSPACE_BACKUP_NAME = re.compile(r"hermes-workspace-\d{8}T\d{6}Z-[0-9a-f]{12}\.tar\.gz")


def safe_name(value):
    name = PurePosixPath(value)
    if name.is_absolute() or '..' in name.parts or not name.parts or '\x00' in value \
            or name.as_posix() != value.rstrip('/'):
        raise ValueError('Unsafe snapshot path')
    return name


def verify_archive(path, *, staging_directory=None):
    observed, tree, manifest = {}, {}, None
    with tempfile.TemporaryDirectory(prefix="hermes-verify-", dir=staging_directory or Path(path).parent) as temporary, \
            bounded_cache(temporary), \
            tarfile.open(path, "r|gz") as archive:
        for entry in archive:
            safe_name(entry.name)
            if not (entry.isfile() or entry.isdir() or entry.issym()):
                raise ValueError("Unsafe snapshot entry")
            if entry.name == MANIFEST:
                # Full homes can contain hundreds of thousands of files.
                if not entry.isfile() or manifest is not None or entry.size > 256 * 1024 * 1024:
                    raise ValueError("Invalid snapshot manifest")
                manifest = json.load(archive.extractfile(entry))
                continue
            if entry.name in tree:
                raise ValueError("Duplicate snapshot entry")
            details = {'path': entry.name,
                'type': 'file' if entry.isfile() else 'directory' if entry.isdir() else 'symlink',
                'mode': entry.mode, 'uid': entry.uid, 'gid': entry.gid, 'mtime': entry.mtime}
            tree[entry.name] = details
            if entry.issym():
                if not entry.linkname or '\x00' in entry.linkname:
                    raise ValueError('Invalid symlink target')
                details['target'] = entry.linkname
                continue
            if entry.isdir():
                continue
            checksum = hashlib.sha256()
            reader = archive.extractfile(entry)
            first = reader.read(65536)
            is_sqlite = first[:16] == b"SQLite format 3\x00"
            database_path = Path(temporary) / "verify.sqlite3"
            database_stream = database_path.open("wb") if is_sqlite else None
            try:
                chunk = first
                while chunk:
                    checksum.update(chunk)
                    if database_stream:
                        database_stream.write(chunk)
                    chunk = reader.read(65536)
            finally:
                reader.close()
                if database_stream:
                    database_stream.close()
            if is_sqlite:
                try:
                    with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)) as database:
                        database.execute("PRAGMA cache_size=-2048")
                        if database.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                            raise ValueError("SQLite snapshot integrity failed")
                finally:
                    database_path.unlink()
            observed[entry.name] = {"path": entry.name, "size": entry.size,
                                    "sha256": checksum.hexdigest(), "sqlite": is_sqlite}
            details.update({k: v for k, v in observed[entry.name].items() if k != 'path'})
    if not manifest:
        raise ValueError("Missing or unsupported snapshot manifest")
    if manifest.get('schema') == 1 and manifest.get('consistency') == 'sqlite-per-database':
        observed_links = {name: {'path': name, 'target': entry['target']}
                          for name, entry in tree.items() if entry['type'] == 'symlink'}
        expected_links = {entry['path']: entry for entry in manifest.get('links', [])}
        if len(expected_links) != len(manifest.get('links', [])) or expected_links != observed_links:
            raise ValueError('State aliases do not match their manifest')
        validate_state_links(list(observed_links.values()), observed)
        if any(parent != PurePosixPath('.') and tree.get(str(parent), {}).get('type') != 'directory'
               for name in tree for parent in PurePosixPath(name).parents):
            raise ValueError('State path must have archived directory ancestors')
        expected = {entry['path']: entry for entry in manifest['files']}
        if len(expected) != len(manifest['files']) or expected != observed:
            raise ValueError('Snapshot contents do not match their manifest')
    elif manifest.get('schema') == 2 and manifest.get('kind') == 'workspace' \
            and manifest.get('consistency') == 'stable-files-and-sqlite-per-database':
        expected = {entry['path']: entry for entry in manifest['entries']}
        if len(expected) != len(manifest['entries']) or expected != tree:
            raise ValueError('Workspace contents or metadata do not match their manifest')
        for name, details in tree.items():
            if details['type'] == 'file' and details['mode'] & 0o6000:
                raise ValueError('Privileged executable mode in snapshot')
            for parent in PurePosixPath(name).parents:
                if parent != PurePosixPath('.') and tree.get(str(parent), {}).get('type') != 'directory':
                    raise ValueError('Missing directory or non-directory snapshot ancestor')
        selection = {}
        if 'selection' in manifest or 'selection_sha256' in manifest:
            from workspace_policy import normalize, selected, checksum
            policy = normalize(manifest.get('selection'))
            if manifest.get('selection_sha256') != checksum(policy) \
                    or any(not selected(name, d['type'] == 'directory', policy) for name, d in tree.items()) \
                    or any(p not in tree for p in policy['required']):
                raise ValueError('Workspace archive violates its selection policy')
            selection = {'selection_sha256': checksum(policy),
                         'selected_roots': len(policy['include']), 'excluded_roots': len(policy['exclude'])}
            if policy.get('external_links'):
                selection['out_of_scope_link_targets'] = policy['external_links']
            caches = [e['path'] for e in policy['exclude'] if e['kind'] == 'upstream-git'
                      and e['path'].endswith('/.git/objects/pack')]
            if caches:
                selection['git_caches_to_restore'] = caches
        return {'files': len(observed), 'databases': sum(e['sqlite'] for e in observed.values()),
                'symlinks': sum(e['type'] == 'symlink' for e in tree.values()),
                'consistency': manifest['consistency'], **selection}
    else:
        raise ValueError('Missing or unsupported snapshot manifest')
    result = {"files": len(observed), "databases": sum(e["sqlite"] for e in observed.values()),
              "consistency": manifest["consistency"]}
    if 'links' in manifest:
        result['symlinks'] = len(observed_links)
    return result


def retain_backups(directory, keep, *, kind='state'):
    pattern = {'state': BACKUP_NAME, 'workspace': WORKSPACE_BACKUP_NAME}[kind]
    backups = sorted((p for p in directory.iterdir() if pattern.fullmatch(p.name)
        and not p.is_symlink() and p.is_file() and p.stat().st_uid == os.getuid()),
        key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for path in backups[keep:]:
        path.unlink()
