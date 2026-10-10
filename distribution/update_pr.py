"""Open one reviewed candidate PR using the repository's own GitHub token."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def run(*args):
    return subprocess.check_output(args,text=True,stderr=subprocess.PIPE).strip()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--upstream',action='store_true');a=p.parse_args()
    lock='upstream.lock.json' if a.upstream else 'platform.lock.json'
    names=[lock] if a.upstream else [lock,'kustomization.yaml','customizations']
    if not run('git','diff','--name-only','--',*names):return
    value=json.loads(Path(lock).read_text());version=value['release']['tag'] if a.upstream else value['version']
    branch=('automation/hermes-upstream-' if a.upstream else 'automation/hermes-distribution-')+version
    repository=os.environ['GITHUB_REPOSITORY']
    if json.loads(run('gh','pr','list','--repo',repository,'--head',branch,'--state','open','--json','number')):
        print(json.dumps({'status':'candidate-already-open','version':version}));return
    run('git','config','user.name','github-actions[bot]');run('git','config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
    run('git','checkout','-b',branch)
    # Stage only paths present or previously tracked; an empty instance has no customizations yet.
    for name in names:
        if Path(name).exists() or run('git','ls-files','--',name):run('git','add','--',name)
    run('git','commit','-m','chore: review Hermes '+version);run('git','push','origin','HEAD:refs/heads/'+branch)
    with tempfile.TemporaryDirectory(prefix='hermes-pr-') as temporary:
        body=Path(temporary)/'body.md'
        body.write_text('Updates the pinned '+('official upstream' if a.upstream else 'shared distribution')+' to '+version+'.\n\nAutomated candidate checks completed before opening this PR. Personal skill reconciliation must succeed before deployment. This PR does not change active runtime state.\n')
        run('gh','pr','create','--repo',repository,'--base','main','--head',branch,'--title','Update Hermes '+version,'--body-file',str(body))
    print(json.dumps({'status':'pr-opened','version':version}))


if __name__=='__main__':main()
