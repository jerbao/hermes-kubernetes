"""Pinned upstream and explicit local patches, independent of a developer checkout."""
import hashlib
import json
import re
import subprocess
import os
import tempfile
from pathlib import Path, PurePosixPath


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                          check=True, timeout=300).stdout


def specification(root):
    value = json.loads((root / 'upstream.lock.json').read_text())
    if value.get('schema') != 1 or not re.fullmatch(r'[0-9a-f]{40}', value['revision']):
        raise ValueError('Unsupported upstream lock or unpinned revision')
    release = value.get('release')
    if release is not None:
        if not isinstance(release, dict) or not re.fullmatch(r'v\d+\.\d+\.\d+', release.get('tag', '')):
            raise ValueError('Upstream release must be a stable version tag')
        if release.get('commit') != value['revision']:
            raise ValueError('Upstream release commit differs from the pinned revision')
        expected_url = value['repository'].rstrip('/') + '/releases/tag/' + release['tag']
        if release.get('url') != expected_url:
            raise ValueError('Upstream release must come from the locked official repository')
    patch_names = set()
    for entry in value['patches']:
        path = PurePosixPath(entry['path'])
        if len(path.parts) != 2 or path.parts[0] != 'patches' or path.suffix != '.patch':
            raise ValueError('Patches must be regular files in patches/')
        if entry['path'] in patch_names:
            raise ValueError('Patch paths must be unique in the application order')
        patch_names.add(entry['path'])
        file = root / path
        if file.is_symlink() or not file.is_file():
            raise ValueError('Patch is missing or is a symlink')
        if hashlib.sha256(file.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Patch checksum does not match the lock')
    tests = value.get('native_tests', [])
    if value['patches'] and not tests:
        raise ValueError('Core patches require affected native test paths')
    for name in tests:
        path = PurePosixPath(name)
        if not name.startswith('tests/') or '..' in path.parts or path.is_absolute():
            raise ValueError('Native test paths must be inside tests/')
    if any('native_tests' in entry for entry in value['patches']):
        grouped_tests = set()
        for entry in value['patches']:
            declared = entry.get('native_tests')
            if not isinstance(declared, list) or any(not isinstance(name, str) or not name.startswith('tests/')
                    or '..' in PurePosixPath(name).parts for name in declared):
                raise ValueError('Grouped patches require native_tests for every patch')
            grouped_tests.update(declared)
        if grouped_tests != set(tests):
            raise ValueError('Native tests must match the union of the patch-specific tests')
    return value


def inputs_digest(root):
    value = specification(root)
    digest = hashlib.sha256((root / 'upstream.lock.json').read_bytes())
    for entry in value['patches']:
        digest.update((root / entry['path']).read_bytes())
    return digest.hexdigest()


def has_locked_base(root, output):
    revision = specification(root)['revision']
    if git(output, 'rev-parse', 'HEAD').decode().strip() == revision:
        return True
    record = output / '.git/hermes-personal-source.json'
    if not record.exists() or json.loads(record.read_text()).get('upstream_revision') != revision:
        return False
    return subprocess.run(['git', '-C', str(output), 'merge-base', '--is-ancestor', revision, 'HEAD'],
                          capture_output=True, timeout=30).returncode == 0


def locked_tree(root, output):
    value = specification(root)
    with tempfile.TemporaryDirectory(prefix='hermes-source-index-') as temporary:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        def indexed(*args):
            return subprocess.run(['git', '-C', str(output), *args], env=env,
                                  capture_output=True, check=True, timeout=60).stdout
        indexed('read-tree', value['revision'])
        for patch in value['patches']:
            indexed('apply', '--cached', str((root / patch['path']).resolve()))
        return indexed('write-tree').decode().strip()


def commit_locked_patches(root, output):
    """Commit the generated patch tree locally, without changing source files."""
    value = specification(root)
    head = git(output, 'rev-parse', 'HEAD').decode().strip()
    if not value['patches'] or head != value['revision']:
        return None
    if git(output, 'branch', '--show-current').strip():
        raise ValueError('Preserve the development branch; use an independent managed checkout')
    tree = locked_tree(root, output)
    if git(output, 'diff', '--name-only', tree).strip():
        raise ValueError('Working files differ from the published patches; preserve local edits')
    index = git(output, 'write-tree').decode().strip()
    base_tree = git(output, 'rev-parse', 'HEAD^{tree}').decode().strip()
    if index not in {tree, base_tree}:
        raise ValueError('Staged work differs from the patch tree; preserve its index')
    if index != tree:
        git(output, 'read-tree', tree)
    date = '@' + git(output, 'show', '-s', '--format=%ct', head).decode().strip() + ' +0000'
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'Hermes distribution',
           'GIT_AUTHOR_EMAIL': 'hermes-distribution@localhost',
           'GIT_COMMITTER_NAME': 'Hermes distribution',
           'GIT_COMMITTER_EMAIL': 'hermes-distribution@localhost',
           'GIT_AUTHOR_DATE': date, 'GIT_COMMITTER_DATE': date}
    derived = subprocess.run(['git', '-C', str(output), 'commit-tree', tree, '-p', head,
                              '-m', 'Managed patches ' + inputs_digest(root)],
                             env=env, capture_output=True, check=True, timeout=60).stdout.decode().strip()
    git(output, 'update-ref', 'HEAD', derived, head)
    return derived


def record_checkout(root, output, tree_sha256=None, *, derived_revision=None):
    from source_tree import fingerprint
    value = specification(root)
    record = {'schema': 1, 'repository': value['repository'],
              'upstream_revision': value['revision'],
              'upstream_release': value.get('release', {}).get('tag'),
              'source_inputs_sha256': inputs_digest(root),
              'source_tree_sha256': tree_sha256 or fingerprint(output)}
    path = output / '.git/hermes-personal-source.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    head = git(output, 'rev-parse', 'HEAD').decode().strip()
    if derived_revision is None and previous.get('derived_revision') == head:
        derived_revision = head
    if derived_revision:
        record['derived_revision'] = derived_revision
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        json.dump(record, stream)
        stream.write('\n')
        pending = Path(stream.name)
    os.replace(pending, path)
    return record


def ensure_release_tag(root, output):
    value = specification(root)
    if not value.get('release'):
        return
    ref = 'refs/tags/' + value['release']['tag']
    existing = subprocess.run(['git', '-C', str(output), 'rev-parse', '--verify', ref + '^{commit}'],
                              capture_output=True, timeout=30)
    if existing.returncode == 0:
        if existing.stdout.decode().strip() != value['revision']:
            raise ValueError('Local release tag differs from the pinned revision; source is preserved')
        return
    record = output / '.git/hermes-personal-source.json'
    known = json.loads(record.read_text()) if record.exists() else {}
    if (known.get('repository') == value['repository']
            and known.get('upstream_revision') == value['revision']
            and known.get('upstream_release') == value['release']['tag']):
        # This checkout already verified the release remotely. Restore its
        # known commit locally without downloading the same objects again.
        git(output, 'update-ref', ref, value['revision'])
        return
    git(output, 'fetch', '--depth', '1', 'origin', ref)
    if git(output, 'rev-parse', 'FETCH_HEAD^{commit}').decode().strip() != value['revision']:
        raise ValueError('Published release tag no longer matches the pinned commit')
    git(output, 'update-ref', ref, git(output, 'rev-parse', 'FETCH_HEAD').decode().strip())


def checkout(root, output):
    """Create a new checkout; never update or overwrite an existing installation."""
    value = specification(root)
    if output.exists():
        raise FileExistsError('Existing source is preserved; choose another checkout directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    git(output.parent, 'init', '-q', str(output))
    git(output, 'remote', 'add', 'origin', value['repository'])
    release = value.get('release')
    fetch_ref = 'refs/tags/' + release['tag'] if release else value['revision']
    git(output, 'fetch', '--depth', '1', 'origin', fetch_ref)
    fetched = git(output, 'rev-parse', 'FETCH_HEAD^{commit}').decode().strip()
    if fetched != value['revision']:
        raise ValueError('Published release tag no longer matches the pinned commit')
    if release:
        # Fetching only to FETCH_HEAD omits the local tag used by native version detection.
        git(output, 'update-ref', 'refs/tags/' + release['tag'], git(output, 'rev-parse', 'FETCH_HEAD').decode().strip())
    git(output, 'checkout', '--detach', '-q', value['revision'])
    if git(output, 'rev-parse', 'HEAD').decode().strip() != value['revision']:
        raise ValueError('Downloaded source does not match the pinned revision')
    for entry in value['patches']:
        git(output, 'apply', '--check', '--index', str((root / entry['path']).resolve()))
        git(output, 'apply', '--index', str((root / entry['path']).resolve()))
    derived = commit_locked_patches(root, output)
    record_checkout(root, output, derived_revision=derived)
    return value
