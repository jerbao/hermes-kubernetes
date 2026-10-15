"""Private edits must not prevent adoption of the release already running remotely."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hermes_kubernetes.instances import cli
from hermes_kubernetes.terminal import versioning


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.origin, self.root, self.other = [base / name for name in ("origin", "local", "other")]
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.root)],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        git(self.root, "switch", "-c", "main")
        self.identity(self.root)
        self.revision = git(cli.BUNDLE, "rev-parse", "HEAD")
        (self.root / "project-files.json").write_text(
            json.dumps({"schema": 1, "runtime_files": [], "skills": []})
        )
        (self.root / ".gitignore").write_text("/.local/\n/state/\n")
        (self.root / "platform.lock.json").write_text(json.dumps({"revision": self.revision}))
        self.skill = "customizations/default/skills/own/SKILL.md"
        path = self.root / self.skill
        path.parent.mkdir(parents=True)
        path.write_text("original instructions")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "initial")
        git(self.root, "push", "-qu", "origin", "main")
        subprocess.run(
            ["git", "clone", "-q", "--branch", "main", str(self.origin), str(self.other)],
            check=True,
        )
        self.identity(self.other)
        (self.root / self.skill).write_text("Nyra's unpublished instructions")

    @staticmethod
    def identity(root):
        git(root, "config", "user.name", "fixture")
        git(root, "config", "user.email", "fixture@example.test")

    def incoming(self, *, conflict=False):
        if conflict:
            (self.other / self.skill).write_text("independently reviewed instructions")
        else:
            (self.other / "platform.lock.json").write_text(json.dumps({"revision": "f" * 40}))
            plugin = self.other / "customizations/default/plugins/new/plugin.py"
            plugin.parent.mkdir(parents=True)
            plugin.write_text("# newly reviewed plugin\n")
        git(self.other, "add", ".")
        git(self.other, "commit", "-qm", "reviewed remote changes")
        git(self.other, "push", "-q", "origin", "main")

    def synchronize(self):
        output = StringIO()
        original = versioning.publish
        with (
            patch.object(
                versioning, "publish", side_effect=lambda root: original(root, quiet_seconds=0)
            ),
            patch.object(
                versioning,
                "reconcile",
                return_value={"status": "waiting-for-runtime", "profiles": {}},
            ) as reconcile,
            redirect_stdout(output),
        ):
            code = cli.synchronize(self.root, True)
        return code, json.loads(output.getvalue().splitlines()[-1]), reconcile

    def test_dirty_personalization_does_not_block_new_distribution_or_plugin(self):
        self.incoming()
        code, result, reconcile = self.synchronize()
        self.assertEqual(result["status"], "reload-distribution")
        self.assertEqual(code, 0)
        reconcile.assert_not_called()
        self.assertEqual(
            json.loads((self.root / "platform.lock.json").read_text())["revision"], "f" * 40
        )
        self.assertEqual(
            git(self.origin, "show", "main:" + self.skill), "Nyra's unpublished instructions"
        )
        self.assertTrue((self.root / "customizations/default/plugins/new/plugin.py").is_file())
        self.assertEqual(git(self.root, "status", "--porcelain"), "")

    def test_pending_image_does_not_block_publishing_existing_private_work(self):
        code, result, reconcile = self.synchronize()
        self.assertEqual(
            git(self.origin, "show", "main:" + self.skill), "Nyra's unpublished instructions"
        )
        self.assertEqual(result["status"], "waiting-for-runtime")
        self.assertEqual(code, 2, "A blocked runtime must be observable as an incomplete sync")
        reconcile.assert_called_once()

    def test_real_merge_conflict_preserves_both_versions_without_touching_runtime(self):
        self.incoming(conflict=True)
        code, result, reconcile = self.synchronize()
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "git-conflict")
        reconcile.assert_not_called()
        self.assertTrue(git(self.root, "ls-files", "--unmerged"))
        self.assertEqual(
            git(self.root, "show", "HEAD:" + self.skill), "Nyra's unpublished instructions"
        )
        self.assertEqual(
            git(self.origin, "show", "main:" + self.skill), "independently reviewed instructions"
        )
