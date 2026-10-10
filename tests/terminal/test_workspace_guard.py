import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

from hermes_kubernetes.backup.job import backup
from hermes_kubernetes.terminal.guard import guard

spec = importlib.util.spec_from_file_location(
    "guarded_workspace_sync", ROOT / "hermes_kubernetes/terminal/sync.py"
)
synchronization = importlib.util.module_from_spec(spec)
spec.loader.exec_module(synchronization)


class WorkspaceGuardTests(unittest.TestCase):
    def test_other_process_is_excluded_and_read_only_lock_contents_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "coordination.lock"
            with guard(path, create=True):
                program = "from hermes_kubernetes.terminal.guard import guard; import sys\ntry:\n with guard(sys.argv[1],blocking=False): sys.exit(1)\nexcept BlockingIOError: sys.exit(0)"
                result = subprocess.run(
                    [sys.executable, "-c", program, str(path)],
                    env={**os.environ, "PYTHONPATH": str(ROOT / "runtime")},
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            path.chmod(0o400)
            before = path.stat()
            with guard(path):
                self.assertEqual(path.read_bytes(), b"")
            after = path.stat()
            self.assertEqual(
                (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode),
                (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode),
            )

    def test_symlink_and_fifo_are_refused_without_modifying_the_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "private"
            target.write_text("preserved")
            path = base / "coordination.lock"
            path.symlink_to(target)
            with self.assertRaises(OSError), guard(path, create=True):
                self.fail("Symlink was accepted")
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(ValueError), guard(path):
                self.fail("FIFO was accepted")
            self.assertEqual(target.read_text(), "preserved")

    def test_sync_skips_git_while_backup_owns_the_coordination_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                guard(root / "state/workspace-sync.lock", create=True),
                patch.object(
                    synchronization,
                    "git",
                    side_effect=AssertionError("Git must not run"),
                ),
            ):
                self.assertEqual(synchronization.sync(root), "workspace-busy")

    def test_missing_configured_lock_refuses_snapshot_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            source = base / "home"
            source.mkdir()
            (source / "file").write_text("preserved")
            with self.assertRaises(FileNotFoundError):
                backup(
                    source,
                    base / "backups",
                    kind="workspace",
                    workspace_lock=source / "missing.lock",
                )
            self.assertFalse(list((base / "backups").glob("*.tar.gz")))

    def test_workspace_environment_does_not_change_state_backup_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            source = base / "state"
            source.mkdir()
            (source / "SOUL.md").write_text("preserved personality")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "hermes_kubernetes/backup/job.py"),
                    "--kind",
                    "state",
                    "--source",
                    str(source),
                    "--directory",
                    str(base / "backups"),
                ],
                env={**os.environ, "HERMES_WORKSPACE_LOCK": str(base / "missing.lock")},
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["files"], 1)
