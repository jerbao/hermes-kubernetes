"""Reconcile a candidate's managed skills against each Git-versioned profile."""
import argparse
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'runtime'),str(ROOT/'scripts')]
from check_official_skill_patches import git, replay
from skill_updates import content, group, plan, snapshot
from versioned_state import CREDENTIAL, atomic, check_content, dump, environment_secrets


def tree_snapshot(source, tree):
    archive = git(source,'archive',tree,'skills')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as handle:
        members = [m for m in handle.getmembers() if m.isfile()]
        roots = [str(Path(m.name).parent) for m in members if Path(m.name).name == 'SKILL.md']
        for member in members:
            if any(Path(member.name).is_relative_to(root) for root in roots):
                data = handle.extractfile(member).read()
                files[member.name] = {'data':base64.b64encode(data).decode(),
                                     'mode':0o700 if member.mode & 0o111 else 0o600}
    return files


def git_distribution(root, revision):
    contract = json.loads(git(root,'show',revision+':project-files.json'))
    names = git(root,'ls-tree','-r',revision,'--','skills').decode().splitlines()
    result = {}
    for row in names:
        metadata,name = row.split('\t',1)
        if len(Path(name).parts) < 3 or Path(name).parts[1] not in contract['skills']:
            continue
        data = git(root,'show',revision+':'+name)
        result[name] = {'data':base64.b64encode(data).decode(),'mode':0o700 if metadata.startswith('100755') else 0o600}
    return result


def reconcile(root, source, base_revision, write=False):
    root, source = Path(root).resolve(),Path(source).resolve()
    previous_lock = json.loads(git(root,'show',base_revision+':upstream.lock.json'))
    if git(source,'cat-file','-e',previous_lock['revision']+'^{commit}',missing=True) is None:
        git(source,'fetch','--depth','1',previous_lock['repository'],previous_lock['revision'])
    old_official = tree_snapshot(source,replay(root,source,base_revision))
    official = tree_snapshot(source,replay(root,source))
    old_distribution = git_distribution(root,base_revision)
    contract = json.loads((root/'project-files.json').read_text())
    distribution = snapshot(root/'skills',names=contract['skills'])
    previous, desired = {**old_official,**old_distribution},{**official,**distribution}
    changes, conflicts, expected = {}, [], {}
    secrets = environment_secrets(root)
    public_examples = {m.group() for item in [*old_official.values(),*official.values()]
                       for m in CREDENTIAL.finditer(content(item).decode(errors='ignore'))}
    profiles = sorted(p for p in (root/'customizations').iterdir() if p.is_dir() and not p.is_symlink()) if (root/'customizations').is_dir() else []
    for profile in profiles:
        recipe_path = profile/'installations.json'
        recipe = json.loads(recipe_path.read_text()) if recipe_path.is_file() else {}
        current = {}
        for name in [*recipe.get('upstream_skills',[]),*recipe.get('distribution_skills',[])]:
            if name in previous: current[name] = previous[name]
        projected = snapshot(profile/'skills')
        # Older projections explicitly carry installed distribution copies.
        if not recipe:
            installed_groups = {group(name,previous) for name in projected}
            current.update({n:v for n,v in previous.items() if group(n,previous) in installed_groups-{None}})
        current.update(projected)
        result = plan(previous,desired,current,distribution,new_profile=not recipe)
        conflicts += [profile.name+'/'+name for name in result['conflicts']]
        for name, change in result['changes'].items():
            if change['value'] is None: current.pop(name,None)
            else: current[name] = change['value']
        if not result['ready']: continue
        for name in set(projected)|set(result['changes']):
            path = profile/name
            value = current.get(name)
            # Identical managed sources are reconstructed from the candidate image.
            data = content(value) if value != desired.get(name) else None
            if data is not None:
                for example in public_examples: data = data.replace(example.encode(),b'<CREDENTIAL>')
                check_content(data,secrets)
            before = path.read_bytes() if path.is_file() else None
            if before != data:
                changes[path] = (data,value['mode'] if value else 0o600)
                expected[path] = before
        if recipe:
            recipe['upstream_skills'] = sorted(n for n,v in current.items() if v == official.get(n))
            recipe['distribution_skills'] = sorted(n for n,v in current.items() if v == distribution.get(n))
            recipe['upstream_revision'] = json.loads((root/'upstream.lock.json').read_text())['revision']
            data = dump(recipe)
            before = recipe_path.read_bytes()
            if data != before:
                changes[recipe_path] = (data,0o600)
                expected[recipe_path] = before
    report = {'ready':not conflicts,'conflicts':conflicts,'profiles':len(profiles),
              'planned_files':len(changes),'written_files':0,'model_calls':0}
    if write and not conflicts:
        for path, before in expected.items():
            if any(p.is_symlink() for p in [path,*path.parents]) or (path.read_bytes() if path.exists() else None) != before:
                raise ValueError('Concurrent or aliased Git skill edit')
        for path, (data,mode) in changes.items():
            if data is None: path.unlink(missing_ok=True)
            else: atomic(path,data,mode)
        report['written_files'] = len(changes)
    return report


LIVE = r'''
import json, sys, types
from pathlib import Path
sys.path.insert(0,'/opt/hermes-personal')
options = json.load(sys.stdin)
module = types.ModuleType('skill_updates')
exec(compile(options['module'],'skill_updates.py','exec'),module.__dict__)
from operations_protocol import Client
info = Client('/tmp/hermes-operations/control.sock').request('info')
if options.get('inspect'):
 import gzip, hashlib
 state = Path(info['roots']['state'])/'state/versioning/managed-skills/bases.json.gz'
 baseline = json.loads(gzip.decompress(state.read_bytes()))
 report = {'ready':True,'profiles':baseline['profiles'],'source_identity':baseline['source_identity'],
           'matches_project_base':baseline['distribution']==options['distribution_baseline'],
           'distribution_sha256':hashlib.sha256(json.dumps(baseline['distribution'],sort_keys=True,separators=(',',':')).encode()).hexdigest()}
else:
 report = module.installed_update(Path(info['roots']['state']),Path(info['roots']['source']),
                                 Path(info['roots']['integration']),seed_only=options['seed'],
                                 distribution_baseline=options.get('distribution_baseline'))
report['instance'] = info['instance']
print(json.dumps(report))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'hermes-agent')
    parser.add_argument('--base-revision',default='HEAD')
    parser.add_argument('--write',action='store_true')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--kubeconfig',type=Path)
    parser.add_argument('--namespace',default='hermes')
    parser.add_argument('--seed-live',action='store_true')
    parser.add_argument('--inspect-live',action='store_true',help='Read baseline identity without modifying installed files')
    parser.add_argument('--require-current',action='store_true',help='Fail if the Git projection still needs reconciliation')
    args = parser.parse_args()
    if (args.seed_live or args.inspect_live) and not args.kubeconfig: parser.error('Live operations require --kubeconfig')
    if args.seed_live and args.inspect_live: parser.error('Choose one live operation')
    if args.kubeconfig:
        result = subprocess.run(['kubectl','--kubeconfig',str(args.kubeconfig),'-n',args.namespace,
                                 'exec','-i','deployment/hermes','-c','operations','--',
                                 '/opt/hermes/.venv/bin/python','-c',LIVE],
                                input=json.dumps({'module':(ROOT/'runtime/skill_updates.py').read_text(),'seed':args.seed_live,'inspect':args.inspect_live,
                                                  'distribution_baseline':git_distribution(ROOT,args.base_revision) if args.seed_live or args.inspect_live else None}),
                                text=True,timeout=360)
        return result.returncode
    report = reconcile(ROOT,args.source,args.base_revision,args.write)
    report['projection_current'] = report['ready'] and (args.write or report['planned_files'] == 0)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        atomic(args.output,dump(report))
    print(json.dumps(report))
    return 0 if report['ready'] and (not args.require_current or report['projection_current']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
