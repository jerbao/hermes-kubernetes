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

    def test_earlier_file_change_during_later_copy_keeps_the_stable_spooled_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, backups = base / "home", base / "backups"
            home.mkdir()
            (home / "first").write_text("first bytes")
            (home / "second").write_text("second bytes")
            previous = backup_job.backup(home, backups, keep=2, kind="workspace")
            original_copy = workspace_snapshot._copy_stable_file
            changed = False

            def change_first_after_second(path, destination, expected):
                nonlocal changed
                result = original_copy(path, destination, expected)
                if path.name == "second" and not changed:
                    (home / "first").write_text("changed after first was spooled")
                    changed = True
                return result

            with patch.object(
                workspace_snapshot,
                "_copy_stable_file",
                side_effect=change_first_after_second,
            ):
                current = backup_job.backup(home, backups, keep=2, kind="workspace")

            self.assertTrue(changed)
            self.assertTrue((backups / previous["archive"]).exists())
            restored = base / "restored"
            restore(
                backups / current["archive"],
                restored,
                sha256=current["sha256"],
            )
            self.assertEqual((restored / "first").read_text(), "first bytes")
            self.assertEqual((restored / "second").read_text(), "second bytes")

    def test_file_changed_after_inventory_is_captured_from_its_fresh_version(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, backups = base / "home", base / "backups"
            home.mkdir()
            changing = home / "first"
            changing.write_text("catalogued bytes")
            original_catalogue = workspace_snapshot.catalogue
            changed = False
            catalogues = 0

            def update_after_inventory(directory, policy=None):
                nonlocal changed, catalogues
                entries = original_catalogue(directory, policy)
                catalogues += 1
                if not changed:
                    changing.write_text("updated before entry capture")
                    changed = True
                return entries

            with patch.object(
                workspace_snapshot,
                "catalogue",
                side_effect=update_after_inventory,
            ):
                status = backup_job.backup(home, backups, kind="workspace")

            self.assertEqual(catalogues, 1)
            restored = base / "restored"
            restore(
                backups / status["archive"],
                restored,
                sha256=status["sha256"],
            )
            self.assertEqual((restored / "first").read_text(), "updated before entry capture")

    def test_database_changed_before_its_entry_capture_uses_fresh_recovery_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, backups = base / "home", base / "backups"
            home.mkdir()
            (home / "a-first").write_text("fixture")
            database_path = home / "z-database"
            database = sqlite3.connect(database_path)
            database.execute("CREATE TABLE values_table (value TEXT)")
            database.execute("INSERT INTO values_table VALUES ('catalogued')")
            database.commit()
            original_copy = workspace_snapshot._copy_stable_file
            committed = False

            def commit_before_database_capture(path, destination, expected):
                nonlocal committed
                result = original_copy(path, destination, expected)
                if path.name == "a-first" and not committed:
                    database.execute("INSERT INTO values_table VALUES ('updated before capture')")
                    database.commit()
                    committed = True
                return result

            try:
                with patch.object(
                    workspace_snapshot,
                    "_copy_stable_file",
                    side_effect=commit_before_database_capture,
                ):
                    status = backup_job.backup(home, backups, kind="workspace")
            finally:
                database.close()

            restored = base / "restored"
            restore(
                backups / status["archive"],
                restored,
                sha256=status["sha256"],
            )
            with closing(sqlite3.connect(restored / "z-database")) as copy:
                self.assertEqual(
                    copy.execute("SELECT value FROM values_table").fetchall(),
                    [("catalogued",), ("updated before capture",)],
                )

    def test_file_change_during_its_own_capture_retries_and_publishes_new_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, backups = base / "home", base / "backups"
            home.mkdir()
            changing = home / "first"
            changing.write_text("original bytes")
            (home / "second").write_text("second bytes")
            original_read = workspace_snapshot._read_chunk
            changed = False

            def change_during_read(stream):
                nonlocal changed
                chunk = original_read(stream)
                if not changed:
                    changing.write_text("updated while its fd was being captured")
                    changed = True
                return chunk

            with patch.object(workspace_snapshot, "_read_chunk", side_effect=change_during_read):
                status = backup_job.backup(home, backups, kind="workspace")

            self.assertTrue(changed)
            restored = base / "restored"
            restore(
                backups / status["archive"],
                restored,
                sha256=status["sha256"],
            )
            self.assertEqual(
                (restored / "first").read_text(),
                "updated while its fd was being captured",
            )
            self.assertEqual((restored / "second").read_text(), "second bytes")

    def test_repeated_mid_read_changes_preserve_the_previous_verified_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, backups = base / "home", base / "backups"
            home.mkdir()
            changing = home / "first"
            changing.write_text("initial bytes")
            previous = backup_job.backup(home, backups, kind="workspace")
            latest_before = (backups / "latest.json").read_bytes()
            original_read = workspace_snapshot._read_chunk
            mutations = 0

            def change_during_read(stream):
                nonlocal mutations
                chunk = original_read(stream)
                mutations += 1
                changing.write_text(f"concurrent update {mutations}")
                return chunk

            with patch.object(
                workspace_snapshot,
                "_read_chunk",
                side_effect=change_during_read,
            ):
                with self.assertRaises(RuntimeError):
                    backup_job.backup(home, backups, keep=1, kind="workspace")

            self.assertGreaterEqual(mutations, 2)
            self.assertEqual((backups / "latest.json").read_bytes(), latest_before)
            self.assertTrue((backups / previous["archive"]).is_file())
            self.assertEqual(len(list(backups.glob("*.tar.gz"))), 1)

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

    def test_sqlite_generated_private_wal_is_removed_before_next_database(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home, staging = base / "home", base / "staging"
            home.mkdir()
            staging.mkdir()

            first = home / "first.db"
            with closing(sqlite3.connect(first)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE values_table(value)")
                db.commit()
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.assertFalse(Path(str(first) + "-wal").exists())

            second = home / "second.db"
            writer = sqlite3.connect(second)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE values_table(value)")
                writer.execute("INSERT INTO values_table VALUES ('active')")
                writer.commit()
                self.assertTrue(Path(str(second) + "-wal").exists())

                status = backup_job.backup(home, base / "backups", kind="workspace")
                self.assertEqual(status["databases"], 2)

                first_copy = workspace_snapshot._capture_sqlite_sources(
                    home, first.name, first.lstat(), staging
                )
                first_copy.unlink()
                self.assertFalse((staging / "source.sqlite3-wal").exists())

                second_copy = workspace_snapshot._capture_sqlite_sources(
                    home, second.name, second.lstat(), staging
                )
                self.assertTrue(second_copy.is_file())
                second_copy.unlink()
                self.assertEqual(list(staging.iterdir()), [])
            finally:
                writer.close()

    def test_wal_commit_after_private_database_capture_keeps_valid_archive(self):
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
                original = workspace_snapshot.sqlite_snapshot

                def commit_after_private_capture(source, destination):
                    result = original(source, destination)
                    db.execute("INSERT INTO cookies VALUES ('during copy')")
                    db.commit()
                    return result

                with patch.object(
                    workspace_snapshot,
                    "sqlite_snapshot",
                    side_effect=commit_after_private_capture,
                ):
                    status = backup_job.backup(home, base / "backups", kind="workspace")

                restore(
                    base / "backups" / status["archive"],
                    base / "restored",
                    sha256=status["sha256"],
                )
                with closing(sqlite3.connect(base / "restored/cookies")) as restored:
                    self.assertEqual(
                        restored.execute("SELECT value FROM cookies").fetchall(),
                        [("before",)],
                    )
                self.assertEqual(
                    db.execute("SELECT value FROM cookies").fetchall(),
                    [("before",), ("during copy",)],
                )
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
                original_open = workspace_snapshot.os.open
                failed_once = False

                def transient_missing_sidecar(path, flags, *args, **kwargs):
                    nonlocal failed_once
                    if str(path) == str(wal) and not failed_once:
                        failed_once = True
                        raise FileNotFoundError(str(path))
                    return original_open(path, flags, *args, **kwargs)

                with patch.object(
                    workspace_snapshot.os,
                    "open",
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
