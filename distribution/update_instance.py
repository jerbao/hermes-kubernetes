"""Prepare a private release update atomically; never touch active runtime state."""
import base64
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'runtime'),str(ROOT/'vm')]
from release import dump, resolve, validate
from versioned_state import atomic
from workspace_guard import guard


def dependency(root, manifest):
    spec=importlib.util.spec_from_file_location('instance_bootstrap',ROOT/'examples/instance/hermes.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.checkout(root,manifest)


def reconcile_projection(root, old_bundle, new_bundle):
    from version_workspace import projection
    from reconcile_skills import reconcile
    files=projection(root)
    if not files:return {'ready':True,'profiles':0,'conflicts':[]},{}
    with tempfile.TemporaryDirectory(prefix='hermes-release-review-') as temporary:
        stage=Path(temporary)/'review'
        subprocess.run(['git','clone','-q','--local',str(old_bundle),str(stage)],check=True)
        for name,item in files.items():
            path=stage/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(base64.b64decode(item['data']));path.chmod(item['mode'])
        def git(*args):return subprocess.check_output(['git','-C',str(stage),*args],stderr=subprocess.PIPE,text=True).strip()
        git('config','user.name','Hermes release review');git('config','user.email','review@example.invalid')
        git('add','customizations');git('commit','-qm','Private candidate baseline')
        base=git('rev-parse','HEAD')
        previous=set(git('ls-files','-z').split('\0'))-{''}
        current=set(subprocess.check_output(['git','-C',str(new_bundle),'ls-files','-z'],text=True).split('\0'))-{''}
        for name in previous-current:
            if not name.startswith('customizations/'):(stage/name).unlink(missing_ok=True)
        for name in current:
            source=new_bundle/name;target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        (stage/'customizations').mkdir(exist_ok=True)
        source=stage/'hermes-agent'
        subprocess.run([sys.executable,str(new_bundle/'scripts/prepare_source.py'),'--checkout','--output',str(source)],check=True,cwd=new_bundle,stdout=subprocess.DEVNULL)
        report=reconcile(stage,source,base,write=True)
        if not report['ready']:return report,{}
        after={}
        for path in (stage/'customizations').rglob('*'):
            if path.is_file():after[path.relative_to(stage).as_posix()]=(path.read_bytes(),path.stat().st_mode&0o777)
        changes={}
        for name in files.keys()|after.keys():
            before=base64.b64decode(files[name]['data']) if name in files else None
            value=after.get(name)
            if before != (value[0] if value else None):changes[name]=(before,value)
        return report,changes


def update(root, version='latest', write=True, resolver=resolve, fetch=dependency):
    import yaml
    root=Path(root).resolve();lock_path=root/'platform.lock.json'
    old_bytes=lock_path.read_bytes();old=validate(json.loads(old_bytes))
    new=validate(resolver(old['repository'],version),old['repository'])
    if new==old:return {'status':'current','version':old['version'],'model_calls':0}
    numeric=lambda value:tuple(map(int,value['version'][1:].split('.')))
    if numeric(new)<=numeric(old):raise ValueError('Refusing a downgrade or mutable release')
    old_bundle,new_bundle=fetch(root,old),fetch(root,new)
    report,changes=reconcile_projection(root,old_bundle,new_bundle)
    diagnostics=root/'.local/update-report.json';atomic(diagnostics,dump(report).encode())
    if not report['ready']:raise ValueError('Managed skill conflict; inspect private .local/update-report.json')
    deployment=root/'kustomization.yaml';original=deployment.read_bytes();overlay=yaml.safe_load(original)
    old_resource=f'https://github.com/{old["repository"]}//distribution/kubernetes?ref={old["revision"]}'
    if old_resource not in overlay.get('resources',[]):raise ValueError('Private deployment base requires manual review')
    overlay['resources']=[resource.replace(old_resource,old_resource.replace(old['revision'],new['revision'])) for resource in overlay['resources']]
    aliases={'hermes-personal':'hermes','hermes-mcp':'mcp','hermes-mcp-codegraph':'mcp-codegraph'}
    present={item['name'] for item in overlay.get('images',[])}
    if not set(aliases)<=present:raise ValueError('Private deployment is missing component image pins')
    for image in overlay['images']:
        if image['name'] in aliases:
            image['newName'],image['digest']=new['images'][aliases[image['name']]].split('@');image.pop('newTag',None)
    changes['platform.lock.json']=(old_bytes,(dump(new).encode(),0o600))
    changes['kustomization.yaml']=(original,(yaml.safe_dump(overlay,sort_keys=False).encode(),0o600))
    if write:
        with guard(root/'state/workspace-sync.lock',create=True,blocking=False):
            for name,(before,value) in changes.items():
                path=root/name
                if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('Aliased update destination')
                if (path.read_bytes() if path.exists() else None)!=before:raise ValueError('Concurrent private edit: '+name)
            applied=[]
            try:
                for name,(before,value) in changes.items():
                    path=root/name
                    if value is None:path.unlink(missing_ok=True)
                    else:atomic(path,value[0],value[1])
                    applied.append(name)
            except BaseException:
                for name in reversed(applied):
                    before=changes[name][0];path=root/name
                    if before is None:path.unlink(missing_ok=True)
                    else:atomic(path,before)
                raise
    return {'status':'updated' if write else 'ready','version':new['version'],'files':len(changes),'skill_review':report,'model_calls':0}
