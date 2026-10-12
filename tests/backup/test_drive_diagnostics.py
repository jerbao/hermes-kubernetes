import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_kubernetes.backup.drive import Remote, RemoteError, safe_failure_reason


class BackupFailureDiagnosticsTests(unittest.TestCase):
    def test_workspace_consistency_failures_have_safe_actionable_codes(self):
        self.assertEqual(
            safe_failure_reason(RuntimeError("Workspace changed during snapshot; retry")),
            "workspace_file_changed_during_snapshot",
        )
        self.assertEqual(
            safe_failure_reason(RuntimeError("Workspace paths changed during snapshot; retry")),
            "workspace_paths_changed_during_snapshot",
        )
        self.assertEqual(
            safe_failure_reason(
                RuntimeError("SQLite recovery data changed during snapshot; retry")
            ),
            "workspace_sqlite_recovery_changed_during_snapshot",
        )
        self.assertEqual(
            safe_failure_reason(RuntimeError("Personal file changed during snapshot; retry")),
            "state_file_changed_during_snapshot",
        )
        self.assertEqual(
            safe_failure_reason(RuntimeError("SQLite snapshot timed out")),
            "sqlite_snapshot_timed_out",
        )

    def test_remote_failure_reports_only_command_and_exit_code(self):
        error = RemoteError("lsjson", 7, b"private token and remote path")
        self.assertEqual(safe_failure_reason(error), "rclone_lsjson_exit_7")
        self.assertNotIn("private", safe_failure_reason(error))

    def test_file_exists_failures_use_safe_specific_or_generic_codes(self):
        self.assertEqual(
            safe_failure_reason(FileExistsError("Never overwrite an existing remote snapshot")),
            "remote_snapshot_already_exists",
        )
        self.assertEqual(
            safe_failure_reason(FileExistsError("Snapshot output already exists")),
            "snapshot_output_already_exists",
        )
        self.assertEqual(
            safe_failure_reason(FileExistsError("/private/workspace/name.tar.gz")),
            "backup_path_already_exists",
        )
        self.assertEqual(
            safe_failure_reason(RuntimeError("Local backup path already exists")),
            "local_backup_path_already_exists",
        )
        self.assertEqual(
            safe_failure_reason(RuntimeError("Remote backup path already exists")),
            "remote_backup_path_already_exists",
        )

    def test_failure_context_names_only_the_project_frame(self):
        with tempfile.TemporaryDirectory(prefix="private-path-") as temporary:
            archive = Path(temporary) / ("hermes-workspace-20261012T093024Z-012345abcdef.tar.gz")
            archive.write_bytes(b"fixture")
            remote = object.__new__(Remote)
            remote.remote = "hermes-backup:"

            def run(command, *args, **kwargs):
                if command == "lsjson":
                    return json.dumps(
                        [{"Name": archive.name.removesuffix(".tar.gz"), "IsDir": True}]
                    )
                return ""

            remote.run = run
            with patch(
                "hermes_kubernetes.backup.drive.verify_archive",
                return_value={"consistency": "stable-files-and-sqlite-per-database"},
            ):
                try:
                    remote._upload(archive, "workspace", keep=6, kind="workspace")
                except FileExistsError as error:
                    reason = safe_failure_reason(error)
                else:
                    self.fail("the duplicate snapshot fixture did not raise")

        self.assertIn(
            "remote_snapshot_already_exists@hermes_kubernetes/backup/drive.py:_upload:", reason
        )
        self.assertNotIn("private-path-", reason)

    def test_os_error_reports_errno_without_path_or_message(self):
        try:
            raise FileExistsError(errno.EEXIST, "private path", "/secret/profile")
        except FileExistsError as error:
            reason = safe_failure_reason(error)
        self.assertEqual(reason, "backup_path_already_exists:errno_17")
        self.assertNotIn("/secret", reason)
        self.assertNotIn("private path", reason)

    def test_unrecognized_exception_text_is_never_emitted(self):
        error = RuntimeError("secret path and credential must stay private")
        self.assertEqual(safe_failure_reason(error), "RuntimeError")


if __name__ == "__main__":
    unittest.main()
