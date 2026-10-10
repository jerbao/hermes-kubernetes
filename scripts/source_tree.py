"""Content identity of a native Git checkout or an isolated build snapshot."""
import hashlib
import os
import subprocess
from pathlib import Path

IGNORED = {'.git', '.local', '.build', '.venv', 'venv', 'node_modules',
           '__pycache__', '.pytest_cache', '.pytest-cache', '.ruff_cache', '.DS_Store', '.env', '.codegraph'}
GENERATED = {'install-stamp.json', 'source-provenance.json', 'test_durations.json'}


def is_source_path(name):
    return not (any(part in IGNORED for part in Path(name).parts)
                or name in GENERATED or name.endswith(('.pyc', '.pyo')))


def source_files(root):
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError('Source checkout is not installed')
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z',
                                 '--cached', '--others', '--exclude-standard'],
                                capture_output=True, check=True)
        entries = sorted(set(result.stdout.decode().split('\0')) - {''})
    else:
        entries = []
        for directory, folders, names in os.walk(root):
            folders[:] = sorted(name for name in folders if name not in IGNORED)
            for name in folders:
                if (Path(directory) / name).is_symlink():
                    raise ValueError('Source directory is a symlink')
            entries.extend(str((Path(directory) / name).relative_to(root)) for name in names)
        entries.sort()
    for name in entries:
        relative = Path(name)
        if not is_source_path(name):
            continue
        path = root / relative
        if path.is_symlink():
            raise ValueError('Build source contains a symlink: ' + name)
        if path.is_file():
            yield relative


def fingerprint(root):
    digest = hashlib.sha256()
    for relative in source_files(root):
        path = Path(root) / relative
        digest.update(str(relative).encode() + b'\0')
        digest.update(b'x' if path.stat().st_mode & 0o111 else b'-')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
