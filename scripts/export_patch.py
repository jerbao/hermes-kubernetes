"""Export native source edits into the responsibility groups in patches/series.json."""
import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from upstream_source import git, has_locked_base, specification


def series(root):
    value = json.loads((root / 'patches/series.json').read_text())
    if value.get('schema') != 1 or not isinstance(value.get('groups'), list) or not value['groups']:
        raise ValueError('Declare patch groups in patches/series.json')
    names, owners = set(), {}
    for group in value['groups']:
        if not isinstance(group, dict):
            raise ValueError('Patch groups must be objects')
        name = group.get('path', '')
        if not isinstance(name, str) or not re.fullmatch(r'patches/[0-9]{4}-[a-z0-9][a-z0-9-]*\.patch', name) or name in names:
            raise ValueError('Patch group paths must be unique numbered .patch files in patches/')
        names.add(name)
        if not isinstance(group.get('description'), str) or not group['description'].strip():
            raise ValueError('Patch groups require a description')
        files = group.get('files')
        if not isinstance(files, list) or not files:
            raise ValueError('Patch groups require explicit source file paths')
        for name in files:
            if not isinstance(name, str):
                raise ValueError('Patch source paths must be explicit relative filenames')
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or not path.parts or any(
                    token in name for token in ('*', '?', '[', '\\')):
                raise ValueError('Patch source paths must be explicit relative filenames')
            if name in owners:
                raise ValueError('A source file belongs to more than one patch group: ' + name)
            owners[name] = group['path']
        tests = group.get('native_tests')
        if not isinstance(tests, list) or any(not isinstance(name, str) or not name.startswith('tests/')
                or '..' in PurePosixPath(name).parts for name in tests):
            raise ValueError('Declare affected native test paths for every patch group')
    return value['groups'], owners


def export(root, source, tests=None):
    lock = specification(root)
    groups, owners = series(root)
    if not has_locked_base(root, source):
        raise ValueError('Editable source differs from the locked revision; align the checkout before exporting patches')
    # The pinned base must exist locally. Untracked files require explicit native git add.
    paths = set(filter(None, git(source, 'diff', '--no-renames', '--name-only', '-z', lock['revision']).decode().split('\0')))
    forbidden = {'.env', '.git', '.local', '.venv', 'venv', '.codegraph'}
    for name in paths:
        if any(part in forbidden for part in Path(name).parts) or name.endswith(('.db', '.sqlite', '.key', '.pem', '-wal', '-shm')):
            raise ValueError('Private state cannot be exported as a core patch')
    if not paths:
        raise ValueError('No core changes to export')
    unmapped = paths - owners.keys()
    if unmapped:
        raise ValueError('Classify core changes in patches/series.json before exporting: ' + ', '.join(sorted(unmapped)))
    patches, data_by_path, native_tests = [], {}, []
    for group in groups:
        changed = sorted(paths.intersection(group['files']))
        if not changed:
            continue
        data = git(source, '--literal-pathspecs', 'diff', '--binary', '--no-renames',
                   '--no-ext-diff', '--no-textconv', lock['revision'], '--', *changed)
        patches.append({'path': group['path'], 'sha256': hashlib.sha256(data).hexdigest(),
                        'description': group['description'], 'native_tests': group['native_tests']})
        data_by_path[group['path']] = data
        native_tests.extend(name for name in group['native_tests'] if name not in native_tests)
    if any(name not in native_tests for name in (tests or [])):
        raise ValueError('Declare --test paths in the corresponding patches/series.json group')
    if not native_tests:
        raise ValueError('Core patches require affected native test paths')
    candidate = {**lock, 'patches': patches, 'native_tests': native_tests}
    # Validate everything before writing generated patches or the lock.
    folder = root / 'patches'
    folder.mkdir(exist_ok=True)
    for name, data in data_by_path.items():
        file = root / name
        if file.is_symlink():
            raise ValueError('Patch output cannot be a symlink')
    for name, data in data_by_path.items():
        (root / name).write_bytes(data)
    (root / 'upstream.lock.json').write_text(json.dumps(candidate, indent=2, ensure_ascii=False) + '\n')
    # Remove only formerly locked generated patches, never unrelated files.
    for entry in lock['patches']:
        if entry['path'] not in data_by_path:
            (root / entry['path']).unlink()
    return candidate


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=root / 'hermes-agent')
    parser.add_argument('--test', action='append', help='Assert that this test is declared in patches/series.json')
    args = parser.parse_args()
    export(root, args.source.resolve(), args.test)
    print('Grouped patches exported; review patches/series.json, the .patch files and upstream.lock.json before publishing')
