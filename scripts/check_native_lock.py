"""Exercise frozen restoration with the actual native PM in an isolated home."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess

PROBE=r'''
import hashlib,json,os,pathlib,shutil,tempfile
home=pathlib.Path(os.environ['HERMES_HOME']);source=pathlib.Path('/opt/hermes')
from pm.environments import runtime_facts_path
live=runtime_facts_path(source);before=live.read_bytes() if live.exists() else None
isolated=pathlib.Path(tempfile.mkdtemp(prefix='hermes-native-lock-')).resolve()
previous=os.environ.get('HERMES_RUNTIME_DIR')
try:
 (isolated/'config.yaml').write_text('plugins: {}\n')
 from dependencies import prepare_store
 os.environ['HERMES_RUNTIME_DIR']=str(prepare_store(isolated))
 record={'schema':1,'extras':[],'lock':(source/'uv.lock').read_text(),'members':{}}
 restore_python_installation(isolated,record,source)
 key=hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
 facts=json.loads((isolated/'installs'/key/'facts.json').read_text())['packages']['venv']
 assert pathlib.Path(facts['environment'],'bin/python').is_file()
 assert pathlib.Path(facts['resolved_lock']).read_text()==record['lock']
 assert (live.read_bytes() if live.exists() else None)==before
 print(json.dumps({'passed':True,'frozen_lock_preserved':True,'production_facts_unchanged':True,'native_pm_environment_verified':True}))
finally:
 if previous is None:os.environ.pop('HERMES_RUNTIME_DIR',None)
 else:os.environ['HERMES_RUNTIME_DIR']=previous
 shutil.rmtree(isolated)
'''

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--terminal',required=True);p.add_argument('--terminal-user',required=True);p.add_argument('--terminal-home',required=True)
 p.add_argument('--identity',type=Path,required=True);p.add_argument('--known-hosts',type=Path,required=True);p.add_argument('--host-key-alias',default='hermes-lxc-bootstrap')
 p.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'.local/versioning/native-lock-check.json')
 a=p.parse_args();root=Path(__file__).resolve().parents[1]
 source=(root/'runtime/versioned_state.py').read_text().replace('from __future__ import annotations\n','').replace("if __name__=='__main__':main()",'')
 code='import sys;sys.path[:0]=["/opt/hermes-personal","/opt/hermes"]\n'+source+'\n'+PROBE
 remote=['runuser','-u',a.terminal_user,'--','env','HOME='+a.terminal_home,'python3',a.terminal_home+'/.hermes/vm/runtime_client.py','exec','--timeout','540','--','/opt/hermes/.venv/bin/python','-c',code]
 command=['ssh','-oBatchMode=yes','-oStrictHostKeyChecking=yes','-oConnectTimeout=10','-oHostKeyAlias='+a.host_key_alias,'-oUserKnownHostsFile='+str(a.known_hosts),'-i',str(a.identity),a.terminal,shlex.join(remote)]
 a.output.parent.mkdir(parents=True,exist_ok=True);error=a.output.with_suffix('.err')
 with a.output.open('wb') as out,error.open('wb') as err:
  a.output.chmod(0o600);error.chmod(0o600);result=subprocess.run(command,stdout=out,stderr=err,timeout=600)
 if result.returncode:raise SystemExit('Native lock check failed; private diagnostics: '+str(error))
 print(a.output.read_text())
if __name__=='__main__':main()
