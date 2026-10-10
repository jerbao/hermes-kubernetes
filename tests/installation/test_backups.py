"""Check scheduled backup selection with tiny local fixtures and no remote API calls."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from hermes_kubernetes.backup.archive import verify_archive
from hermes_kubernetes.installation.backups import WORKSPACE_SNAPSHOT, main
from hermes_kubernetes.installation.settings import BUNDLE


class ScheduledBackupTest(unittest.TestCase):
    def test_peripheral_selection_omits_unselected_cache_and_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            root = directory / "workspace"
            root.mkdir()
            for name, contents in {
                "customizations/default/skills/own/SKILL.md": "# Own skill\n",
                "projects/hello.py": 'print("hello")\n',
                ".cache/download.bin": "discardable cache",
                ".env": "TOKEN=private",
                "state.db": "not part of terminal peripheral selection",
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
            (root / "platform.lock.json").write_text("{}")
            (root / "hermes.py").write_text(
                "from pathlib import Path\ndef checkout(*args): return Path("
                + repr(str(BUNDLE))
                + ")\n"
            )
            archive = directory / "snapshot.tar.gz"
            with archive.open("wb") as output:
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        WORKSPACE_SNAPSHOT,
                        str(root),
                        json.dumps(["customizations", "projects"]),
                    ],
                    stdout=output,
                    check=True,
                )
            result = verify_archive(archive)
            self.assertEqual(result["consistency"], "stable-files-and-sqlite-per-database")
            with tarfile.open(archive) as reader:
                names = reader.getnames()
            self.assertTrue(any("SKILL.md" in name for name in names))
            self.assertTrue(any("hello.py" in name for name in names))
            for excluded in (".cache", ".env", "state.db"):
                self.assertFalse(any(excluded in name for name in names))

    def test_installation_does_not_trigger_an_immediate_backup(self):
        event = Mock()
        event.wait.return_value = True
        with (
            patch("hermes_kubernetes.installation.backups.threading.Event", return_value=event),
            patch("hermes_kubernetes.installation.backups.signal.signal"),
            patch("hermes_kubernetes.installation.backups.once") as backup,
            patch.dict("os.environ", {"BACKUP_INTERVAL_HOURS": "24"}),
        ):
            main()
        backup.assert_not_called()
        event.wait.assert_called_once_with(86400)

    def test_failure_is_visible_and_unsuccessful(self):
        event = Mock()
        event.wait.return_value = False
        output = io.StringIO()
        with (
            patch("hermes_kubernetes.installation.backups.threading.Event", return_value=event),
            patch("hermes_kubernetes.installation.backups.signal.signal"),
            patch(
                "hermes_kubernetes.installation.backups.once",
                side_effect=RuntimeError("secret diagnostics"),
            ),
            patch.dict("os.environ", {"BACKUP_INTERVAL_HOURS": "1", "BACKUP_KEEP": "6"}),
            patch("sys.stdout", output),
            self.assertRaises(SystemExit) as error,
        ):
            main()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(
            json.loads(output.getvalue()), {"event": "backup-failed", "reason": "RuntimeError"}
        )
