import io
import json
from contextlib import redirect_stderr, redirect_stdout
import time
import unittest

from hermes_kubernetes.backup.progress import ProgressReporter


class ProgressReporterTests(unittest.TestCase):
    def test_default_output_is_stderr_and_does_not_pollute_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            progress = ProgressReporter(interval=60)
            progress.finish()
            progress.close()

        self.assertEqual(stdout.getvalue(), "")
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual(events[0]["phase"], "starting")
        self.assertEqual(events[-1]["phase"], "done")

    def test_json_heartbeats_track_aggregate_counts_and_filter_rclone_output(self):
        output = io.StringIO()
        progress = ProgressReporter(stream=output, interval=0.01)
        progress.phase("scan")
        progress.scanned_entry()
        progress.scan_complete(3)
        progress.start_capture(entries=3, files=1)
        progress.bytes_read(4096)
        progress.file_processed()
        progress.entry_processed()
        progress.phase("upload")
        progress.transfer_stats("Transferred: 1.0 MiB / 2.0 MiB, 50%, 1.0 MiB/s, ETA 1s")
        progress.transfer_stats("Transferred private/path with token=secret")
        time.sleep(0.025)
        progress.finish()
        progress.close()

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(event["event"] == "heartbeat" for event in events))
        self.assertEqual(events[-1]["phase"], "done")
        self.assertTrue(
            any(
                event["event"] == "transfer_progress"
                and event["percent"] == "50"
                and event["done"] == "1.0 MiB"
                for event in events
            )
        )
        self.assertTrue(any(event["files_processed"] == 1 for event in events))
        self.assertTrue(any(event["bytes_processed"] == 4096 for event in events))
        self.assertNotIn("private/path", output.getvalue())
        self.assertNotIn("secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
