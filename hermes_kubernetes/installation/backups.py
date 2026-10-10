"""Scheduled encrypted state/peripheral backups using the operations snapshot protocol."""

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import threading

from hermes_kubernetes.backup.archive import verify_archive
from hermes_kubernetes.backup.drive import Remote
from hermes_kubernetes.backup.remote import backup
from hermes_kubernetes.backup.state import digest


# Only explicitly selected terminal paths. No recursive home or implicit cache selection.
WORKSPACE_SNAPSHOT = r"""
import importlib.util,json,pathlib,sys,tempfile
root=pathlib.Path(sys.argv[1]); paths=json.loads(sys.argv[2])
spec=importlib.util.spec_from_file_location('instance', root/'hermes.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
bundle=module.checkout(root,json.loads((root/'platform.lock.json').read_text()))
sys.path.insert(0,str(bundle))
from hermes_kubernetes.backup.job import backup
policy={'schema':1,'include':paths,'exclude':[],'required':[],'external_links':[]}
with tempfile.TemporaryDirectory(prefix='hermes-peripherals-') as tmp:
    report=backup(root,pathlib.Path(tmp).resolve(),kind='workspace',policy=policy)
    with (pathlib.Path(tmp)/report['archive']).open('rb') as f:
        import shutil; shutil.copyfileobj(f,sys.stdout.buffer)
"""


def once(home, directory, *, extra_paths=(), keep=6):
    status = backup(Path("/tmp/hermes-operations/control.sock"), directory, keep)
    with Remote("/etc/hermes-backup/rclone.conf") as remote:
        remote.upload(directory / status["archive"], "state", keep=keep)
        instance = json.loads(Path("/etc/hermes-policy/instance.json").read_text())
        selected = ["customizations", *extra_paths]
        target = instance["ssh_user"] + "@hermes-terminal"
        archive = directory / status["archive"].replace("hermes-state-", "hermes-workspace-")
        with archive.open("wb") as output:
            result = subprocess.run(
                [
                    "ssh",
                    "-T",
                    "-o",
                    "BatchMode=yes",
                    target,
                    "python3 -c "
                    + shlex.quote(WORKSPACE_SNAPSHOT)
                    + " "
                    + shlex.quote(instance["workspace"])
                    + " "
                    + shlex.quote(json.dumps(selected)),
                ],
                stdout=output,
                stderr=subprocess.DEVNULL,
            )
        if result.returncode:
            raise RuntimeError("Peripheral snapshot failed")
        verify_archive(archive)
        remote.upload(archive, "workspace", keep=keep)
        return {"state_sha256": status["sha256"], "workspace_sha256": digest(archive)}


def main():
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    interval = int(os.environ["BACKUP_INTERVAL_HOURS"]) * 3600
    # Installation itself never runs a backup. First scheduled execution is one interval later.
    while not stop.wait(interval):
        try:
            with tempfile.TemporaryDirectory(prefix="hermes-backup-") as temporary:
                result = once(
                    Path("/opt/data"),
                    Path(temporary),
                    extra_paths=[
                        p for p in os.environ.get("BACKUP_EXTRA_PATHS", "").split(",") if p
                    ],
                    keep=int(os.environ["BACKUP_KEEP"]),
                )
            print(json.dumps({"event": "backup-completed", **result}), flush=True)
        except Exception as error:
            # A visible container error is retained in Kubernetes logs; no token/command output.
            print(
                json.dumps({"event": "backup-failed", "reason": type(error).__name__}), flush=True
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
