"""Publish an already exported projection with a reviewed controller revision.

Useful when an older controller stopped after capturing native data. Load the
new publisher in memory, preserving dirty source and the existing projection;
the normal commit/merge/push path integrates the fetched update afterward.
"""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess

REMOTE = r"""
import json,os,pathlib,subprocess,sys,types,uuid
root=pathlib.Path.home()/'.hermes';revision=sys.argv[1]
from hermes_kubernetes.terminal.guard import guard
with guard(root/'state/workspace-sync.lock',create=True,blocking=False):
 if not (root/'.local/versioning/baseline.json').is_file():raise RuntimeError('No completed projection; run the initial capture instead')
 lock=root/'.git/index.lock'
 if lock.exists():
  if len(sys.argv)<3 or sys.argv[2]!='preserve-stale-index-lock':raise RuntimeError('Inspect the previous Git process before preserving its stale index lock')
  if lock.is_symlink() or lock.stat().st_uid!=os.getuid():raise RuntimeError('Unexpected index lock owner/type')
  for process in pathlib.Path('/proc').iterdir():
   if not process.name.isdigit():continue
   try:
    if process.stat().st_uid==os.getuid() and (process/'comm').read_text().strip()=='git':raise RuntimeError('Git process still running; retain its index lock')
   except OSError:pass
  lock.rename(root/'.local/versioning'/('stale-index-lock-'+uuid.uuid4().hex))
 def git(*args):return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.PIPE).strip()
 git('fetch','origin','main')
 subprocess.run(['git','-C',str(root),'merge-base','--is-ancestor',revision,'origin/main'],check=True,capture_output=True)
 review=types.ModuleType('sync_workspace');review.__file__=str(root/'hermes_kubernetes/terminal/sync.py')
 exec(git('show',revision+':hermes_kubernetes/terminal/sync.py'),review.__dict__);sys.modules['sync_workspace']=review
 module=types.ModuleType('reviewed_publisher');module.__file__=str(root/'hermes_kubernetes/terminal/versioning.py')
 exec(git('show',revision+':hermes_kubernetes/terminal/versioning.py'),module.__dict__)
 result=module.publish(root,quiet_seconds=0)
 module.atomic(root/'.local/versioning/resumed-publication.json',module.dump(result))
 print(json.dumps(result))
 if result['status'] not in {'published','current'}:sys.exit(2)
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in [
        "terminal",
        "terminal-user",
        "terminal-home",
        "identity",
        "known-hosts",
        "revision",
    ]:
        p.add_argument("--" + name, required=True)
    p.add_argument("--host-key-alias", default="hermes-lxc-bootstrap")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--preserve-stale-index-lock",
        action="store_true",
        help="After checking no Git process is alive, archive an interrupted index lock",
    )
    a = p.parse_args()
    if not re.fullmatch("[0-9a-f]{40}", a.revision):
        p.error("Use the reviewed full commit SHA")
    remote = [
        "runuser",
        "-u",
        a.terminal_user,
        "--",
        "env",
        "HOME=" + a.terminal_home,
        "python3",
        "-c",
        REMOTE,
        a.revision,
    ]
    if a.preserve_stale_index_lock:
        remote.append("preserve-stale-index-lock")
    command = [
        "ssh",
        "-oBatchMode=yes",
        "-oStrictHostKeyChecking=yes",
        "-oConnectTimeout=10",
        "-oHostKeyAlias=" + a.host_key_alias,
        "-oUserKnownHostsFile=" + a.known_hosts,
        "-i",
        a.identity,
        a.terminal,
        shlex.join(remote),
    ]
    a.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    error = a.output.with_suffix(".err")
    with a.output.open("wb") as out, error.open("wb") as err:
        a.output.chmod(0o600)
        error.chmod(0o600)
        result = subprocess.run(command, stdout=out, stderr=err, timeout=1200)
    if result.returncode:
        raise SystemExit("Publication failed; inspect private diagnostics: " + str(error))
    print(a.output.read_text())


if __name__ == "__main__":
    main()
