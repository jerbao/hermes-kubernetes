import json
import os
import socket
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


from hermes_kubernetes.backup.state import MANIFEST, build_snapshot, digest
from hermes_kubernetes.backup.remote import retain_backups, verify_archive
from hermes_kubernetes.backup.drive import restore_state


class StateSnapshotTests(unittest.TestCase):
    def test_running_gateway_socket_is_transient_but_unexpected_fifo_is_not_silently_omitted(
        self,
    ):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            (home / "state").mkdir(parents=True)
            (home / "SOUL.md").write_text("personal state")
            control = socket.socket(socket.AF_UNIX)
            self.addCleanup(control.close)
            control.bind(str(home / "state/gateway.loop-tick.1162.sock"))
            archive = base / "state.tar.gz"
            build_snapshot(home, archive)
            self.assertEqual(verify_archive(archive)["files"], 1)
            self.assertTrue((home / "state/gateway.loop-tick.1162.sock").is_socket())
            with tarfile.open(archive) as reader:
                self.assertNotIn("state/gateway.loop-tick.1162.sock", reader.getnames())
            os.mkfifo(home / "unexpected-fifo")
            with self.assertRaises(RuntimeError):
                build_snapshot(home, base / "fifo.tar.gz")

    def test_profile_environment_alias_survives_restore_and_profile_packages_are_rebuilt(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            (home / "profiles/research").mkdir(parents=True)
            (home / "profiles/analyst/lsp").mkdir(parents=True)
            (home / "profiles/analyst/config.yaml").write_text("profile settings")
            (home / "profiles/analyst/lsp/generated").write_text("rebuild language server")
            (home / "profiles/research/.env").write_text("private fixture")
            (home / "profiles/research/.env").chmod(0o600)
            (home / "profiles/analyst/.env").symlink_to("../research/.env")
            archive = base / "state.tar.gz"
            build_snapshot(home, archive)
            self.assertEqual(verify_archive(archive)["symlinks"], 1)
            restored = base / "restored"
            restore_state(archive, restored, sha256=digest(archive))
            alias = restored / "profiles/analyst/.env"
            self.assertTrue(alias.is_symlink())
            (restored / "profiles/research/.env").write_text("updated shared environment")
            self.assertEqual(alias.read_text(), "updated shared environment")
            self.assertFalse((restored / "profiles/analyst/lsp").exists())
            self.assertEqual(
                (restored / "profiles/analyst/config.yaml").read_text(),
                "profile settings",
            )
            alias.unlink()
            alias.symlink_to("/etc/passwd")
            with self.assertRaises(RuntimeError):
                build_snapshot(restored, base / "unsafe.tar.gz")

    def test_runtime_home_alias_keeps_snapshots_bounded_and_other_symlinks_are_still_rejected(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            (home / "profiles/analyst").mkdir(parents=True)
            (home / "profiles/analyst/SOUL.md").write_text("profile content")
            (home / ".hermes").symlink_to(".", target_is_directory=True)
            output = base / "snapshot.tar.gz"
            result = build_snapshot(home, output)
            self.assertEqual(result["files"], 1)
            self.assertEqual(verify_archive(output)["files"], 1)
            with tarfile.open(output) as archive:
                self.assertEqual(
                    archive.extractfile("profiles/analyst/SOUL.md").read(),
                    b"profile content",
                )
                self.assertFalse(
                    any(m.name == ".hermes" or m.name.startswith(".hermes/") for m in archive)
                )
            (home / "personal-alias").symlink_to("profiles", target_is_directory=True)
            with self.assertRaises(RuntimeError):
                build_snapshot(home, base / "second.tar.gz")

    def test_restore_checks_all_databases_including_extensionless_plugins_and_wal(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            (home / "plugins/custom").mkdir(parents=True)
            (home / "skills/personal").mkdir(parents=True)
            (home / "skills/personal/SKILL.md").write_text("personal skill")
            (home / ".env").write_text("TOKEN=private fixture")
            (home / ".local/runtime").mkdir(parents=True)
            (home / ".local/runtime/transient").write_text("exclude")
            (home / ".local/functional-validation").mkdir()
            (home / ".local/functional-validation/previous-state.tar.gz").write_bytes(
                b"generated validation archive"
            )
            (home / ".local/plugin-state").mkdir()
            (home / ".local/plugin-state/retained").write_text("retain")
            databases = []
            for name in (
                "state.db",
                "plugins/custom/history",
                "plugins/custom/cache.sqlite3",
            ):
                database = sqlite3.connect(home / name)
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("CREATE TABLE committed (value TEXT)")
                database.execute("INSERT INTO committed VALUES ('live WAL commit')")
                database.commit()
                (home / name).chmod(0o600)
                databases.append(database)
            try:
                output = base / "state.tar.gz"
                result = build_snapshot(home, output)
                self.assertEqual(result["databases"], 3)
                self.assertEqual(verify_archive(output)["databases"], 3)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                restored = base / "restore"
                with tarfile.open(output) as archive:
                    archive.extractall(restored, filter="data")
                manifest = json.loads((restored / MANIFEST).read_text())
                self.assertEqual(manifest["consistency"], "sqlite-per-database")
                for entry in manifest["files"]:
                    path = restored / entry["path"]
                    self.assertEqual(entry["sha256"], digest(path))
                    if entry["sqlite"]:
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                        restored_db = sqlite3.connect(path)
                        try:
                            self.assertEqual(
                                restored_db.execute("PRAGMA quick_check").fetchone()[0],
                                "ok",
                            )
                            self.assertEqual(
                                restored_db.execute("SELECT value FROM committed").fetchone()[0],
                                "live WAL commit",
                            )
                        finally:
                            restored_db.close()
                self.assertFalse((restored / ".local/runtime").exists())
                self.assertFalse((restored / ".local/functional-validation").exists())
                self.assertTrue((restored / ".local/plugin-state/retained").exists())
                self.assertEqual(
                    (restored / "skills/personal/SKILL.md").read_text(),
                    "personal skill",
                )
            finally:
                for database in databases:
                    database.close()

    def test_failure_does_not_publish_partial_archive_or_overwrite_existing_backup(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            home.mkdir()
            (home / "linked").symlink_to(base / "missing")
            output = base / "state.tar.gz"
            with self.assertRaises(RuntimeError):
                build_snapshot(home, output)
            self.assertFalse(output.exists())
            self.assertFalse(list(base.glob(".hermes-archive-*")))
            output.write_text("existing backup")
            with self.assertRaises(FileExistsError):
                build_snapshot(home, output)
            self.assertEqual(output.read_text(), "existing backup")

    def test_retention_preserves_unrelated_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for day in (1, 2, 3):
                (directory / f"hermes-state-2026100{day}T120000Z-abcdefabcdef.tar.gz").write_text(
                    "fixture"
                )
            unrelated = directory / "personal.tar.gz"
            unrelated.write_text("personal")
            linked = directory / "hermes-state-20261004T120000Z-abcdefabcdef.tar.gz"
            linked.symlink_to(unrelated)
            retain_backups(directory, 2)
            self.assertTrue(unrelated.exists())
            self.assertTrue(linked.is_symlink())
            self.assertFalse(
                (directory / "hermes-state-20261001T120000Z-abcdefabcdef.tar.gz").exists()
            )


if __name__ == "__main__":
    unittest.main()
