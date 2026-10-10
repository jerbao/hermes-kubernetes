"""Bounded frames and streaming transfers over a private SSH Unix socket."""

import hashlib
import json
import os
import socket
import struct
import tempfile
from pathlib import Path

CHUNK = 65536
MAX_FRAME = 1024 * 1024


class OperationError(RuntimeError):
    pass


def receive_exact(connection, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise EOFError("Runtime connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def send(connection, kind, payload):
    if kind not in {b"J", b"D", b"O", b"E"} or len(payload) > MAX_FRAME:
        raise OperationError("invalid-frame")
    connection.sendall(kind + struct.pack("!I", len(payload)) + payload)


def receive(connection):
    header = receive_exact(connection, 5)
    kind, size = header[:1], struct.unpack("!I", header[1:])[0]
    if kind not in {b"J", b"D", b"O", b"E"} or size > MAX_FRAME:
        raise OperationError("invalid-frame")
    return kind, receive_exact(connection, size)


def send_json(connection, value):
    send(connection, b"J", json.dumps(value, ensure_ascii=False).encode())


def receive_json(connection):
    kind, payload = receive(connection)
    if kind != b"J":
        raise OperationError("expected-json")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise OperationError("expected-object")
    if "error" in value:
        raise OperationError(value["error"])
    return value


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class Client:
    def __init__(self, path, timeout=150):
        self.path, self.timeout = str(path), timeout

    def connect(self, request):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self.path)
            send_json(connection, request)
            return connection
        except BaseException:
            connection.close()
            raise

    def request(self, operation, **fields):
        with self.connect({"op": operation, **fields}) as connection:
            return receive_json(connection)

    def download(self, destination, operation="get", **fields):
        destination = Path(destination)
        # Preserve existing files, including symlinks. Never place a partial
        # database at the final name or buffer the database in memory.
        if destination.exists() or destination.is_symlink():
            raise OperationError("destination-exists")
        descriptor, name = tempfile.mkstemp(prefix=".hermes-transfer-", dir=destination.parent)
        temporary = Path(name)
        try:
            with (
                os.fdopen(descriptor, "wb") as handle,
                self.connect({"op": operation, **fields}) as connection,
            ):
                metadata = receive_json(connection)
                if metadata.get("event") != "file":
                    raise OperationError("expected-file")
                digest, total = hashlib.sha256(), 0
                while True:
                    kind, payload = receive(connection)
                    if kind == b"J":
                        result = json.loads(payload)
                        if "error" in result:
                            raise OperationError(result["error"])
                        if result.get("event") != "complete":
                            raise OperationError("incomplete-transfer")
                        break
                    if kind != b"D":
                        raise OperationError("expected-file-data")
                    total += len(payload)
                    if total > metadata["size"]:
                        raise OperationError("invalid-file-size")
                    digest.update(payload)
                    handle.write(payload)
                if total != metadata["size"] or result["sha256"] != digest.hexdigest():
                    raise OperationError("transfer-integrity-failed")
                handle.flush()
                os.fsync(handle.fileno())
            # link() is atomic and refuses a file created concurrently.
            os.link(temporary, destination)
            return {**result, "size": total}
        finally:
            temporary.unlink(missing_ok=True)

    def upload(self, source, path, expected, scope="state"):
        source = Path(source)
        digest = file_hash(source)
        with (
            source.open("rb") as handle,
            self.connect(
                {
                    "op": "put",
                    "scope": scope,
                    "path": path,
                    "expected": expected,
                    "size": source.stat().st_size,
                    "sha256": digest,
                }
            ) as connection,
        ):
            receive_json(connection)
            while chunk := handle.read(CHUNK):
                send(connection, b"D", chunk)
            send_json(connection, {"event": "complete"})
            return receive_json(connection)

    def execute(self, argv, stdout, stderr, timeout=120, scope="state", cwd="."):
        with self.connect(
            {"op": "exec", "argv": argv, "timeout": timeout, "scope": scope, "cwd": cwd}
        ) as connection:
            receive_json(connection)
            while True:
                kind, payload = receive(connection)
                if kind in {b"O", b"E"}:
                    target = stdout if kind == b"O" else stderr
                    target.write(payload)
                    target.flush()
                elif kind == b"J":
                    result = json.loads(payload)
                    if "error" in result:
                        raise OperationError(result["error"])
                    if result.get("event") != "complete":
                        raise OperationError("incomplete-command")
                    return result
                else:
                    raise OperationError("unexpected-command-frame")
