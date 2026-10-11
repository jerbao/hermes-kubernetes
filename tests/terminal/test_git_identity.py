"""Verify real commit attribution while retaining existing Git hooks."""

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hermes_kubernetes.terminal.git_identity import configure


class GitIdentityTests(unittest.TestCase):
    def test_owner_and_assistant_are_attributed_without_hook_replacement(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "HOME": directory,
                    "GIT_CONFIG_GLOBAL": str(Path(directory) / ".gitconfig"),
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            ),
        ):

            def git(*args):
                return subprocess.check_output(["git", "-C", directory, *args], text=True).strip()

            git("init", "-q")
            git("config", "--global", "user.name", "Owner")
            git("config", "--global", "user.email", "owner@example.invalid")
            hook = Path(directory) / ".git/hooks/prepare-commit-msg"
            hook.write_text("#!/bin/sh\nprintf '\\nHook preserved\\n' >> \"$1\"\n")
            hook.chmod(0o755)
            configure("Assistant", "assistant@example.invalid")
            status = subprocess.check_output(
                [
                    sys.executable,
                    "-m",
                    "hermes_kubernetes.instances.cli",
                    "--root",
                    directory,
                    "git-identity",
                    "--check",
                ],
                text=True,
            )
            self.assertTrue(json.loads(status)["configured"])
            git(
                "hermes-commit",
                "--allow-empty",
                "-m",
                "Fixture\n\nCo-authored-by: Assistant <assistant@example.invalid>",
            )
            self.assertEqual(
                git("log", "-1", "--format=%an <%ae>"), "Owner <owner@example.invalid>"
            )
            body = git("log", "-1", "--format=%B")
            self.assertEqual(body.count("Co-authored-by: Assistant <assistant@example.invalid>"), 1)
            self.assertIn("Hook preserved", body)
