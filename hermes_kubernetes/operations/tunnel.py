"""Lease a user-private VM socket and reconnect its reverse SSH forwarding."""

import errno
import json
import os
import selectors
import shlex
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path


def private_directory(path):
    path = Path(path)
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise RuntimeError("Socket directory must not contain symlinks")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    details = path.stat()
    if details.st_uid != os.getuid() or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError("Socket directory belongs to another user")
    path.chmod(0o700)


def remove_stale_socket(path):
    path = Path(path)
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if details.st_uid != os.getuid() or not stat.S_ISSOCK(details.st_mode):
        raise RuntimeError("Existing path is not an owned socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        try:
            connection.connect(str(path))
        except OSError as error:
            if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise
        else:
            raise RuntimeError("Socket already has an active listener")
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (details.st_dev, details.st_ino):
        raise RuntimeError("Socket changed during cleanup")
    path.unlink()


REMOTE_LEASE = r"""
import errno, fcntl, json, os, pathlib, socket, stat, sys
path = pathlib.Path(sys.argv[1])
if not path.is_absolute() or len(os.fsencode(path)) > 103 or ':' in str(path) or '\n' in str(path):
    raise RuntimeError('Invalid runtime socket path')
parent = path.parent
if any(p.is_symlink() for p in [parent, *parent.parents]):
    raise RuntimeError('Runtime directory contains a symlink')
parent.mkdir(parents=True, mode=0o700, exist_ok=True)
if parent.stat().st_uid != os.getuid():
    raise RuntimeError('Runtime directory belongs to another user')
parent.chmod(0o700)
lock = os.open(parent / 'lease.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
if os.fstat(lock).st_uid != os.getuid():
    raise RuntimeError('Lease belongs to another user')
os.fchmod(lock, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
owned = None
try:
    if path.exists() or path.is_symlink():
        previous = path.lstat()
        if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.getuid():
            raise RuntimeError('Existing path is not an owned socket')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(2)
            try:
                probe.connect(str(path))
            except OSError as error:
                if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}: raise
            else:
                raise RuntimeError('Runtime socket is already active')
        current = path.lstat()
        if (previous.st_dev, previous.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError('Runtime socket changed during cleanup')
        path.unlink()
    print(json.dumps({'event': 'lease-ready'}), flush=True)
    if sys.stdin.readline().strip() != 'bound':
        raise RuntimeError('Forwarding was not acknowledged')
    current = path.lstat()
    if not stat.S_ISSOCK(current.st_mode) or current.st_uid != os.getuid():
        raise RuntimeError('Forwarded socket has an unexpected owner')
    owned = (current.st_dev, current.st_ino)
    path.chmod(0o600)
    print(json.dumps({'event': 'bound'}), flush=True)
    for line in sys.stdin:
        if line.strip() == 'close': break
finally:
    if owned is not None:
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == owned: path.unlink()
        except FileNotFoundError:
            pass
    os.close(lock)
"""


def acknowledgement(process, expected, timeout=20):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise RuntimeError("SSH lease acknowledgement timed out")
        line = process.stdout.readline(4096)
    try:
        event = json.loads(line).get("event")
    except ValueError:
        raise RuntimeError("SSH lease closed before acknowledgement") from None
    if event != expected:
        raise RuntimeError("Unexpected SSH lease acknowledgement")


class Tunnel:
    def __init__(self, home, instance, local_socket):
        self.home, self.local_socket = Path(home), Path(local_socket)
        self.remote_socket = str(Path(instance["workspace"]) / ".local/runtime/control.sock")
        if (
            not Path(self.remote_socket).is_absolute()
            or len(os.fsencode(self.remote_socket)) > 103
            or ":" in self.remote_socket
        ):
            raise ValueError("Invalid or too long VM Unix socket path")
        self.target = (
            f"{instance['ssh_user']}@{os.environ.get('TERMINAL_SSH_HOST', 'hermes-terminal')}"
        )
        self.options = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"UserKnownHostsFile={self.home / '.ssh/known_hosts'}",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-i",
            str(self.home / ".ssh/id_ed25519"),
            "-p",
            str(instance.get("terminal_port", 22)),
        ]

    def connection(self, stop, runtime):
        with tempfile.TemporaryDirectory(prefix="hermes-ssh-") as temporary:
            control = str(Path(temporary) / "master")
            command = self.options + [
                "-M",
                "-S",
                control,
                self.target,
                "python3",
                "-u",
                "-c",
                shlex.quote(REMOTE_LEASE),
                shlex.quote(self.remote_socket),
            ]
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                acknowledgement(process, "lease-ready")
                subprocess.run(
                    self.options
                    + [
                        "-S",
                        control,
                        "-O",
                        "forward",
                        "-R",
                        f"{self.remote_socket}:{self.local_socket}",
                        self.target,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=20,
                )
                process.stdin.write(b"bound\n")
                process.stdin.flush()
                acknowledgement(process, "bound")
                runtime.tunnel_ready = True
                print(
                    json.dumps(
                        {
                            "event": "runtime-connected",
                            "instance": runtime.instance.get("name"),
                        }
                    ),
                    flush=True,
                )
                while not stop.wait(1) and process.poll() is None:
                    pass
            finally:
                runtime.tunnel_ready = False
                if process.poll() is None:
                    try:
                        process.stdin.write(b"close\n")
                        process.stdin.flush()
                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                process.stdin.close()
                process.stdout.close()

    def run(self, stop, runtime):
        delay = 1
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.connection(stop, runtime)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                # Arguments, config and SSH stderr can contain personal data.
                print(
                    json.dumps(
                        {
                            "event": "runtime-disconnected",
                            "reason": type(error).__name__,
                        }
                    ),
                    flush=True,
                )
            if time.monotonic() - started > 60:
                delay = 1
            stop.wait(delay)
            delay = min(30, delay * 2)
