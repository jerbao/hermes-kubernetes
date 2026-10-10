import io
import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from hermes_kubernetes.operations.server import Runtime, Server
from hermes_kubernetes.operations.protocol import (
    Client,
    OperationError,
    file_hash,
    receive,
    receive_json,
    send,
    send_json,
)
from hermes_kubernetes.operations.tunnel import REMOTE_LEASE, remove_stale_socket


class RuntimeOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "state"
        self.home.mkdir()
        self.source = self.base / "source"
        self.source.mkdir()
        self.runtime = Runtime(self.home, self.source, self.base, {"name": "fixture"})
        self.server = Server(self.base / "socket", self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = Client(self.base / "socket")

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary.cleanup()

    def test_existing_edits_and_source_are_preserved(self):
        source = self.base / "input"
        source.write_text("first version")
        self.client.upload(source, "skill.txt", "absent")
        expected = file_hash(self.home / "skill.txt")
        (self.home / "skill.txt").write_text("concurrent edit")
        with self.assertRaisesRegex(OperationError, "conflict"):
            self.client.upload(source, "skill.txt", expected)
        self.assertEqual((self.home / "skill.txt").read_text(), "concurrent edit")
        with self.assertRaisesRegex(OperationError, "read-only-scope"):
            self.client.upload(source, "code.py", "absent", scope="source")
        with self.assertRaisesRegex(OperationError, "path-outside-scope"):
            self.client.request("stat", path="../input")
        (self.home / "external").symlink_to(source)
        with self.assertRaisesRegex(OperationError, "symlink"):
            self.client.request("stat", path="external")
        self.assertEqual((self.home / "skill.txt").stat().st_mode & 0o777, 0o600)

    def test_upload_rechecks_after_streaming_and_discards_partial_files(self):
        (self.home / "config.yaml").write_text("original")
        expected = file_hash(self.home / "config.yaml")
        with self.client.connect(
            {
                "op": "put",
                "path": "config.yaml",
                "expected": expected,
                "size": 3,
                "sha256": __import__("hashlib").sha256(b"new").hexdigest(),
            }
        ) as connection:
            receive_json(connection)
            (self.home / "config.yaml").write_text("changed during upload")
            send(connection, b"D", b"new")
            send_json(connection, {"event": "complete"})
            with self.assertRaisesRegex(OperationError, "conflict"):
                receive_json(connection)
        self.assertEqual((self.home / "config.yaml").read_text(), "changed during upload")
        self.assertFalse(list(self.home.glob(".hermes-write-*")))

    def test_live_database_uses_sqlite_backup_including_wal(self):
        path = self.home / "state.db"
        database = sqlite3.connect(path)
        self.addCleanup(database.close)
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE data (value TEXT)")
        database.execute("INSERT INTO data VALUES ('committed in WAL')")
        database.commit()
        with self.assertRaisesRegex(OperationError, "use-sqlite-backup"):
            self.client.download(self.base / "unsafe.db", path="state.db")
        self.assertFalse((self.base / "unsafe.db").exists())
        destination = self.base / "restored.db"
        self.client.download(destination, "sqlite-backup", path="state.db")
        restored = sqlite3.connect(destination)
        try:
            self.assertEqual(
                restored.execute("SELECT value FROM data").fetchone()[0],
                "committed in WAL",
            )
            self.assertEqual(restored.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            restored.close()
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(OperationError, "destination-exists"):
            self.client.download(destination, "sqlite-backup", path="state.db")

    def test_streaming_preserves_binary_and_sha_without_partial_overwrite(self):
        data = bytes(range(256)) * 20000
        (self.home / "binary").write_bytes(data)
        destination = self.base / "download"
        result = self.client.download(destination, path="binary")
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(result["size"], len(data))
        self.assertEqual(result["sha256"], file_hash(destination))

    def test_exec_uses_live_home_preserves_streams_and_returns_status(self):
        stdout, stderr = io.BytesIO(), io.BytesIO()
        result = self.client.execute(
            [
                sys.executable,
                "-c",
                "import os,sys; print(os.environ['HERMES_HOME']); print('error stream',file=sys.stderr); sys.exit(7)",
            ],
            stdout,
            stderr,
        )
        self.assertEqual(result["returncode"], 7)
        self.assertFalse(result["timed_out"])
        self.assertEqual(stdout.getvalue().decode().strip(), str(self.home))
        self.assertEqual(stderr.getvalue(), b"error stream\n")

    def test_exec_timeout_does_not_stop_an_unrelated_process(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            result = self.client.execute(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                io.BytesIO(),
                io.BytesIO(),
                timeout=0.2,
            )
            self.assertTrue(result["timed_out"])
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait()


class LeaseTests(unittest.TestCase):
    def test_cleanup_preserves_live_listeners_and_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "socket"
            path.write_text("personal file")
            with self.assertRaises(RuntimeError):
                remove_stale_socket(path)
            self.assertEqual(path.read_text(), "personal file")
            path.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen()
                with self.assertRaises(RuntimeError):
                    remove_stale_socket(path)
            remove_stale_socket(path)
            self.assertFalse(path.exists())

    def test_remote_lease_excludes_competitors_and_cleans_only_owned_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "runtime/socket"
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", REMOTE_LEASE, str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                self.assertEqual(json.loads(process.stdout.readline())["event"], "lease-ready")
                competitor = subprocess.run(
                    [sys.executable, "-u", "-c", REMOTE_LEASE, str(path)],
                    input=b"",
                    capture_output=True,
                    timeout=3,
                )
                self.assertNotEqual(competitor.returncode, 0)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(path))
                    listener.listen()
                    process.stdin.write(b"bound\n")
                    process.stdin.flush()
                    self.assertEqual(json.loads(process.stdout.readline())["event"], "bound")
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    process.stdin.write(b"close\n")
                    process.stdin.flush()
                    self.assertEqual(process.wait(timeout=3), 0)
                self.assertFalse(path.exists())
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()

    def test_frame_size_is_rejected_before_allocation(self):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(b"D" + struct.pack("!I", 2**32 - 1))
            with self.assertRaisesRegex(OperationError, "invalid-frame"):
                receive(right)


if __name__ == "__main__":
    unittest.main()
