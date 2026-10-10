"""Audit the public Git inventory without printing private content or calling models."""
import ast
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
PRIVATE_PATHS={'.env','auth.json','secrets.yaml','rclone.conf','client_secret.json'}
PRIVATE_MARKERS=()
TOKENS=re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|sk-ant-api\d+-[A-Za-z0-9_-]{40,}|-----BEGIN (?:OPENSSH|RSA|EC|PRIVATE) PRIVATE KEY-----')


def audit(root=ROOT, markers=PRIVATE_MARKERS):
    root=Path(root);rows=subprocess.check_output(['git','-C',str(root),'ls-files','--stage','-z'],text=True).split('\0')
    files=0
    for row in rows:
        if not row:continue
        metadata,name=row.split('\t',1);mode,blob,stage=metadata.split();path=Path(name)
        if stage!='0' or mode not in {'100644','100755'}:raise ValueError('Unreviewed Git object: '+name)
        if path.parts[0]=='customizations' or any(p in PRIVATE_PATHS for p in path.parts) or name.startswith(('vm/history/','.local/','.build/')):
            raise ValueError('Personal/generated path in public source: '+name)
        actual=root/path
        if actual.is_symlink():raise ValueError('Source alias: '+name)
        data=actual.read_bytes()
        if len(data)>8*1024*1024:raise ValueError('Unexpected source size: '+name)
        text=data.decode()
        # The audit's own denylist must describe exactly what it rejects.
        if any(m.lower() in text.lower() for m in markers):
            raise ValueError('Personal identifier in public source: '+name)
        if TOKENS.search(text):raise ValueError('Credential-shaped content requires review: '+name)
        if path.suffix=='.py':ast.parse(text,filename=name)
        if path.suffix=='.json':json.loads(text)
        files+=1
    if not files:raise ValueError('Stage public source before reviewing it')
    print(json.dumps({'public_source':'reviewed','files':files,'model_calls':0}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--deny-marker',action='append',default=[])
    args=parser.parse_args();audit(markers=args.deny_marker)
