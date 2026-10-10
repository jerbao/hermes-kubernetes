"""Real descriptor/cache advice, preserving backup bytes and unrelated files."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from hermes_kubernetes.backup.cache import bounded_cache, open_read, release


@unittest.skipUnless(
    Path("/proc/self/fd").is_dir() and hasattr(os, "posix_fadvise"),
    "Linux page-cache advice",
)
class BackupCacheTests(unittest.TestCase):
    def test_live_source_writers_are_never_flushed_by_read_advice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            staging = root / "staging"
            staging.mkdir()
            with (source / "live.db").open("wb+") as writer:
                writer.write(b"live writer")
                writer.flush()
                with patch("hermes_kubernetes.backup.cache.os.fdatasync") as flush:
                    release((staging.resolve(),), (source.resolve(),))
                    flush.assert_not_called()
                    with open_read(source / "live.db") as reader:
                        self.assertEqual(reader.read(), b"live writer")
                    flush.assert_not_called()
                writer.seek(0)
                self.assertEqual(writer.read(), b"live writer")

    def test_random_sqlite_reader_is_not_repeatedly_invalidated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "verify.sqlite3"
            path.write_bytes(b"x" * 1048576)
            with (
                path.open("rb") as reader,
                patch("hermes_kubernetes.backup.cache.os.posix_fadvise") as advise,
            ):
                self.assertEqual(os.pread(reader.fileno(), 1, 512), b"x")
                release((root.resolve(),))
                advise.assert_not_called()

    def test_only_explicit_staging_descriptors_are_advised_and_data_survives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            data = b"private-backup" * 100000
            with (
                (staging / "archive").open("wb+") as target,
                (root / "unrelated").open("wb+") as unrelated,
            ):
                target.write(data)
                target.flush()
                unrelated.write(b"live data")
                unrelated.flush()
                advised = []
                original = os.posix_fadvise

                def observe(fd, *args):
                    advised.append(Path("/proc/self/fd", str(fd)).resolve())
                    return original(fd, *args)

                with patch(
                    "hermes_kubernetes.backup.cache.os.posix_fadvise",
                    side_effect=observe,
                ):
                    self.assertGreater(release((staging.resolve(),)), 0)
                self.assertEqual(set(advised), {staging / "archive"})
                target.seek(0)
                self.assertEqual(
                    hashlib.sha256(target.read()).digest(),
                    hashlib.sha256(data).digest(),
                )
                unrelated.seek(0)
                self.assertEqual(unrelated.read(), b"live data")

    def test_scope_handles_child_writer_and_preserves_body_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    'import pathlib,sys; f=pathlib.Path(sys.argv[1]).open("wb"); f.write(b"x"*1048576); f.flush(); print("ready",flush=True); sys.stdin.readline(); f.close()',
                    str(root / "download"),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                self.assertGreater(release((root.resolve(),)), 0)
                with self.assertRaisesRegex(RuntimeError, "original failure"):
                    with bounded_cache(root, interval=0.01):
                        raise RuntimeError("original failure")
            finally:
                child.communicate("\n", timeout=10)
            self.assertEqual((root / "download").stat().st_size, 1048576)


if __name__ == "__main__":
    unittest.main()
