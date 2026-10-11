import subprocess
import unittest
from unittest.mock import patch

from hermes_kubernetes.terminal.sync_status import report


class SyncStatusTests(unittest.TestCase):
    def test_reports_enabled_active_timer_and_running_sync(self):
        outputs = [
            "ActiveState=active\nUnitFileState=enabled\nLastTriggerUSec=yesterday\nNextElapseUSecRealtime=tomorrow\n",
            "ActiveState=activating\nResult=success\nExecMainStatus=0\n",
        ]
        with patch(
            "hermes_kubernetes.terminal.sync_status.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, outputs[0], ""),
                subprocess.CompletedProcess([], 0, outputs[1], ""),
            ],
        ) as run:
            result = report()
        self.assertIn("--user", run.call_args_list[0].args[0])
        self.assertIn("--user", run.call_args_list[1].args[0])
        self.assertTrue(result["ok"])
        self.assertTrue(result["timer"]["active"])
        self.assertTrue(result["timer"]["enabled"])
        self.assertTrue(result["service"]["running"])
        self.assertEqual(result["timer"]["next_trigger"], "tomorrow")

    def test_reports_inactive_timer_and_failed_service_explicitly(self):
        outputs = [
            "ActiveState=inactive\nUnitFileState=disabled\n",
            "ActiveState=failed\nResult=exit-code\nExecMainStatus=2\n",
        ]
        with patch(
            "hermes_kubernetes.terminal.sync_status.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, outputs[0], ""),
                subprocess.CompletedProcess([], 0, outputs[1], ""),
            ],
        ):
            result = report()
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["errors"]), 3)
        self.assertFalse(result["timer"]["active"])
        self.assertFalse(result["timer"]["enabled"])
        self.assertEqual(result["service"]["exit_code"], "2")

    def test_systemd_inspection_errors_are_reported_without_traceback(self):
        with patch(
            "hermes_kubernetes.terminal.sync_status.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            result = report()
        self.assertFalse(result["ok"])
        self.assertIn("FileNotFoundError", result["error"])


if __name__ == "__main__":
    unittest.main()
