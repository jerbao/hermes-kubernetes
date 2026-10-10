"""Inspect native PM generations and recover a selection from its recorded lock."""
import argparse
import base64
import json
from pathlib import Path
import shlex
import subprocess

PROBE=r'''
import hashlib,json,os,pathlib,sys
home=pathlib.Path(os.environ['HERMES_HOME']);source=pathlib.Path('/opt/hermes')
from pm.environments import runtime_facts_path,install_state_dir
facts=runtime_facts_path(source);record=json.loads(facts.read_text())['packages']['venv'];active=pathlib.Path(record['environment']).parent.resolve()
generations=[]
for generation in (install_state_dir(source)/'environments').iterdir():
 lock=generation/'workspace/uv.lock'
 if lock.is_file():generations.append({'name':generation.name,'active':generation.resolve()==active,'mtime':generation.stat().st_mtime,'lock':hashlib.sha256(lock.read_bytes()).hexdigest(),'packages':len(list((generation/'venv/lib').glob('python*/site-packages/*.dist-info')))})
print(json.dumps({'active':active.name,'extras':record['extras'],'generations':sorted(generations,key=lambda item:item['mtime'],reverse=True)}))
'''

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--terminal',required=True);p.add_argument('--terminal-user',required=True);p.add_argument('--terminal-home',required=True)
 p.add_argument('--identity',type=Path,required=True);p.add_argument('--known-hosts',type=Path,required=True);p.add_argument('--host-key-alias',default='hermes-lxc-bootstrap')
 p.add_argument('--restore-generation');p.add_argument('--snapshot',type=Path)
 p.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'.local/versioning/pm-selection.json')
 a=p.parse_args();code='import sys;sys.path[:0]=["/opt/hermes-personal","/opt/hermes"]\n'+PROBE
 if a.restore_generation:
  if not a.snapshot:p.error('Restoration requires a prior captured snapshot')
  if not __import__('re').fullmatch('[a-f0-9]{32}',a.restore_generation):p.error('Invalid generation ID')
  snapshot=json.loads(a.snapshot.read_text())['files']
  original=json.loads(base64.b64decode(snapshot['customizations/default/python-lock.json']['data']))
  features=json.loads(base64.b64decode(snapshot['customizations/default/features.json']['data']))
  code+='\noriginal='+repr(original)+'\nfeatures='+repr(features)+'\ngeneration_id='+repr(a.restore_generation)+'\n'
  code+=r'''
from pm.lock import Facts
from pm.packages import Venv
from pm.workspace import enabled_member_dirs
from pm.features import write_features
generation=install_state_dir(source)/'environments'/generation_id
lock=generation/'workspace/uv.lock'
expected=original['lock'].replace('${HERMES_HOME}',str(home)).replace('${HERMES_SOURCE}',str(source))
assert lock.read_text()==expected,'Generation does not match prior dependency lock'
assert (generation/'venv/bin/python').is_file()
private=home/'state/versioning/selection-recovery-preimage.json'
private.write_bytes(facts.read_bytes());private.chmod(0o600)
Facts(facts).record_state('venv',Venv().expected_stamp(original['extras'],plugin_dirs=enabled_member_dirs()),original['extras'],environment=generation/'venv',resolved_lock=lock)
write_features(features['extras'],home)
print(json.dumps({'restored':True,'generation':generation_id,'extras':original['extras']}))
'''
  # Send the large prior lock over stdin to the terminal instead of SSH argv.
 helper=r'''
import io,pathlib,sys,tempfile,uuid
root=pathlib.Path.home()/'.hermes';sys.path.insert(0,str(root/'runtime'))
from operations_protocol import Client
client=Client(str(root/'.local/runtime/control.sock'),timeout=150)
destination='state/versioning/pm-selection-'+uuid.uuid4().hex+'.py'
with tempfile.NamedTemporaryFile() as pending:
 pending.write(sys.stdin.buffer.read());pending.flush();client.upload(pending.name,destination,expected='absent')
entry='from pathlib import Path;import os;p=Path(os.environ["HERMES_HOME"])/'+repr(destination)+'\ntry:\n exec(compile(p.read_bytes(),str(p),"exec"))\nfinally:\n p.unlink(missing_ok=True)'
result=client.execute(['/opt/hermes/.venv/bin/python','-c',entry],sys.stdout.buffer,sys.stderr.buffer,timeout=120)
sys.exit(result['returncode'])
'''
 remote=['runuser','-u',a.terminal_user,'--','env','HOME='+a.terminal_home,'python3','-c',helper]
 command=['ssh','-oBatchMode=yes','-oStrictHostKeyChecking=yes','-oConnectTimeout=10','-oHostKeyAlias='+a.host_key_alias,'-oUserKnownHostsFile='+str(a.known_hosts),'-i',str(a.identity),a.terminal,shlex.join(remote)]
 a.output.parent.mkdir(parents=True,exist_ok=True);error=a.output.with_suffix('.err')
 with a.output.open('wb') as out,error.open('wb') as err:
  a.output.chmod(0o600);error.chmod(0o600);result=subprocess.run(command,input=code.encode(),stdout=out,stderr=err,timeout=180)
 if result.returncode:raise SystemExit('PM selection operation failed; private diagnostics: '+str(error))
 print(a.output.read_text())
if __name__=='__main__':main()
