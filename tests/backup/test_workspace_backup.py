from contextlib import closing
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import hermes_kubernetes.backup.archive as backup_archive
import hermes_kubernetes.backup.job as backup_job
from hermes_kubernetes.backup.restore import restore
from hermes_kubernetes.backup.state import MANIFEST, digest
import hermes_kubernetes.backup.workspace as workspace_snapshot


class WorkspaceBackupTests(unittest.TestCase):
    def test_full_home_restores_git_work_browser_wal_modes_and_links_without_following_them(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, backups, destination = (
                base / "home",
                base / "backups",
                base / "restored",
            )
            project = home / ".hermes"
            project.mkdir(parents=True)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(project), *args], stderr=subprocess.DEVNULL
                )

            git("init", "-q")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.invalid")
            (project / "README.md").write_text("committed")
            git("add", "README.md")
            git("commit", "-qm", "fixture")
            git("checkout", "-qb", "local-only")
            (project / "README.md").write_text("uncommitted")
            (project / "staged").write_text("staged")
            git("add", "staged")
            (project / "untracked").write_text("untracked")
            (project / ".hermes").symlink_to(".")
            for name in [
                ".venv/bin",
                "skills/personal",
                ".cache/browser/Default",
                "empty",
            ]:
                (home / name).mkdir(parents=True)
            (home / ".venv/bin/python").symlink_to("/usr/bin/python3")
            external = base / "external"
            external.write_text("do not touch")
            (home / "external-alias").symlink_to(external)
            (home / "dangling").symlink_to("missing")
            for name in [
                "skills/personal/SKILL.md",
                "package-lock.json",
                "personal.lock",
                "personal.bak",
                ".cache/browser/Default/localStorage",
                ".cache/browser/Local State",
                "not-a-db-wal",
            ]:
                (home / name).write_text("preserve")
            (home / ".cache/browser/SingletonSocket").symlink_to("/tmp/transient-browser-socket")
            (home / ".ssh").mkdir(mode=0o700)
            key = home / ".ssh/id_ed25519"
            key.write_text("private fixture")
            key.chmod(0o600)
            (home / "empty").chmod(0o2700)
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(home / "runtime.sock"))
            database = sqlite3.connect(home / ".cache/browser/Default/Cookies")
            try:
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("CREATE TABLE cookies(value TEXT)")
                database.execute("INSERT INTO cookies VALUES ('live committed cookie')")
                database.commit()
                status = backup_job.backup(home, backups, kind="workspace")
                archive = backups / status["archive"]
                self.assertTrue(archive.name.startswith("hermes-workspace-"))
                self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
                restored = restore(archive, destination, sha256=status["sha256"])
                self.assertEqual(restored["databases"], 1)
                self.assertEqual(restored["symlinks"], 4)
                restored_git = ["git", "-C", str(destination / ".hermes")]
                self.assertEqual(
                    subprocess.check_output([*restored_git, "branch", "--show-current"]).strip(),
                    b"local-only",
                )
                self.assertEqual(
                    subprocess.check_output([*restored_git, "status", "--porcelain"]),
                    git("status", "--porcelain"),
                )
                self.assertEqual((destination / ".ssh/id_ed25519").stat().st_mode & 0o777, 0o600)
                self.assertEqual((destination / "empty").stat().st_mode & 0o7777, 0o2700)
                self.assertEqual(os.readlink(destination / "external-alias"), str(external))
                self.assertEqual(external.read_text(), "do not touch")
                with closing(
                    sqlite3.connect(destination / ".cache/browser/Default/Cookies")
                ) as copy:
                    self.assertEqual(
                        copy.execute("SELECT value FROM cookies").fetchone()[0],
                        "live committed cookie",
                    )
                for name in [
                    "personal.lock",
                    "personal.bak",
                    "not-a-db-wal",
                    ".cache/browser/Default/localStorage",
                ]:
                    self.assertEqual((destination / name).read_text(), "preserve")
                self.assertFalse((destination / "runtime.sock").exists())
                self.assertFalse((destination / ".cache/browser/SingletonSocket").is_symlink())
                with self.assertRaises(FileExistsError):
                    restore(archive, destination, sha256=status["sha256"])
            finally:
                sock.close()
                database.close()

    def test_changed_file_after_it_was_copied_rejects_backup_and_preserves_previous_copy(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, backups = base / "home", base / "backups"
            home.mkdir()
            (home / "first").write_text("first")
            (home / "second").write_text("second")
            status = backup_job.backup(home, backups, kind="workspace")
            previous = (backups / "latest.json").read_bytes()
            attempts = 0

            def change_earlier_file(path):
                nonlocal attempts
                if path.name == "second":
                    attempts += 1
                    (home / "first").write_text("changed after copying")
                return digest(path)

            with patch.object(workspace_snapshot, "digest", side_effect=change_earlier_file):
                with self.assertRaises(RuntimeError):
                    backup_job.backup(home, backups, keep=1, kind="workspace")
            self.assertTrue((backups / status["archive"]).exists())
            self.assertEqual((backups / "latest.json").read_bytes(), previous)
            self.assertEqual(len(list(backups.glob("*.tar.gz"))), 1)
            self.assertEqual(attempts, 2)

    def test_transient_workspace_change_retries_once_and_publishes_consistent_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, backups = base / "home", base / "backups"
            home.mkdir()
            (home / "first").write_text("first")
            (home / "second").write_text("second")
            real_digest = digest
            changed = False

            def change_once(path):
                nonlocal changed
                value = real_digest(path)
                if path.name == "second" and not changed:
                    (home / "first").write_text("updated before retry")
                    changed = True
                return value

            with patch.object(workspace_snapshot, "digest", side_effect=change_once):
                status = backup_job.backup(home, backups, kind="workspace")

            self.assertTrue(changed)
            self.assertTrue((backups / status["archive"]).is_file())
            restored = base / "restored"
            restore(backups / status["archive"], restored, sha256=status["sha256"])
            self.assertEqual((restored / "first").read_text(), "updated before retry")
            self.assertEqual((restored / "second").read_text(), "second")

    def test_untyped_runtime_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            home.mkdir()
            (home / "file").write_text("fixture")
            with patch.object(
                workspace_snapshot,
                "build_snapshot",
                side_effect=RuntimeError("unclassified failure"),
            ) as build:
                with self.assertRaisesRegex(RuntimeError, "unclassified failure"):
                    backup_job.backup(home, base / "backups", kind="workspace")
            build.assert_called_once()

    def test_retention_for_workspace_never_deletes_state_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, backups = base / "home", base / "backups"
            home.mkdir()
            (home / "SOUL.md").write_text("fixture")
            state = backup_job.backup(home, backups)
            first = backup_job.backup(home, backups, kind="workspace", keep=1)
            second = backup_job.backup(home, backups, kind="workspace", keep=1)
            self.assertTrue((backups / state["archive"]).exists())
            self.assertFalse((backups / first["archive"]).exists())
            self.assertTrue((backups / second["archive"]).exists())

    def test_exclusively_locked_idle_database_recovers_from_private_files_without_source_sqlite_locks(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            home.mkdir()
            db = sqlite3.connect(home / "browser-cache.db")
            try:
                db.execute("CREATE TABLE cache(value TEXT)")
                db.execute("INSERT INTO cache VALUES ('committed')")
                db.commit()
                db.execute("BEGIN EXCLUSIVE")
                status = backup_job.backup(home, base / "backups", kind="workspace")
                restore(
                    base / "backups" / status["archive"],
                    base / "restored",
                    sha256=status["sha256"],
                )
                with closing(sqlite3.connect(base / "restored/browser-cache.db")) as copy:
                    self.assertEqual(
                        copy.execute("SELECT value FROM cache").fetchone()[0],
                        "committed",
                    )
                self.assertTrue(db.in_transaction, "Backup must not disturb the original writer")
            finally:
                db.close()

    def test_wal_commit_during_workspace_copy_is_rejected_without_touching_existing_backups(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            home.mkdir()
            db = sqlite3.connect(home / "cookies")
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE cookies(value TEXT)")
                db.execute("INSERT INTO cookies VALUES ('before')")
                db.commit()
                status = backup_job.backup(home, base / "backups", kind="workspace")
                original = workspace_snapshot.sqlite_snapshot

                def commit_during_private_backup(source, destination):
                    result = original(source, destination)
                    db.execute("INSERT INTO cookies VALUES ('during copy')")
                    db.commit()
                    return result

                with patch.object(
                    workspace_snapshot,
                    "sqlite_snapshot",
                    side_effect=commit_during_private_backup,
                ):
                    with self.assertRaises(RuntimeError):
                        backup_job.backup(home, base / "backups", keep=1, kind="workspace")
                self.assertTrue((base / "backups" / status["archive"]).exists())
                self.assertEqual(len(list((base / "backups").glob("*.tar.gz"))), 1)
            finally:
                db.close()

    def test_missing_catalogued_wal_retries_without_touching_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            home.mkdir()
            database_path = home / "cookies.db"
            db = sqlite3.connect(database_path)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("CREATE TABLE cookies(value TEXT)")
                db.execute("INSERT INTO cookies VALUES ('preserve')")
                db.commit()
                wal = Path(str(database_path) + "-wal")
                self.assertTrue(wal.exists())
                original_copy = workspace_snapshot.shutil.copyfile
                failed_once = False

                def transient_missing_sidecar(source, destination):
                    nonlocal failed_once
                    if str(source) == str(wal) and not failed_once:
                        failed_once = True
                        raise FileNotFoundError(str(source))
                    return original_copy(source, destination)

                with patch.object(
                    workspace_snapshot.shutil,
                    "copyfile",
                    side_effect=transient_missing_sidecar,
                ):
                    status = backup_job.backup(home, base / "backups", kind="workspace")

                self.assertTrue(failed_once)
                self.assertTrue(wal.exists())
                restore(
                    base / "backups" / status["archive"],
                    base / "restored",
                    sha256=status["sha256"],
                )
                with closing(sqlite3.connect(base / "restored/cookies.db")) as restored:
                    self.assertEqual(
                        restored.execute("SELECT value FROM cookies").fetchone()[0],
                        "preserve",
                    )
            finally:
                db.close()

    def test_unsafe_link_ancestor_and_manifest_metadata_mismatch_are_rejected_before_restore(
        self,
    ):
        for attack in ["ancestor", "metadata"]:
            with (
                self.subTest(attack=attack),
                tempfile.TemporaryDirectory() as directory,
            ):
                base = Path(directory)
                archive_path = base / "unsafe.tar.gz"
                with tarfile.open(archive_path, "w:gz") as archive:
                    info = tarfile.TarInfo("alias")
                    info.type, info.linkname = tarfile.SYMTYPE, "/tmp/external-fixture"
                    archive.addfile(info)
                    entries = [workspace_snapshot.record(info)]
                    if attack == "ancestor":
                        info = tarfile.TarInfo("alias/overwrite")
                        info.size = 1
                        archive.addfile(info, io.BytesIO(b"x"))
                        import hashlib

                        entries.append(
                            workspace_snapshot.record(
                                info, checksum=hashlib.sha256(b"x").hexdigest()
                            )
                        )
                    else:
                        entries[0]["target"] = "/different-target"
                    payload = json.dumps(
                        {
                            "schema": 2,
                            "kind": "workspace",
                            "consistency": "stable-files-and-sqlite-per-database",
                            "entries": entries,
                        }
                    ).encode()
                    info = tarfile.TarInfo(MANIFEST)
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                with self.assertRaises(ValueError):
                    restore(archive_path, base / "restored", sha256=digest(archive_path))
                self.assertFalse((base / "restored").exists())

    def test_directory_listing_failure_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            with patch.object(Path, "iterdir", side_effect=PermissionError("fixture")):
                with self.assertRaises(PermissionError):
                    workspace_snapshot.build_snapshot(home, Path(directory) / "backup.tar.gz")
