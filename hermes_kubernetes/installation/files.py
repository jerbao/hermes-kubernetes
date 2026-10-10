"""Private credential IO and checked command execution."""

import os
from pathlib import Path
import subprocess
import tempfile


def private_directory(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Private paths must not use symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_private(path, content):
    path = Path(path)
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Refusing a symlinked credential")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def credential(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("Credential must be a regular, non-symlinked file")
    if path.stat().st_mode & 0o077:
        raise ValueError("Credential file must have mode 0600 or 0400")
    return path.read_text()


def run(argv, *, input=None, env=None):
    result = subprocess.run(argv, input=input, text=True, capture_output=True, env=env)
    if result.returncode:
        # Tool stderr and command arguments may contain secrets. Do not forward them.
        raise RuntimeError(f"{Path(argv[0]).name} failed (exit {result.returncode})")
    return result.stdout


def keypair(path):
    path = Path(path)
    private_directory(path.parent)
    if not path.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)])
    credential(path)
    return run(["ssh-keygen", "-y", "-f", str(path)]).strip()
