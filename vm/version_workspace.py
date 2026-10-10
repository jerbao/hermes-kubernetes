"""Reconcile native customizations with Git and publish safe changes without discarding work."""
from __future__ import annotations
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'runtime'), str(ROOT/'scripts'), str(ROOT/'vm')]
from operations_protocol import Client
from project_files import allowed, layout
from versioned_state import atomic, capture, apply, check_content, digest, dump, environment_secrets


class PendingRuntime(ValueError):
    pass


class SocketRuntime:
    def __init__(self, root, socket, bundle=None):
        self.root, self.bundle, self.client = root, Path(bundle or root), Client(str(socket), timeout=300)
    def run(self, operation, changes=None):
        (self.root/'.local/versioning').mkdir(mode=0o700,parents=True,exist_ok=True)
        source='import sys\nsys.path.insert(0,\"/opt/hermes-personal\")\n'+(self.bundle/'runtime/versioned_state.py').read_text().replace('from __future__ import annotations\n','')
        request = None
        if operation == 'capture':
            changes={name:item['sha256'] for name,item in projection(self.root).items()}
        if changes is not None:
            # The transfer protocol deliberately does not create destination parents.
            # Bootstrap only this private directory through the authenticated socket.
            out, err = io.BytesIO(), io.BytesIO()
            bootstrap = self.client.execute(['/opt/hermes/.venv/bin/python','-c',
                'import os; from pathlib import Path; p=Path(os.environ["HERMES_HOME"])/"state/versioning"; '
                'assert not any(x.is_symlink() for x in [p,*p.parents]); p.mkdir(parents=True,exist_ok=True,mode=0o700)'],out,err)
            if bootstrap['returncode'] or bootstrap['timed_out']:
                raise RuntimeError('Cannot initialize private versioning directory')
            # Native state snapshots already exclude *.tmp. A short-lived
            # transport payload must not race the durable-state backup walker.
            request = 'state/versioning/request-' + uuid.uuid4().hex + '.tmp'
            with tempfile.NamedTemporaryFile(dir=self.root/'.local/versioning') as pending:
                pending.write(dump(changes));pending.flush()
                self.client.upload(pending.name, request, expected='absent')
        expression = 'capture(Path(os.environ["HERMES_HOME"]), known=json.loads((Path(os.environ["HERMES_HOME"])/'+repr(request)+').read_text()))' if operation=='capture' else 'apply(Path(os.environ["HERMES_HOME"]), json.loads((Path(os.environ["HERMES_HOME"])/'+repr(request)+').read_text()))'
        code=source+'\ntry:\n print(json.dumps('+expression+'))\nfinally:\n '+('(Path(os.environ["HERMES_HOME"])/'+repr(request)+').unlink(missing_ok=True)' if request else 'pass')+'\n'
        # Execute the versioned exporter on the active PVC via the existing private socket.
        # Avoid the CLI main branch in the embedded module.
        code=code.replace("if __name__=='__main__':main()",'')
        stdout, stderr=io.BytesIO(), io.BytesIO()
        result=self.client.execute(['/opt/hermes/.venv/bin/python','-c',code],stdout,stderr,timeout=240)
        if result['returncode'] or result['timed_out']:
            private=self.root/'.local/versioning/runtime-error.log'
            atomic(private,stderr.getvalue())
            raise RuntimeError('Native projection failed; private diagnostics in '+str(private))
        return json.loads(stdout.getvalue())
    def capture(self):
        from upstream_source import inputs_digest
        from skill_updates import distribution_digest
        provenance = self.client.request('info').get('provenance') or {}
        expected_distribution = distribution_digest(self.bundle/'skills',layout(self.bundle)['skills'])
        if (provenance.get('source_inputs_sha256') != inputs_digest(self.bundle) or
                provenance.get('distribution_skills_sha256') != expected_distribution):
            raise PendingRuntime('Waiting for the image matching managed skill sources')
        return self.run('capture')
    def apply(self,changes):return self.run('apply',changes)


class LocalRuntime:
    """Same reconciliation logic for offline restoration and isolated tests."""
    def __init__(self, home, source=Path('/opt/hermes')):self.home,self.source=home,source
    def capture(self):return capture(self.home,self.source)
    def apply(self,changes):return apply(self.home,changes)


def projection(root):
    result={}
    directory=root/'customizations'
    if directory.is_symlink():raise ValueError('Customization directory cannot be a symlink')
    if directory.is_dir() and (root/'.git').exists():
        return git_projection(root)
    if directory.is_dir():
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():raise ValueError('Customization files cannot be symlinks')
            if path.is_file():
                data=path.read_bytes();check_content(data)
                result[path.relative_to(root).as_posix()]={'data':base64.b64encode(data).decode(),'sha256':digest(data),'mode':0o700 if path.stat().st_mode&0o111 else 0o600}
    return result


def git_projection(root):
    """Git's parallel stat refresh detects edits; cache validated blob contents.

    Clean paths are identified by index blob, rather than by HEAD alone, so
    fast-forwards, staged edits, deletes and new files are all observed.
    """
    cache_path=root/'.local/versioning/projection-cache.json.gz'
    try:previous=json.loads(gzip.decompress(cache_path.read_bytes()))
    except (OSError,ValueError,EOFError):previous={}
    if not isinstance(previous,dict):previous={}
    indexed={}
    for entry in git(root,'ls-files','--stage','-z','--','customizations').split('\0'):
        if not entry:continue
        metadata,name=entry.split('\t',1);mode,blob,stage=metadata.split()
        if stage!='0':raise ValueError('Resolve Git index conflicts before projecting customizations')
        if mode not in {'100644','100755'}:raise ValueError('Customization index contains a symlink or submodule')
        indexed[name]=(mode,blob)
    for entry in git(root,'ls-files','-v','-z','--','customizations').split('\0'):
        if entry and (entry[0].islower() or entry[0]=='S'):
            raise ValueError('Customizations cannot use assume-unchanged or skip-worktree')
    changed=set(git(root,'diff-files','--name-only','-z','--','customizations').split('\0'))
    untracked=set(git(root,'ls-files','--others','--exclude-standard','-z','--','customizations').split('\0'))-{''}
    result,cache,read={}, {}, []
    def blob_id(data, expected=''):
        content=b'blob '+str(len(data)).encode()+b'\0'+data
        hash_function=hashlib.sha256 if len(expected)==64 else hashlib.sha1
        return hash_function(content).hexdigest()
    for name in sorted(set(indexed)|untracked):
        old=previous.get(name)
        valid=False
        if name not in changed and name in indexed and isinstance(old,dict):
            try:
                data=base64.b64decode(old['item']['data'],validate=True);check_content(data)
                valid=old['blob']==indexed[name][1]==blob_id(data,indexed[name][1]) and old['mode']==indexed[name][0] and old['item']['sha256']==digest(data) and old['item']['mode']==(0o700 if indexed[name][0]=='100755' else 0o600)
            except (ValueError,KeyError,TypeError):pass
        if valid:
            result[name]=old['item'];cache[name]=old
        else:read.append(name)
    def load(name):
        path=root/name
        if any(p.is_symlink() for p in [path,*path.parents] if p!=root.parent and p.is_relative_to(root)):
            raise ValueError('Symlink in customization source')
        if not path.is_file():return name,None
        data=path.read_bytes();check_content(data)
        executable=bool(path.stat().st_mode&0o111)
        item={'data':base64.b64encode(data).decode(),'sha256':digest(data),'mode':0o700 if executable else 0o600}
        return name,{'blob':blob_id(data,indexed.get(name,('', ''))[1]),'mode':'100755' if executable else '100644','item':item}
    with ThreadPoolExecutor(max_workers=8) as executor:
        for name,entry in executor.map(load,read):
            if entry is not None:cache[name]=entry;result[name]=entry['item']
    if cache!=previous:atomic(cache_path,gzip.compress(dump(cache),compresslevel=3))
    return result


def reconcile(root, runtime):
    state=root/'.local/versioning/baseline.json'
    previous=json.loads(state.read_text()) if state.is_file() else {}
    try:
        remote=runtime.capture()
    except PendingRuntime:
        return {'status':'waiting-for-runtime','profiles':{}}
    local=projection(root)
    baseline, changes, copies, conflicts={}, {}, {}, []
    inventories=[]
    for name in sorted(set(previous)|set(local)|set(remote['files'])):
        old=previous.get(name)
        ours=local.get(name,{}).get('sha256')
        theirs=remote['files'].get(name,{}).get('sha256')
        if ours==theirs:
            baseline[name]=ours
        elif ours==old:
            copies[name]=remote['files'].get(name)
            baseline[name]=theirs
        elif theirs==old:
            changes[name]={'expected':theirs,**local.get(name,{'data':None})}
            baseline[name]=ours
        else:
            if name.endswith('/installations.json') and name in local and name in remote['files']:
                inventories.append(name)
            else:conflicts.append(name)
    # Installation inventories describe actual installed sources. Rebuilding
    # them after a release is not a competing plugin/link configuration edit.
    derived={'upstream_skills','distribution_skills','upstream_revision','source_mode'}
    for name in inventories:
        ours=json.loads(base64.b64decode(local[name]['data']))
        theirs=json.loads(base64.b64decode(remote['files'][name]['data']))
        if {k:v for k,v in ours.items() if k not in derived}!={k:v for k,v in theirs.items() if k not in derived}:
            conflicts.append(name);continue
        desired=json.loads(json.dumps(theirs))
        prefix=name.removesuffix('installations.json')
        for path,change in list(changes.items()):
            if path.startswith(prefix+'skills/') and change.get('data') is None:
                relative=path.removeprefix(prefix)
                for field in ['upstream_skills','distribution_skills']:
                    if relative in ours.get(field,[]):
                        desired[field]=sorted(set(desired.get(field,[]))|{relative})
        data=dump(desired);checksum=digest(data)
        item={'data':base64.b64encode(data).decode(),'sha256':checksum,'mode':0o600}
        copies[name]=item;baseline[name]=checksum
        if desired!=theirs:
            changes[name]={'expected':remote['files'][name]['sha256'],**item}
    # A deleted override can mean "use the managed template". Carry that
    # intent even when another inventory field was refreshed from the runtime.
    for path,change in list(changes.items()):
        parts=Path(path).parts
        if len(parts)<4 or parts[2]!='skills' or change.get('data') is not None:continue
        name='/'.join(parts[:2])+'/installations.json'
        if name not in local or name not in remote['files']:continue
        ours=json.loads(base64.b64decode(local[name]['data']))
        item=changes.get(name) or copies.get(name) or remote['files'][name]
        if not item.get('data'):item=local[name]
        desired=json.loads(base64.b64decode(item['data']))
        relative='/'.join(parts[2:])
        for field in ['upstream_skills','distribution_skills']:
            if relative in ours.get(field,[]):desired[field]=sorted(set(desired.get(field,[]))|{relative})
        data=dump(desired);checksum=digest(data)
        if checksum!=remote['files'][name]['sha256']:
            item={'data':base64.b64encode(data).decode(),'sha256':checksum,'mode':0o600}
            changes[name]={'expected':remote['files'][name]['sha256'],**item}
            copies[name]=item;baseline[name]=checksum
    if conflicts:
        atomic(root/'.local/versioning/conflicts.json',dump({'paths':conflicts}))
        return {'status':'conflict','conflicts':conflicts,'profiles':remote['profiles']}
    # Runtime CAS must succeed before changing the Git projection/baseline.
    applied=runtime.apply(changes) if changes else None
    for name, item in copies.items():
        path=root/name
        if item is None:path.unlink(missing_ok=True)
        else:atomic(path,base64.b64decode(item['data']),item['mode'])
    atomic(state,dump({k:v for k,v in baseline.items() if v is not None}))
    (root/'.local/versioning/conflicts.json').unlink(missing_ok=True)
    result={'status':'synchronized','from_runtime':len(copies),'to_runtime':len(changes),'profiles':remote['profiles']}
    if applied:
        result['runtime_apply']=applied
        if applied.get('restart_required'):atomic(root/'.local/versioning/reload-required.json',dump(applied))
    return result


def git(root,*args,check=True):
    try:
        result=subprocess.run(['git','-C',str(root),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                              timeout=600 if args[0] in {'add','commit','merge'} else 90)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Git operation timed out: '+args[0]+'; preserve the index and inspect the process before retrying') from None
    if check and result.returncode:raise RuntimeError('Git operation failed: '+args[0])
    return result.stdout.strip() if check else result


def publish(root, *, quiet_seconds=120, now=None, code_only=False):
    if git(root,'branch','--show-current')!='main':return {'status':'development-branch'}
    if git(root,'ls-files','--unmerged'):
        return {'status':'git-conflict','paths':git(root,'diff','--name-only','--diff-filter=U').splitlines()}
    value=layout(root)
    # Include tracked edits/deletions and eligible new source; never Git-add the home wholesale.
    names=set(git(root,'diff','--name-only','HEAD','-z').split('\0'))
    names.update(git(root,'ls-files','--others','--exclude-standard','-z').split('\0'))
    names.discard('')
    if code_only and any(name.startswith('customizations/') for name in names):
        return {'status':'waiting-for-runtime','files':len(names)}
    if any(not allowed(n,value) for n in names):raise ValueError('Unclassified project change')
    secrets=environment_secrets(root)
    hashes={}
    def read_source(name):
        path=root/name
        if path.is_symlink():raise ValueError('Source symlink cannot be published')
        if path.is_file():
            data=path.read_bytes();check_content(data,secrets);return name,digest(data)
        return name,None
    with ThreadPoolExecutor(max_workers=8) as executor:
        hashes.update(executor.map(read_source,sorted(names)))
    now=time.time() if now is None else now
    signature=digest(dump(hashes))
    marker=root/'.local/versioning/pending.json'
    previous=json.loads(marker.read_text()) if marker.is_file() else {}
    if names and previous.get('signature')!=signature:
        previous={'signature':signature,'since':now};atomic(marker,dump(previous))
    if names and now-previous['since']<quiet_seconds:
        return {'status':'waiting-for-stable-files','files':len(names)}
    git(root,'fetch','origin','main')
    head,remote=git(root,'rev-parse','HEAD'),git(root,'rev-parse','FETCH_HEAD')
    incoming = head!=remote and git(root,'merge-base','--is-ancestor',remote,head,check=False).returncode
    if incoming:
        from sync_workspace import review_remote
        review=review_remote(root,head,remote)
        if review:return {'status':review,'files':len(names)}
    if names:
        git(root,'add','--',*sorted(names))
        git(root,'commit','-m','Sincroniza código e personalizações do Hermes')
    if incoming:
        merged=git(root,'merge','--no-edit',remote,check=False)
        if merged.returncode:
            # Keep the local commit, incoming revision and index stages for human review.
            return {'status':'git-conflict','paths':git(root,'diff','--name-only','--diff-filter=U').splitlines()}
    if git(root,'rev-parse','HEAD')!=remote:
        # Push rejection preserves the local commit for a later review, never force pushes.
        result=git(root,'push','origin','HEAD:main',check=False)
        if result.returncode:return {'status':'push-pending','files':len(names)}
        marker.unlink(missing_ok=True)
        return {'status':'published','files':len(names),'revision':git(root,'rev-parse','HEAD')}
    return {'status':'current','files':0}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--socket',type=Path)
    p.add_argument('--offline-home',type=Path)
    p.add_argument('--publish',action='store_true')
    p.add_argument('--quiet-seconds',type=int,default=120)
    a=p.parse_args();root=a.root.resolve()
    from workspace_guard import guard
    with guard(root/'state/workspace-sync.lock',create=True,blocking=False):
        backend=LocalRuntime(a.offline_home) if a.offline_home else SocketRuntime(root,a.socket or root/'.local/runtime/control.sock')
        result=reconcile(root,backend)
        if a.publish and result['status']=='synchronized':result['git']=publish(root,quiet_seconds=a.quiet_seconds)
        atomic(root/'.local/versioning/status.json',dump(result));print(json.dumps(result))
    return 0 if result['status']=='synchronized' else 2
if __name__=='__main__':raise SystemExit(main())
