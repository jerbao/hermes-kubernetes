"""Operate the actual Hermes home; publish only through its existing SSH link."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import hashlib
import json
import os
import selectors
import signal
import socketserver
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

from hermes_kubernetes.operations.protocol import (
    CHUNK,
    Client,
    OperationError,
    file_hash,
    receive,
    receive_json,
    send,
    send_json,
)


class Runtime:
    def __init__(
        self,
        home,
        source=Path("/opt/hermes"),
        bundle=Path("/opt/hermes-personal"),
        instance=None,
    ):
        self.roots = {
            "state": Path(home).resolve(),
            "source": Path(source).resolve(),
            "integration": Path(bundle).resolve(),
        }
        self.instance = instance or {}
        self.tunnel_ready = False
        self.write_lock = threading.Lock()
        self.snapshot_lock = threading.Lock()

    def path(self, request, field="path", writable=False):
        scope = request.get("scope", "state")
        if scope not in self.roots or writable and scope != "state":
            raise OperationError("read-only-scope")
        relative = Path(request.get(field, "."))
        if relative.is_absolute() or ".." in relative.parts:
            raise OperationError("path-outside-scope")
        path = self.roots[scope]
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise OperationError("symlink-requires-explicit-exec")
        if not path.resolve().is_relative_to(self.roots[scope]):
            raise OperationError("path-outside-scope")
        return path

    def info(self):
        provenance = self.roots["state"] / "state/hermes-provenance.json"
        return {
            "instance": self.instance.get("name"),
            "pid": os.getpid(),
            "uid": os.getuid(),
            "tunnel_ready": self.tunnel_ready,
            "roots": {k: str(v) for k, v in self.roots.items()},
            "provenance": json.loads(provenance.read_text()) if provenance.exists() else None,
        }

    @staticmethod
    def reject_database(path):
        if path.name.endswith(("-wal", "-shm")):
            raise OperationError("use-sqlite-backup")
        if path.exists() and path.is_file():
            with path.open("rb") as handle:
                if handle.read(16) == b"SQLite format 3\x00":
                    raise OperationError("use-sqlite-backup")

    @staticmethod
    def transfer(connection, path):
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            send_json(connection, {"event": "file", "size": before.st_size})
            digest, total = hashlib.sha256(), 0
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
                total += len(chunk)
                send(connection, b"D", chunk)
            after = os.fstat(handle.fileno())
            if total != before.st_size or (before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise OperationError("file-changed-during-transfer")
            send_json(connection, {"event": "complete", "sha256": digest.hexdigest()})

    def put(self, connection, request):
        path = self.path(request, writable=True)
        self.reject_database(path)
        expected = request.get("expected")
        if not isinstance(expected, str) or (
            expected != "absent"
            and (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected))
        ):
            raise OperationError("expected-sha256-required")
        size, checksum = request.get("size"), request.get("sha256")
        if not isinstance(size, int) or size < 0 or not isinstance(checksum, str):
            raise OperationError("invalid-file-metadata")

        def unchanged():
            if expected == "absent":
                return not path.exists() and not path.is_symlink()
            return path.is_file() and not path.is_symlink() and file_hash(path) == expected

        with self.write_lock:
            if not unchanged():
                raise OperationError("conflict")
        descriptor, name = tempfile.mkstemp(prefix=".hermes-write-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                send_json(connection, {"event": "ready"})
                total, digest = 0, hashlib.sha256()
                while True:
                    kind, payload = receive(connection)
                    if kind == b"J":
                        if json.loads(payload) != {"event": "complete"}:
                            raise OperationError("incomplete-upload")
                        break
                    if kind != b"D" or total + len(payload) > size:
                        raise OperationError("invalid-file-size")
                    total += len(payload)
                    digest.update(payload)
                    handle.write(payload)
                if total != size or digest.hexdigest() != checksum:
                    raise OperationError("transfer-integrity-failed")
                handle.flush()
                os.fsync(handle.fileno())
            self.reject_database(temporary)
            with self.write_lock:
                # Recheck after transfer to preserve concurrent personal edits.
                self.path(request, writable=True)
                if not unchanged():
                    raise OperationError("conflict")
                if expected == "absent":
                    os.link(temporary, path)
                else:
                    os.replace(temporary, path)
            send_json(
                connection,
                {"event": "complete", "sha256": digest.hexdigest(), "size": size},
            )
        finally:
            temporary.unlink(missing_ok=True)

    def sqlite_backup(self, connection, request):
        path = self.path(request)
        if not path.is_file():
            raise OperationError("file-not-found")
        with tempfile.TemporaryDirectory(prefix="hermes-sqlite-") as temporary:
            destination = Path(temporary) / "database.sqlite3"
            started = time.monotonic()

            def progress(status, remaining, total):
                if time.monotonic() - started > 120:
                    raise OperationError("database-backup-timeout")

            with (
                closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source,
                closing(sqlite3.connect(destination)) as target,
            ):
                source.execute("PRAGMA cache_size=-2048")
                source.backup(target, pages=256, progress=progress, sleep=0.05)
            self.transfer(connection, destination)

    def execute(self, connection, request):
        argv, timeout = request.get("argv"), request.get("timeout", 120)
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) and "\x00" not in a for a in argv)
        ):
            raise OperationError("argv-required")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise OperationError("invalid-command-timeout")
        cwd = self.path(request, "cwd")
        env = {
            **os.environ,
            "HOME": str(self.roots["state"]),
            "HERMES_HOME": str(self.roots["state"]),
        }
        env["PATH"] = str(self.roots["source"] / ".venv/bin") + ":" + env.get("PATH", os.defpath)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            send_json(connection, {"event": "started", "pid": process.pid})
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, b"O")
                selector.register(process.stderr, selectors.EVENT_READ, b"E")
                while selector.get_map():
                    if time.monotonic() > deadline:
                        timed_out = True
                        break
                    for key, _ in selector.select(
                        timeout=min(0.2, max(0, deadline - time.monotonic()))
                    ):
                        chunk = os.read(key.fileobj.fileno(), CHUNK)
                        if chunk:
                            send(connection, key.data, chunk)
                        else:
                            selector.unregister(key.fileobj)
                if timed_out:
                    self.stop_command(process)
                else:
                    try:
                        process.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        self.stop_command(process)
            send_json(
                connection,
                {
                    "event": "complete",
                    "returncode": process.returncode,
                    "timed_out": timed_out,
                },
            )
        finally:
            if process.poll() is None:
                self.stop_command(process)
            process.stdout.close()
            process.stderr.close()

    @staticmethod
    def stop_command(process):
        # This session is created and held by Popen above, never selected by argv.
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            process.wait()
        finally:
            # A child may ignore TERM even after the command leader exits.
            # The group was created by this Popen, not found by a name search.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def handle(self, connection):
        request = receive_json(connection)
        operation = request.get("op")
        if operation == "info":
            send_json(connection, self.info())
        elif operation == "list":
            path = self.path(request)
            send_json(
                connection,
                {
                    "entries": [
                        {
                            "name": p.name,
                            "directory": p.is_dir(),
                            "symlink": p.is_symlink(),
                        }
                        for p in sorted(path.iterdir())
                    ]
                },
            )
        elif operation == "stat":
            path = self.path(request)
            details = path.stat()
            send_json(
                connection,
                {
                    "size": details.st_size,
                    "mode": oct(details.st_mode & 0o777),
                    "directory": path.is_dir(),
                    "sha256": file_hash(path) if request.get("sha256") and path.is_file() else None,
                },
            )
        elif operation == "get":
            path = self.path(request)
            self.reject_database(path)
            self.transfer(connection, path)
        elif operation == "put":
            self.put(connection, request)
        elif operation == "sqlite-backup":
            self.sqlite_backup(connection, request)
        elif operation == "snapshot":
            from hermes_kubernetes.backup.state import build_snapshot

            if not self.snapshot_lock.acquire(blocking=False):
                raise OperationError("snapshot-already-running")
            try:
                with tempfile.TemporaryDirectory(prefix="hermes-snapshot-") as temporary:
                    archive = Path(temporary) / "state.tar.gz"
                    build_snapshot(self.roots["state"], archive)
                    self.transfer(connection, archive)
            finally:
                self.snapshot_lock.release()
        elif operation == "exec":
            self.execute(connection, request)
        else:
            raise OperationError("unknown-operation")


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            send_json(self.request, {"error": "runtime-busy"})
            return
        try:
            self.request.settimeout(150)
            self.server.runtime.handle(self.request)
        except (OperationError, OSError, EOFError, ValueError, sqlite3.Error) as error:
            try:
                send_json(
                    self.request,
                    {
                        "error": str(error)
                        if isinstance(error, OperationError)
                        else type(error).__name__
                    },
                )
            except OSError:
                pass
        finally:
            self.server.slots.release()


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, runtime):
        self.runtime, self.slots = runtime, threading.BoundedSemaphore(4)
        super().__init__(str(path), Handler)
        Path(path).chmod(0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", action="store_true")
    arguments = parser.parse_args()
    socket_path = Path(
        os.environ.get("HERMES_OPERATIONS_SOCKET", "/tmp/hermes-operations/control.sock")
    )
    if arguments.health:
        return 0 if Client(socket_path, timeout=3).request("info")["tunnel_ready"] else 1
    from hermes_kubernetes.operations.tunnel import (
        Tunnel,
        private_directory,
        remove_stale_socket,
    )

    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    policy = Path(os.environ.get("HERMES_MANAGED_DIR", "/etc/hermes-policy"))
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    while not stop.is_set() and not (home / ".ssh/config").exists():
        stop.wait(1)
    if stop.is_set():
        return 0
    instance = json.loads((policy / "instance.json").read_text())
    runtime = Runtime(home, instance=instance)
    private_directory(socket_path.parent)
    remove_stale_socket(socket_path)
    with Server(socket_path, runtime) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            Tunnel(home, instance, socket_path).run(stop, runtime)
        finally:
            server.shutdown()
            thread.join()
            socket_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
