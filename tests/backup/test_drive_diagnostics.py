import unittest

from hermes_kubernetes.backup.drive import RemoteError, safe_failure_reason


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

    def test_remote_failure_reports_only_command_and_exit_code(self):
        error = RemoteError("lsjson", 7, b"private token and remote path")
        self.assertEqual(safe_failure_reason(error), "rclone_lsjson_exit_7")
        self.assertNotIn("private", safe_failure_reason(error))

    def test_unrecognized_exception_text_is_never_emitted(self):
        error = RuntimeError("secret path and credential must stay private")
        self.assertEqual(safe_failure_reason(error), "RuntimeError")


if __name__ == "__main__":
    unittest.main()
