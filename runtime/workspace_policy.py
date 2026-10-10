"""Explicit workspace selection; never infer exclusions from dependency names."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


def path_name(value, *, root=False):
    if root and value == '.':
        return value
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Invalid policy path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != value \
            or value == '.' or any(c in value for c in '*?[]'):
        raise ValueError('Policy paths must be literal relative paths')
    return value


def beneath(name, parent):
    return parent == '.' or name == parent or name.startswith(parent + '/')


def normalize(value):
    if not isinstance(value, dict) or value.get('schema') != 1 \
            or set(value) - {'schema', 'include', 'exclude', 'required', 'external_links'}:
        raise ValueError('Unsupported workspace policy')
    includes = value.get('include')
    if not isinstance(includes, list) or not includes:
        raise ValueError('Policy requires explicit include roots')
    includes = sorted(set(path_name(p, root=True) for p in includes))
    required = value.get('required', [])
    if not isinstance(required, list):
        raise ValueError('Invalid required roots')
    required = sorted(set(path_name(p) for p in required))
    exclusions = value.get('exclude', [])
    if not isinstance(exclusions, list):
        raise ValueError('Invalid policy exclusions')
    cleaned = []
    for item in exclusions:
        fields = {'path', 'kind', 'reason', 'rebuild'}
        if isinstance(item, dict) and item.get('kind') == 'upstream-git':
            fields |= {'repository', 'revision', 'branch'}
            if item.get('path', '').endswith('/.git/objects/pack'):
                fields.add('tags_sha256')
        if isinstance(item, dict) and item.get('kind') == 'package-cache':
            fields |= {'recipe'}
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError('Exclusions require a reason and recovery description')
        name = path_name(item['path'])
        upstream_git = item['kind'] == 'upstream-git'
        if ('.git' in PurePosixPath(name).parts and not upstream_git) or PurePosixPath(name).name == '.cache':
            raise ValueError('Do not exclude Git metadata or a whole cache directory')
        if item['kind'] not in {'rebuildable', 'generated-validation', 'upstream-git', 'package-cache'} \
                or not all(isinstance(item[k], str) and item[k].strip() for k in ['reason', 'rebuild']):
            raise ValueError('Invalid exclusion classification')
        if upstream_git and (not (name.endswith('/.git') or name.endswith('/.git/objects/pack')) or not re.fullmatch('[0-9a-f]{40}', item['revision'])
                             or not item['repository'].startswith('https://github.com/')
                             or not item['branch']):
            raise ValueError('Upstream Git exclusion requires a pinned public repository and branch')
        if 'tags_sha256' in item and not re.fullmatch('[0-9a-f]{64}', item['tags_sha256']):
            raise ValueError('Record the published upstream tag identities')
        if item['kind'] == 'package-cache' and (not name.endswith('/cache/uv')
                                               or not path_name(item['recipe'])):
            raise ValueError('Package cache must identify the uv download cache and retained recipe')
        if not any(beneath(name, p) for p in includes):
            raise ValueError('Exclusion is outside the included roots')
        cleaned.append(dict(item))
    if len({e['path'] for e in cleaned}) != len(cleaned):
        raise ValueError('Duplicate exclusions')
    policy = {'schema': 1, 'include': includes,
              'exclude': sorted(cleaned, key=lambda e: e['path']), 'required': required}
    if any(not selected(p, False, policy) for p in required):
        raise ValueError('Required roots must be selected')
    if any(not selected(e['recipe'], False, policy) for e in cleaned if e['kind'] == 'package-cache'):
        raise ValueError('Keep package recovery recipes in the backup')
    external = value.get('external_links', [])
    if not isinstance(external, list):
        raise ValueError('Declare external links individually')
    for item in external:
        if not isinstance(item, dict) or set(item) != {'path', 'target', 'reason'} \
                or not isinstance(item['reason'], str) or not item['reason'].strip():
            raise ValueError('External links require a literal path, target and scope reason')
        path_name(item['path'])
        path_name(item['target'])
        if not selected(item['path'], False, policy) or selected(item['target'], False, policy):
            raise ValueError('External link declarations must retain the link and omit its target')
    if len({item['path'] for item in external}) != len(external):
        raise ValueError('Duplicate external link declaration')
    # Keep existing normalized policies and archive checksums unchanged.
    if external:
        policy['external_links'] = sorted((dict(item) for item in external), key=lambda item: item['path'])
    return policy


def selected(name, directory, policy):
    if any(beneath(name, e['path']) for e in policy['exclude']):
        return False
    return any(beneath(name, p) or directory and beneath(p, name) for p in policy['include'])


def checksum(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def repositories(root):
    """Reject excluded repositories, including clean branches and bare repos."""
    found = []
    if not root.is_dir() or root.is_symlink():
        return found
    def fail(error):
        raise error
    for current, directories, files in os.walk(root, followlinks=False, onerror=fail):
        path = Path(current)
        marker = path / '.git'
        git_file = marker.is_file() and marker.stat().st_size < 4096 \
            and marker.read_text(errors='replace').startswith('gitdir:')
        if git_file or marker.is_dir() and (marker / 'HEAD').is_file() \
                or 'HEAD' in files and 'objects' in directories:
            found.append(path)
            directories[:] = []
        else:
            directories[:] = [name for name in directories if name not in {'.git', '.snap'}]
    return found


def published_upstream(home, exclusion):
    """Omit remote history only when there is no local Git work to recover."""
    packed = exclusion['path'].endswith('/.git/objects/pack')
    repo = home / (PurePosixPath(exclusion['path']).parents[2] if packed else PurePosixPath(exclusion['path']).parent)
    if not (repo / '.git').is_dir() or (repo / '.git').is_symlink():
        return False
    command = ['git', '--no-optional-locks', '-C', str(repo)]
    def output(*args):
        return subprocess.check_output([*command, *args], stderr=subprocess.DEVNULL, timeout=60).decode().strip()
    try:
        expected = exclusion['revision']
        refs = output('for-each-ref', '--format=%(refname) %(objectname)').splitlines()
        local = [line for line in refs if line.startswith('refs/heads/') or not packed and not line.startswith('refs/remotes/')]
        tags_ok = not packed or hashlib.sha256(subprocess.check_output(
            [*command, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/tags'], timeout=60)).hexdigest() == exclusion['tags_sha256']
        if packed:
            if not tags_ok or output('remote', 'get-url', 'origin').removesuffix('.git') != exclusion['repository'].removesuffix('.git'):
                return False
            tags = output('for-each-ref', '--format=%(objectname)', 'refs/tags').splitlines()
            # Refs, index, reflogs and loose objects are archived unchanged.
            # Every object outside the declared published history must be
            # loose and selected; a local object packed by gc blocks omission.
            delta = output('rev-list', '--objects', '--all', '--reflog', '--not', expected, *tags)
            return all((repo / '.git/objects' / sha[:2] / sha[2:]).is_file()
                       for line in delta.splitlines() for sha in [line.split()[0]])
        return output('rev-parse', 'HEAD') == expected \
            and output('branch', '--show-current') == exclusion['branch'] \
            and output('remote', 'get-url', 'origin').removesuffix('.git') == exclusion['repository'].removesuffix('.git') \
            and local == [f"refs/heads/{exclusion['branch']} {expected}"] \
            and tags_ok \
            and output('rev-list', '--count', '--reflog', '--all', '--not', '--remotes') == '0' \
            and subprocess.call([*command, 'diff', '--quiet'], stderr=subprocess.DEVNULL) == 0 \
            and subprocess.call([*command, 'diff', '--cached', '--quiet'], stderr=subprocess.DEVNULL) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def review(home, entries, policy):
    missing = [p for p in policy['required'] if p not in entries]
    roots = [p for p in policy['include'] if p == '.' or p in entries]
    links, rebuildable_links, excluded_repositories, unpublished_upstream, missing_recipes = [], [], [], [], []
    external = {item['path']: item for item in policy.get('external_links', [])}
    external_targets = []
    for name, details in entries.items():
        declaration = external.get(name)
        if declaration is not None:
            try:
                matches = stat.S_ISLNK(details.st_mode) \
                    and (home / name).resolve() == (home / declaration['target']).resolve()
            except (OSError, RuntimeError):
                matches = False
            if not matches:
                links.append({'path': name, 'target': None})
            else:
                external_targets.append(dict(declaration))
            continue
        if not stat.S_ISLNK(details.st_mode):
            continue
        try:
            target = (home / name).resolve()
            if target != home and target.is_relative_to(home) and target.exists() \
                    and target.relative_to(home).as_posix() not in entries:
                target_name = target.relative_to(home).as_posix()
                destination = rebuildable_links if any(e['kind'] in {'rebuildable', 'package-cache'} and beneath(target_name, e['path'])
                                                       for e in policy['exclude']) else links
                destination.append({'path': name, 'target': target_name})
        except (OSError, RuntimeError):
            links.append({'path': name, 'target': None})
    for exclusion in policy['exclude']:
        if exclusion['kind'] == 'package-cache' and exclusion['recipe'] not in entries:
            missing_recipes.append(exclusion['recipe'])
        if exclusion['kind'] == 'upstream-git' and not published_upstream(home, exclusion):
            unpublished_upstream.append(exclusion['path'])
        if exclusion['kind'] == 'rebuildable':
            # The snapshot never descends through a link; neither should this audit.
            if any((home / parent).is_symlink() for parent in PurePosixPath(exclusion['path']).parents
                   if str(parent) != '.'):
                continue
            excluded_repositories.extend(p.relative_to(home).as_posix()
                                         for p in repositories(home / exclusion['path']))
    return {'ready': bool(roots) and not missing and not links and not excluded_repositories and not unpublished_upstream and not missing_recipes,
            'selected_roots': roots, 'missing_required': missing,
            'unselected_link_targets': links, 'excluded_repositories': excluded_repositories,
            'upstream_git_needs_preservation': unpublished_upstream,
            'missing_rebuild_recipes': missing_recipes,
            'external_link_targets': external_targets,
            'rebuildable_link_targets': rebuildable_links,
            'files': sum(stat.S_ISREG(s.st_mode) for s in entries.values()),
            'bytes': sum(s.st_size for s in entries.values() if stat.S_ISREG(s.st_mode)),
            'selection_sha256': checksum(policy)}
