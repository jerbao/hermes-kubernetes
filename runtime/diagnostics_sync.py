"""Send private runtime diagnostics to the owning VM for self-debugging."""
import io
import json
import os
import shlex
import signal
import subprocess
import tarfile
import threading
from pathlib import Path

REMOTE = r'''
import os, pathlib, sys, tarfile, tempfile
destination = pathlib.Path(sys.argv[1])
if destination.is_symlink():
    raise ValueError("Diagnostics destination must not be a symlink")
destination.mkdir(mode=0o700, parents=True, exist_ok=True)
destination.chmod(0o700)
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
    for member in archive:
        if not member.isfile() or '/' in member.name or member.name in {'.', '..'} or member.size > 1048576:
            raise ValueError("Invalid diagnostics entry")
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as target:
            target.write(archive.extractfile(member).read())
            temporary = pathlib.Path(target.name)
        temporary.chmod(0o600)
        temporary.replace(destination / member.name)
'''


def collect(home):
    files = {"provenance.json": home / "state/hermes-provenance.json",
             "gateway_state.json": home / "gateway_state.json"}
    files.update({p.name: p for p in sorted((home / "logs").glob("*.log"))[-3:]})
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, path in files.items():
            if not path.is_file() or path.is_symlink():
                continue
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 262144))
                content = handle.read(262144)
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(content), 0o600
            archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


def send(home, instance):
    destination = str(Path(instance["workspace"]) / ".local/diagnostics")
    user = os.environ.get("TERMINAL_SSH_USER", instance["ssh_user"])
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", f"UserKnownHostsFile={home / '.ssh/known_hosts'}", "-o", "ConnectTimeout=10",
               "-i", str(home / ".ssh/id_ed25519"), f"{user}@hermes-terminal",
               "python3 -c " + shlex.quote(REMOTE) + " " + shlex.quote(destination)]
    subprocess.run(command, input=collect(home), stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, timeout=30, check=True)


def main():
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    while not stop.is_set():
        try:
            instance = json.loads(Path("/etc/hermes-policy/instance.json").read_text())
            if (home / "state/hermes-provenance.json").is_file():
                send(home, instance)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"Diagnostics sync: {type(error).__name__}", flush=True)
        stop.wait(60)


if __name__ == "__main__":
    main()
