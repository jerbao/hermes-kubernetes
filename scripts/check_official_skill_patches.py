"""Replay locked skill patches and optionally publish installed copies with CAS."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

GROUP = 'patches/0005-managed-skills.patch'


def git(root, *args, env=None, data=None, missing=False):
    result = subprocess.run(['git', '-C', str(root), *args], env=env,
                            input=data, capture_output=True, timeout=60)
    if result.returncode and not missing:
        raise ValueError('Git operation failed: ' + result.stderr.decode())
    return None if result.returncode else result.stdout


def replay(root, source, revision=None):
    def read(name):
        return git(root, 'show', revision + ':' + name) if revision else (root/name).read_bytes()
    lock = json.loads(read('upstream.lock.json'))
    with tempfile.TemporaryDirectory(prefix='hermes-skill-index-') as temporary:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(temporary)/'index')}
        git(source, 'read-tree', lock['revision'], env=env)
        for patch in lock['patches']:
            data = read(patch['path'])
            if hashlib.sha256(data).hexdigest() != patch['sha256']:
                raise ValueError('Invalid locked patch checksum: ' + patch['path'])
            git(source, 'apply', '--cached', '-', env=env, data=data)
        return git(source, 'write-tree', env=env).decode().strip()


REMOTE = r'''
import hashlib, json, os, subprocess, tempfile
from pathlib import Path
import sys
sys.path.insert(0, '/opt/hermes-personal')
from operations_protocol import Client, OperationError
from versioned_state import CREDENTIAL, check_content
options = json.load(sys.stdin)
client = Client('/tmp/hermes-operations/control.sock', timeout=180)
info = client.request('info')
root = Path(info['roots']['state'])
profiles = [('default', root)]
if (root/'profiles').is_dir():
    profiles += [(p.name, p) for p in sorted((root/'profiles').iterdir())
                 if p.is_dir() and not p.is_symlink() and not p.name.startswith('.')
                 and (p/'config.yaml').is_file()]
changes, checked, dependencies, merged = [], [], [], {}
def plan(path, data, baselines, create=False, bases=(), projection=None):
    wanted = hashlib.sha256(data.encode()).hexdigest()
    try:
        actual = client.request('stat', scope='state', path=path, sha256=True)['sha256']
    except OperationError as error:
        if str(error) != 'FileNotFoundError': raise
        if not create: return False
        actual = 'absent'
    if actual == wanted:
        checked.append((path, wanted))
        return True
    if not options['publish']: raise ValueError('Installed skill differs: '+path)
    if actual not in baselines:
        if not bases: raise ValueError('Preserve concurrent or unknown skill edit: '+path)
        with tempfile.TemporaryDirectory(prefix='hermes-skill-merge-') as temporary:
            temporary = Path(temporary)
            current = temporary/'current'
            client.download(current, path=path, scope='state')
            if hashlib.sha256(current.read_bytes()).hexdigest() != actual:
                raise ValueError('Skill changed during reconciliation: '+path)
            target, base = temporary/'target', temporary/'base'
            target.write_text(data)
            candidates = set()
            for previous in bases:
                base.write_text(previous)
                result = subprocess.run(['git','merge-file','-p',str(target),str(base),str(current)],
                                        capture_output=True, timeout=30)
                if result.returncode == 0: candidates.add(result.stdout.decode())
            if len(candidates) != 1:
                raise ValueError('Preserve conflicting or ambiguous skill edit: '+path)
            data = candidates.pop()
            # Public upstream examples can look like real credentials. Keep
            # imported documentation Git-safe without accepting unknown tokens.
            for example in options['upstream_examples']:
                data = data.replace(example, '<CREDENTIAL>')
            check_content(data.encode())
            wanted = hashlib.sha256(data.encode()).hexdigest()
            merged[projection] = data
    checked.append((path, wanted))
    changes.append((path, data, actual))
    return True
for name, home in profiles:
    prefix = '' if name == 'default' else 'profiles/'+name+'/'
    matched = False
    for relative, native in options['native'].items():
        projected = options['projected'].get(name, {}).get(relative)
        entry = projected or native
        baselines = set(native['baselines']) | set(entry['baselines'])
        matched = plan(prefix+relative, entry['data'], baselines,
                       bases=entry['bases'], projection='customizations/'+name+'/'+relative) or matched
    for relative, entry in options['projected'].get(name, {}).items():
        if relative not in options['native']:
            matched = plan(prefix+relative, entry['data'], entry['baselines'],
                           bases=entry['bases'], projection='customizations/'+name+'/'+relative) or matched
    if matched:
        dependencies.append(name)
        for relative, entry in options['companion'].items():
            plan(prefix+'skills/hermes-self-management/'+relative,
                 entry['data'], entry['baselines']+['absent'], create=True)
# Review every old hash before the first write; each upload also uses CAS.
for path, data, actual in changes:
    parent = root
    for part in Path(path).parts[:-1]:
        parent = parent/part
        if parent.is_symlink(): raise ValueError('Preserve skill symlink: '+path)
        parent.mkdir(exist_ok=True)
for path, data, actual in changes:
    with tempfile.NamedTemporaryFile() as temporary:
        temporary.write(data.encode()); temporary.flush()
        client.upload(temporary.name, path, expected=actual)
for path, wanted in checked:
    if client.request('stat', scope='state', path=path, sha256=True)['sha256'] != wanted:
        raise ValueError('Publication verification failed: '+path)
print(json.dumps({'passed':True, 'instance':info['instance'], 'checked_files':len(checked),
    'updated_files':len(changes), 'profiles':dependencies, 'model_calls':0,
    'plugin_installations':0, 'merged_projections':merged}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--base-revision', help='Project revision before these skill edits')
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--namespace', default='hermes')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if args.publish and (not args.kubeconfig or not args.base_revision):
        parser.error('--publish requires --kubeconfig and --base-revision')
    root = Path(__file__).resolve().parents[1]
    source = (args.source or root/'hermes-agent').resolve()
    group = next(g for g in json.loads((root/'patches/series.json').read_text())['groups']
                 if g['path'] == GROUP)
    tree = replay(root, source)
    old_tree = replay(root, source, args.base_revision) if args.base_revision else tree
    payload = {'publish':args.publish, 'native':{}, 'projected':{}, 'companion':{}}
    sys.path.insert(0, str(root/'runtime'))
    from versioned_state import CREDENTIAL
    payload['upstream_examples'] = []
    def entry(data, previous):
        return {'data':data.decode(), 'baselines':[hashlib.sha256(previous).hexdigest()] if previous else [],
                'bases':[previous.decode()] if previous else []}
    for name in group['files']:
        data = git(source, 'show', tree+':'+name)
        if data != (source/name).read_bytes():
            raise ValueError('Source differs from replayed patch: '+name)
        payload['native'][name] = entry(data, git(source, 'show', old_tree+':'+name))
        payload['upstream_examples'].extend(match.group() for match in CREDENTIAL.finditer(data.decode()))
    for profile in ((root/'customizations').iterdir() if (root/'customizations').is_dir() else []):
        if not profile.is_dir(): continue
        projection = {}
        for name in [*group['files'], 'skills/autonomous-ai-agents/hermes-agent/references/hermes-plugins.md']:
            path = profile/name
            if not path.is_file(): continue
            previous = git(root, 'show', args.base_revision+':'+path.relative_to(root).as_posix(), missing=True) if args.base_revision else path.read_bytes()
            projection[name] = entry(path.read_bytes(), previous)
        payload['projected'][profile.name] = projection
        # Profile overrides may legitimately differ. The generic skill reconciler
        # now checks their compatibility against the preceding managed source.
    for path in (root/'skills/hermes-self-management').rglob('*'):
        if path.is_file():
            payload['companion'][path.relative_to(root/'skills/hermes-self-management').as_posix()] = entry(path.read_bytes(), path.read_bytes())
    if args.kubeconfig:
        result = subprocess.run(['kubectl','--kubeconfig',str(args.kubeconfig),'-n',args.namespace,
                                 'exec','-i','deployment/hermes','-c','operations','--',
                                 '/opt/hermes/.venv/bin/python','-c',REMOTE],
                                input=json.dumps(payload), text=True, capture_output=True, timeout=360)
        if result.returncode:
            print(result.stderr)
        else:
            report = json.loads(result.stdout)
            merged = report.pop('merged_projections')
            for name, data in merged.items():
                path = root/name
                if not path.is_relative_to(root/'customizations') or '..' in path.parts or path.is_symlink():
                    raise ValueError('Invalid merged projection path')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(data)
            report['merged_files'] = len(merged)
            print(json.dumps(report))
        raise SystemExit(result.returncode)
    print(json.dumps({'passed':True, 'replayed_skill_files':len(group['files']),
                      'profile_projections':len(payload['projected']), 'model_calls':0}))


if __name__ == '__main__':
    main()
