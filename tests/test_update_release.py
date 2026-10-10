import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from update_release import (CandidateRejected, ReleaseUnavailable, evaluate_candidate,
                            inspect_latest, main, select_release, update_lock)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                   stderr=subprocess.PIPE).strip()


def fixture(base):
    upstream = base / "upstream"
    project = base / "project"
    upstream.mkdir()
    project.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    git(upstream, "config", "user.name", "Fixture")
    git(upstream, "config", "user.email", "fixture@example.test")
    (upstream / "agent.py").write_text("print('base')\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "base")
    previous = git(upstream, "rev-parse", "HEAD")
    (upstream / "docs.txt").write_text("release change\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "release")
    commit = git(upstream, "rev-parse", "HEAD")
    git(upstream, "tag", "v1.2.3", commit)

    patch = ("diff --git a/agent.py b/agent.py\n"
             "--- a/agent.py\n+++ b/agent.py\n@@ -1 +1 @@\n"
             "-print('base')\n+print('patched')\n")
    (project / "patches").mkdir()
    patch_path = project / "patches/local.patch"
    patch_path.write_text(patch)
    lock = {"schema": 1, "repository": str(upstream), "revision": previous,
            "patches": [{"path": "patches/local.patch",
                         "sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest()}],
            "native_tests": ["tests/test_agent.py"]}
    (project / "upstream.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    return project, upstream, previous, commit


class UpdateReleaseTests(unittest.TestCase):
    def test_select_release_ignores_drafts_prereleases_and_non_semver_tags(self):
        chosen = select_release([
            {"tag_name": "v2.0.0", "draft": True, "prerelease": False, "published_at": "2026-01-01T00:00:00Z"},
            {"tag_name": "v3.0.0-rc1", "draft": False, "prerelease": True, "published_at": "2026-02-01T00:00:00Z"},
            # An incomplete API object is not proof of a published stable release.
            {"tag_name": "v99.0.0", "published_at": "2026-04-15T00:00:00Z"},
            {"tag_name": "v1.9.0", "draft": False, "prerelease": False, "published_at": "2026-03-01T00:00:00Z"},
            {"tag_name": "v1.10.0", "draft": False, "prerelease": False, "published_at": "2026-04-01T00:00:00Z"},
        ])
        self.assertEqual(chosen["tag_name"], "v1.10.0")

    def test_release_selection_uses_publication_date_before_numeric_tag_order(self):
        chosen = select_release([
            {"id": 395553114, "name": "Hermes Agent v0.21.5 (v2026.9.24)",
             "tag_name": "v2026.9.24", "draft": False, "prerelease": False,
             "published_at": "2026-09-24T10:09:38Z"},
            {"id": 406714323, "name": "Hermes Agent v0.21.6", "tag_name": "v0.21.6",
             "draft": False, "prerelease": False, "published_at": "2026-10-08T11:51:57Z"},
        ])
        self.assertEqual(chosen["tag_name"], "v0.21.6")
        self.assertEqual(chosen["id"], 406714323)

    def test_release_selection_normalizes_utc_and_skips_malformed_publish_dates(self):
        chosen = select_release([
            {"tag_name": "v2.1.0", "draft": False, "prerelease": False,
             "published_at": "2026-10-08T12:00:00Z"},
            {"tag_name": "v2.9.0", "draft": False, "prerelease": False,
             "published_at": "2026-10-08T14:00:00+02:00"},
            {"tag_name": "v999.0.0", "draft": False, "prerelease": False,
             "published_at": "not-a-date"},
            {"tag_name": "v888.0.0", "draft": False, "prerelease": False,
             "published_at": "2026-10-08T12:01:00"},
        ])
        # Equal UTC publication instants use the numeric version tuple as a tie-breaker.
        self.assertEqual(chosen["tag_name"], "v2.9.0")
        with self.assertRaises(ReleaseUnavailable):
            select_release([{"tag_name": "v3.0.0", "draft": False,
                             "prerelease": False, "published_at": "malformed"}])

    def test_patch_conflict_is_reported_with_upstream_file_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, upstream, _, commit = fixture(root)
            (upstream / "agent.py").write_text("print('upstream changed same line')\n")
            git(upstream, "add", ".")
            git(upstream, "commit", "-qm", "conflicting upstream change")
            conflicting = git(upstream, "rev-parse", "HEAD")
            report = evaluate_candidate(project,
                {"tag_name": "v1.2.3", "published_at": "2026-01-01T00:00:00Z"}, conflicting)
            self.assertFalse(report["ready"])
            self.assertEqual(report["patches"][0]["status"], "conflict")
            self.assertEqual(report["upstream_patch_overlap"], ["agent.py"])

    def test_cli_update_lock_preserves_conflict_report_and_leaves_lock_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, upstream, _, _ = fixture(root)
            (upstream / "agent.py").write_text("print('upstream changed same line')\n")
            git(upstream, "add", ".")
            git(upstream, "commit", "-qm", "conflicting upstream change")
            candidate_commit = git(upstream, "rev-parse", "HEAD")
            release = {"id": 73, "tag_name": "v1.2.3", "published_at": "2026-01-02",
                       "html_url": "https://example.test/releases/v1.2.3"}
            report = evaluate_candidate(project, release, candidate_commit)
            self.assertFalse(report["ready"])
            lock_path = project / "upstream.lock.json"
            lock_before = lock_path.read_bytes()
            report_path = root / "candidate-report.json"
            argv = ["update_release.py", "--root", str(project), "--update-lock",
                    "--output", str(report_path)]

            with patch.object(sys, "argv", argv), patch("update_release.inspect_latest", return_value=report), \
                    contextlib.redirect_stdout(io.StringIO()):
                status = main()

            self.assertEqual(status, 2)
            written = json.loads(report_path.read_text())
            self.assertEqual(written["tag"], "v1.2.3")
            self.assertEqual(written["commit"], candidate_commit)
            self.assertEqual(written["patches"], report["patches"])
            self.assertEqual(written["upstream_patch_overlap"], ["agent.py"])
            self.assertFalse(written["ready"])
            self.assertFalse(written["lock_updated"])
            self.assertIn("do not apply sequentially", written["lock_update_error"])
            self.assertEqual(lock_path.read_bytes(), lock_before)

    def test_clean_release_applies_patch_and_updates_release_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, upstream, _, commit = fixture(root)
            release = {"id": 42, "tag_name": "v1.2.3", "draft": False, "prerelease": False,
                       "published_at": "2026-10-08T11:51:57Z", "html_url": "https://example.test/release"}
            report = evaluate_candidate(project, release, commit)
            self.assertTrue(report["ready"])
            self.assertEqual(report["patches"][0]["status"], "applies")
            self.assertEqual(report["upstream_patch_overlap"], [])
            update_lock(project, report)
            lock = json.loads((project / "upstream.lock.json").read_text())
            self.assertEqual(lock["revision"], commit)
            self.assertEqual(lock["release"], {"id": 42, "tag": "v1.2.3", "commit": commit,
                "published_at": "2026-10-08T11:51:57Z", "url": "https://example.test/release"})

    def test_release_metadata_refuses_tag_commit_divergence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, _, _, commit = fixture(root)
            lock_path = project / "upstream.lock.json"
            lock = json.loads(lock_path.read_text())
            lock["repository"] = "https://github.com/NousResearch/hermes-agent"
            lock["release"] = {"tag": "v1.2.3", "commit": "0" * 40}
            lock_path.write_text(json.dumps(lock))
            with self.assertRaisesRegex(ValueError, "release commit differs"):
                inspect_latest(project,
                    releases=[{"id": 1, "tag_name": "v1.2.3", "draft": False,
                               "prerelease": False, "published_at": "2026-01-01T00:00:00Z"}],
                    tag_resolver=lambda _repo, _tag: commit)

    def test_missing_published_release_is_a_distinct_failure(self):
        with self.assertRaises(ReleaseUnavailable):
            select_release([])

    def test_release_poll_cannot_downgrade_a_pinned_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, _, previous, _ = fixture(root)
            lock_path = project / "upstream.lock.json"
            lock = json.loads(lock_path.read_text())
            lock["repository"] = "https://github.com/NousResearch/hermes-agent"
            lock["release"] = {"tag": "v2.0.0", "commit": previous,
                               "url": "https://github.com/NousResearch/hermes-agent/releases/tag/v2.0.0"}
            lock["revision"] = previous
            lock_path.write_text(json.dumps(lock))
            with self.assertRaisesRegex(CandidateRejected, "older"):
                inspect_latest(project,
                    releases=[{"id": 1, "tag_name": "v1.2.3", "draft": False,
                               "prerelease": False, "published_at": "2026-01-01T00:00:00Z"}],
                    tag_resolver=lambda _repo, _tag: previous)


if __name__ == "__main__":
    unittest.main()
