from datetime import datetime, timezone
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import hermes_kubernetes.backup.job as backup_job


class BackupJobTests(unittest.TestCase):
    def test_retention_keeps_the_newest_backup_when_uuid_names_share_a_second(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            older = directory / "hermes-state-20261008T120000Z-ffffffffffff.tar.gz"
            newer = directory / "hermes-state-20261008T120000Z-000000000000.tar.gz"
            for path, timestamp in [(older, 1), (newer, 2)]:
                path.write_bytes(b"fixture")
                os.utime(path, ns=(timestamp, timestamp))
            backup_job.retain_backups(directory, 1)
            self.assertTrue(newer.exists())
            self.assertFalse(older.exists())

    def test_verified_backups_preserve_live_wal_and_retention_never_runs_on_failure(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            source, destination = base / "state", base / "backups"
            source.mkdir()
            (source / "SOUL.md").write_text("personal content")
            database = sqlite3.connect(source / "state.db")
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("CREATE TABLE sessions (body TEXT)")
            (source / "state.db").chmod(0o600)
            try:
                archives = []
                for day in [1, 2, 3]:
                    database.execute("INSERT INTO sessions VALUES (?)", (f"committed {day}",))
                    database.commit()
                    with patch.object(backup_job, "datetime") as clock:
                        clock.now.return_value = datetime(2026, 10, day, tzinfo=timezone.utc)
                        status = backup_job.backup(source, destination, keep=2)
                    archives.append(destination / status["archive"])
                    self.assertEqual(status["databases"], 1)
                    self.assertEqual(archives[-1].stat().st_mode & 0o777, 0o600)
                self.assertFalse(archives[0].exists())
                self.assertTrue(all(p.exists() for p in archives[1:]))
                self.assertEqual(json.loads((destination / "latest.json").read_text()), status)
                before = (destination / "latest.json").read_bytes()
                with patch.object(
                    backup_job,
                    "verify_archive",
                    side_effect=ValueError("corrupt fixture"),
                ):
                    with self.assertRaises(ValueError):
                        backup_job.backup(source, destination, keep=1)
                self.assertTrue(all(p.exists() for p in archives[1:]))
                self.assertEqual((destination / "latest.json").read_bytes(), before)
                self.assertFalse(list(destination.glob(".receiving-*")))
                restored = base / "restored"
                with tarfile.open(archives[-1]) as archive:
                    archive.extractall(restored, filter="data")
                with closing(sqlite3.connect(restored / "state.db")) as copy:
                    self.assertEqual(copy.execute("SELECT count(*) FROM sessions").fetchone()[0], 3)
                self.assertEqual((restored / "state.db").stat().st_mode & 0o777, 0o600)
                self.assertEqual((restored / "SOUL.md").read_text(), "personal content")
            finally:
                database.close()

    def test_competing_writer_and_backup_inside_source_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            source, destination = base / "state", base / "backups"
            source.mkdir()
            destination.mkdir()
            with self.assertRaises(ValueError):
                backup_job.backup(source, source / "backups")
            with (destination / "backup.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    backup_job.backup(source, destination)
            self.assertFalse(list(destination.glob("*.tar.gz")))
