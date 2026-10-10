"""Inspect an existing controller process after an observation timeout; never restart it."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess

PROBE=r'''
import json,pathlib,os,signal,sys
root=pathlib.Path.home()/'.hermes';rows=[]
stop=int(sys.argv[1]) if len(sys.argv)>1 else None
for path in pathlib.Path('/proc').iterdir():
 if not path.name.isdigit() or int(path.name)==os.getpid():continue
 try:
  argv=(path/'cmdline').read_bytes().decode().split('\0')
  if not any(value in {'vm/version_workspace.py','vm/sync_workspace.py'} or value.endswith(('/vm/version_workspace.py','/vm/sync_workspace.py')) or "types.ModuleType('reviewed_publisher')" in value for value in argv):continue
  if stop==int(path.name):
   assert path.stat().st_uid==os.getuid(),'Controller belongs to another user'
   assert not (root/'.git/index.lock').exists(),'Git is updating its index; preserve that operation'
   assert (root/'.local/versioning/baseline.json').is_file(),'Preserve unfinished first export'
   os.kill(stop,signal.SIGTERM)
   print(json.dumps({'terminated':stop,'projection_and_baseline':'retained'}));sys.exit(0)
  status=dict(line.split(':',1) for line in (path/'status').read_text().splitlines())
  files=[]
  for fd in (path/'fd').iterdir():
   try:
    target=fd.readlink().as_posix()
    if '/customizations/' in target:files.append(target.split('/customizations/',1)[1])
   except OSError:pass
  io=dict(line.split(':',1) for line in (path/'io').read_text().splitlines())
  children=[]
  pending=[child for task in (path/'task').iterdir() for child in (task/'children').read_text().split()]
  seen=set()
  while pending and len(seen)<20:
   child=pending.pop()
   if child in seen:continue
   seen.add(child)
   try:
    cp=pathlib.Path('/proc')/child
    pending.extend(value for task in (cp/'task').iterdir() for value in (task/'children').read_text().split())
   except OSError:continue
   try:
    cp=pathlib.Path('/proc')/child;argv=(cp/'cmdline').read_bytes().decode().split('\0')
    operation=argv[3] if len(argv)>3 and argv[1]=='-C' else argv[1] if len(argv)>1 else None
    child_io=dict(line.split(':',1) for line in (cp/'io').read_text().splitlines())
    children.append({'pid':child,'name':(cp/'comm').read_text().strip(),'operation':operation,'io':{k:int(child_io[k]) for k in ['rchar','wchar','read_bytes','write_bytes']}})
   except OSError:pass
  rows.append({'pid':path.name,'state':status['State'].strip(),'rss':status.get('VmRSS','').strip(),'wait':(path/'wchan').read_text().strip(),'writing_projection':files,'io':{k:int(io[k]) for k in ['rchar','wchar','read_bytes','write_bytes']},'children':children})
 except (OSError,UnicodeError):pass
private=root/'.local/versioning'
print(json.dumps({'processes':rows,'baseline':(private/'baseline.json').is_file(),'status':json.loads((private/'status.json').read_text()) if (private/'status.json').is_file() else None,'native_error_log':(private/'runtime-error.log').stat().st_size if (private/'runtime-error.log').is_file() else 0}))
'''

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--terminal',required=True);p.add_argument('--terminal-user',required=True);p.add_argument('--terminal-home',required=True)
 p.add_argument('--identity',type=Path,required=True);p.add_argument('--known-hosts',type=Path,required=True);p.add_argument('--host-key-alias',default='hermes-lxc-bootstrap');p.add_argument('--terminate-pid',type=int,help='Explicitly stop a known obsolete controller after its projection was saved');a=p.parse_args()
 remote=['runuser','-u',a.terminal_user,'--','env','HOME='+a.terminal_home,'python3','-c',PROBE]
 if a.terminate_pid is not None:remote.append(str(a.terminate_pid))
 command=['ssh','-oBatchMode=yes','-oStrictHostKeyChecking=yes','-oConnectTimeout=10','-oHostKeyAlias='+a.host_key_alias,'-oUserKnownHostsFile='+str(a.known_hosts),'-i',str(a.identity),a.terminal,shlex.join(remote)]
 result=subprocess.run(command,capture_output=True,text=True,timeout=30)
 if result.returncode:raise SystemExit('Controller inspection failed')
 print(result.stdout)
if __name__=='__main__':main()
