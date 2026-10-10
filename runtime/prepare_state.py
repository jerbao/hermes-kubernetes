"""Prepare native state from a recovered workspace without changing its files."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from state_snapshot import retained


def separate_env_files(home):
    """Give default and every named profile an independent private .env file."""
    home = Path(home).resolve()
    profiles = home / 'profiles'
    roots = [home]
    if profiles.is_dir() and not profiles.is_symlink():
        roots += sorted(p for p in profiles.iterdir() if not p.name.startswith('.')
                        and p.is_dir() and not p.is_symlink() and (p / 'config.yaml').is_file())
    pending = []
    default_contents = b''
    for root in roots:
        path = root / '.env'
        if path.is_symlink():
            source = path.resolve()
            if not source.is_relative_to(home) or not source.is_file():
                raise ValueError('Profile environment target must be a regular file in this bot home')
            pending.append((path, source.read_bytes(), False))
        elif not path.exists():
            pending.append((path, default_contents, True))
        elif path.is_file() and path.stat().st_nlink > 1:
            pending.append((path, path.read_bytes(), False))
        elif not path.is_file():
            raise ValueError('Profile environment must be a regular file')
        if root == home:
            default_contents = pending[0][1] if pending else path.read_bytes()
    for path, contents, _ in pending:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.env-independent-', delete=False) as output:
                temporary = Path(output.name)
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {'profiles': len(roots), 'copied': sum(not created for _, _, created in pending),
            'created': sum(created for _, _, created in pending)}


def resolve_source(path, workspace, original_home):
    """Rebase original absolute home links before touching the recovered tree."""
    path = Path(os.path.normpath(path))
    for _ in range(64):
        if not path.is_relative_to(workspace):
            raise ValueError('State dependency escapes the recovered workspace')
        current = workspace
        parts = path.relative_to(workspace).parts
        for index, part in enumerate(parts):
            current /= part
            if not current.is_symlink():
                continue
            target = Path(os.readlink(current))
            if target.is_absolute():
                if target.is_relative_to(original_home):
                    target = workspace / target.relative_to(original_home)
                elif not target.is_relative_to(workspace):
                    raise ValueError('Absolute state dependency is outside the original home')
            else:
                target = current.parent / target
            path = Path(os.path.normpath(target.joinpath(*parts[index + 1:])))
            break
        else:
            if not path.exists():
                raise FileNotFoundError('A selected state dependency was not recovered')
            return path
    raise ValueError('State dependency contains a symlink cycle')


def plan(workspace, original_home):
    workspace = Path(workspace).resolve()
    original_home = Path(original_home)
    if not original_home.is_absolute() or str(original_home) != os.path.normpath(original_home):
        raise ValueError('Provide the original absolute user home')
    root = resolve_source(workspace / '.hermes', workspace, original_home)
    if not root.is_dir():
        raise ValueError('Recovered workspace must contain .hermes')
    entries = []

    def visit(path, name, ancestors):
        source = resolve_source(path, workspace, original_home)
        details = source.stat()
        if stat.S_ISSOCK(details.st_mode):
            return
        if source.is_dir():
            if source in ancestors:
                raise ValueError('State directory dependency contains a cycle')
            entries.append((name, source, details, None))
            for child in sorted(source.iterdir()):
                relative = name + '/' + child.name if name else child.name
                if not retained(relative):
                    continue
                if not name and child.name == '.hermes' and child.is_symlink() \
                        and os.readlink(child) == '.':
                    continue
                visit(child, relative, ancestors | {source})
        elif source.is_file():
            if details.st_mode & 0o6000:
                raise ValueError('State contains a privileged executable mode')
            target = None
            if path.is_symlink() and Path(name).name != '.env' and source.is_relative_to(root):
                canonical = source.relative_to(root).as_posix()
                if retained(canonical):
                    target = os.path.relpath(canonical, str(Path(name).parent))
            entries.append((name, source, details, target))
        else:
            raise ValueError('Selected state dependency is not a regular file or directory')

    visit(root, '', set())
    files = {name for name, _, info, target in entries if stat.S_ISREG(info.st_mode) and target is None}
    for name, _, _, target in entries:
        if target is not None and os.path.normpath(str(Path(name).parent / target)) not in files:
            raise ValueError('State alias target was not selected as a regular file')
    return entries


def prepare(workspace, original_home, destination, *, check=False):
    workspace, destination = Path(workspace).resolve(), Path(destination).absolute()
    if destination.exists() or any(p.is_symlink() for p in [destination, *destination.parents]):
        raise FileExistsError('State preparation requires a new destination without symlinks')
    if destination.is_relative_to(workspace):
        raise ValueError('Prepare state outside the recovered workspace')
    entries = plan(workspace, original_home)
    report = {'files': sum(stat.S_ISREG(info.st_mode) and target is None for _, _, info, target in entries),
              'symlinks': sum(target is not None for _, _, _, target in entries),
              'directories': sum(stat.S_ISDIR(info.st_mode) for _, _, info, _ in entries), 'check': check}
    if check:
        return report
    destination.mkdir(mode=0o700)
    directories, links = [], []
    for name, source, details, target in entries:
        output = destination / name
        if stat.S_ISDIR(details.st_mode):
            if name:
                output.mkdir(mode=0o700)
            directories.append((output, details))
        elif target is not None:
            links.append((output, target))
        else:
            with source.open('rb') as reader, output.open('xb') as writer:
                shutil.copyfileobj(reader, writer, length=65536)
            output.chmod(stat.S_IMODE(details.st_mode))
            os.utime(output, ns=(details.st_atime_ns, details.st_mtime_ns))
            after = source.stat()
            if (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns, details.st_ctime_ns) != \
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RuntimeError('Recovered state changed while preparing the new copy')
    for output, target in links:
        output.symlink_to(target)
    for output, details in reversed(directories):
        output.chmod(stat.S_IMODE(details.st_mode))
        os.utime(output, ns=(details.st_atime_ns, details.st_mtime_ns))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--original-home', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.workspace, args.original_home, args.destination, check=args.check)))
    except Exception as error:
        raise SystemExit('State preparation failed: ' + type(error).__name__)
