"""Run the actual socket exporter payload, including its CLI guard."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from hermes_kubernetes.terminal.versioning import SocketRuntime

ROOT = Path(__file__).resolve().parents[2]


class SocketExportTests(unittest.TestCase):
    def test_capture_and_apply_payloads_do_not_run_exporter_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            home = directory / "home"
            home.mkdir()
            (home / "config.yaml").write_text("{}\n")

            class Client:
                def execute(self, command, stdout, stderr, **kwargs):
                    result = subprocess.run(
                        [sys.executable, *command[1:]],
                        env={**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(ROOT)},
                        capture_output=True,
                        timeout=30,
                    )
                    stdout.write(result.stdout)
                    stderr.write(result.stderr)
                    return {"returncode": result.returncode, "timed_out": False}

                def upload(self, source, destination, **kwargs):
                    shutil.copyfile(source, home / destination)

            runtime = SocketRuntime(directory / "project", directory / "unused.sock", bundle=ROOT)
            runtime.client = Client()
            snapshot = runtime.run("capture")
            self.assertIn("default", snapshot["profiles"])
            self.assertIn("customizations/default/config.json", snapshot["files"])
            self.assertEqual(runtime.run("apply", {})["applied"], 0)
            self.assertEqual(list((home / "state/versioning").glob("request-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
