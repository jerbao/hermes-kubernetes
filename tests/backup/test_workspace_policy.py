from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import hermes_kubernetes.backup.job as backup_job
from hermes_kubernetes.backup.restore import restore
from hermes_kubernetes.state.git_cache import restore as restore_git_cache
from hermes_kubernetes.backup.state import MANIFEST, digest
import hermes_kubernetes.backup.policy as workspace_policy
import hermes_kubernetes.backup.workspace as workspace_snapshot


def exclusion(path, kind="rebuildable"):
    return {
        "path": path,
        "kind": kind,
        "reason": "Explicit fixture download cache",
        "rebuild": "Reinstall from the retained lock file; never execute this text during restore",
    }


class WorkspacePolicyTests(unittest.TestCase):
    def test_declared_external_project_link_is_preserved_without_backing_up_its_target(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            scratch = home / ".hermes/cache/scratch"
            target = home / "unrelated-project/fixtures"
            scratch.mkdir(parents=True)
            target.mkdir(parents=True)
            (target / "private-data").write_text("outside the Hermes backup scope")
            link = scratch / "fixtures"
            link.symlink_to(target)
            policy = {
                "schema": 1,
                "include": [".hermes"],
                "external_links": [
                    {
                        "path": ".hermes/cache/scratch/fixtures",
                        "target": "unrelated-project/fixtures",
                        "reason": "Unrelated project data; retain the pointer without broadening the backup",
                    }
                ],
            }
            report = workspace_snapshot.preview(home, policy)
            self.assertTrue(report["ready"])
            self.assertEqual(report["external_link_targets"], policy["external_links"])
            status = backup_job.backup(home, base / "backups", kind="workspace", policy=policy)
            restored = base / "restored"
            verification = restore(
                base / "backups" / status["archive"], restored, sha256=status["sha256"]
            )
            self.assertEqual(verification["out_of_scope_link_targets"], policy["external_links"])
            self.assertEqual((restored / ".hermes/cache/scratch/fixtures").readlink(), target)
            self.assertFalse((restored / "unrelated-project").exists())
            self.assertEqual(
                (target / "private-data").read_text(), "outside the Hermes backup scope"
            )
            # A declaration for one project must never approve a newly omitted skill.
            skill = home / ".agents/personal-skill"
            skill.mkdir(parents=True)
            link.unlink()
            link.symlink_to(skill)
            self.assertFalse(workspace_snapshot.preview(home, policy)["ready"])
            prior = (base / "backups/latest.json").read_bytes()
            with self.assertRaises(ValueError):
                backup_job.backup(home, base / "backups", kind="workspace", policy=policy)
            self.assertEqual((base / "backups/latest.json").read_bytes(), prior)

    def test_legacy_policy_checksum_stays_unchanged_and_external_wildcards_are_refused(
        self,
    ):
        old = {"schema": 1, "include": [".hermes"], "exclude": [], "required": []}
        self.assertEqual(
            workspace_policy.checksum(workspace_policy.normalize(old)),
            workspace_policy.checksum(old),
        )
        with self.assertRaises(ValueError):
            workspace_policy.normalize(
                {
                    **old,
                    "external_links": [
                        {
                            "path": ".hermes/*",
                            "target": "project",
                            "reason": "Must be literal",
                        }
                    ],
                }
            )

    def test_selected_backup_restores_git_skill_and_live_browser_wal_without_other_home_data(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            project = home / ".hermes"
            project.mkdir(parents=True)
            git = ["git", "-C", str(project)]
            for args in [
                ["init", "-q"],
                ["config", "user.name", "fixture"],
                ["config", "user.email", "fixture@example.invalid"],
            ]:
                subprocess.run([*git, *args], check=True, capture_output=True)
            (project / "file").write_text("committed")
            (project / ".gitignore").write_text("/download-cache/\n")
            subprocess.run([*git, "add", "file", ".gitignore"], check=True)
            subprocess.run([*git, "commit", "-qm", "local-only"], check=True)
            (project / "file").write_text("uncommitted")
            (project / "staged").write_text("staged")
            subprocess.run([*git, "add", "staged"], check=True)
            (project / "untracked").write_text("personal")
            skill = project / "skills/personal/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("preserve")
            browser = home / ".cache/browser"
            (browser / "Default").mkdir(parents=True)
            (browser / "Local State").write_text("fixture")
            cache = project / "download-cache"
            cache.mkdir()
            (cache / "wheel").write_bytes(b"cache" * 100000)
            (home / "Downloads").mkdir()
            unrelated = home / "Downloads/personal-document"
            unrelated.write_text("outside the chosen Hermes scope; do not delete")
            policy = {
                "schema": 1,
                "include": [".hermes", ".cache/browser"],
                "required": [".hermes", ".cache/browser"],
                "exclude": [exclusion(".hermes/download-cache")],
            }
            database = sqlite3.connect(browser / "Default/Cookies")
            try:
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("CREATE TABLE cookies(value TEXT)")
                database.execute("INSERT INTO cookies VALUES ('committed in live WAL')")
                database.commit()
                preview = workspace_snapshot.preview(home, policy)
                self.assertTrue(preview["ready"])
                status = backup_job.backup(home, base / "backups", kind="workspace", policy=policy)
                copied = restore(
                    base / "backups" / status["archive"],
                    base / "restored",
                    sha256=status["sha256"],
                )
                self.assertEqual(copied["selection_sha256"], preview["selection_sha256"])
                self.assertEqual(copied["excluded_roots"], 1)
                self.assertEqual(
                    subprocess.check_output(
                        [
                            "git",
                            "-C",
                            str(base / "restored/.hermes"),
                            "status",
                            "--porcelain",
                        ]
                    ),
                    subprocess.check_output([*git, "status", "--porcelain"]),
                )
                self.assertEqual(
                    (base / "restored/.hermes/skills/personal/SKILL.md").read_text(),
                    "preserve",
                )
                with closing(
                    sqlite3.connect(base / "restored/.cache/browser/Default/Cookies")
                ) as copy:
                    self.assertEqual(
                        copy.execute("SELECT value FROM cookies").fetchone()[0],
                        "committed in live WAL",
                    )
                self.assertFalse((base / "restored/Downloads").exists())
                self.assertFalse((base / "restored/.hermes/download-cache").exists())
                self.assertTrue(unrelated.exists())
                self.assertTrue((cache / "wheel").exists())
            finally:
                database.close()

    def test_omitted_internal_skill_target_and_missing_required_root_abort_before_retention(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            (home / ".hermes/skills").mkdir(parents=True)
            (home / ".agents/skill").mkdir(parents=True)
            (home / ".agents/skill/SKILL.md").write_text("external skill in the same home")
            (home / ".hermes/skills/alias").symlink_to("../../.agents/skill")
            old = backup_job.backup(home, base / "backups", kind="workspace")
            prior = (base / "backups/latest.json").read_bytes()
            policy = {"schema": 1, "include": [".hermes"], "required": [".hermes"]}
            preview = workspace_snapshot.preview(home, policy)
            self.assertFalse(preview["ready"])
            self.assertEqual(preview["unselected_link_targets"][0]["target"], ".agents/skill")
            with self.assertRaises(ValueError):
                backup_job.backup(home, base / "backups", keep=1, kind="workspace", policy=policy)
            self.assertEqual((base / "backups/latest.json").read_bytes(), prior)
            self.assertTrue((base / "backups" / old["archive"]).exists())
            policy["include"].append(".agents")
            self.assertTrue(workspace_snapshot.preview(home, policy)["ready"])
            policy["required"].append(".hermes/missing")
            self.assertFalse(workspace_snapshot.preview(home, policy)["ready"])

    def test_excluding_wal_of_an_included_database_is_refused_without_disturbing_writer(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            home.mkdir()
            db = sqlite3.connect(home / "state.db")
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE messages(value TEXT)")
                db.execute("INSERT INTO messages VALUES ('keep committed recovery data')")
                db.commit()
                policy = {
                    "schema": 1,
                    "include": ["."],
                    "exclude": [exclusion("state.db-wal")],
                }
                with self.assertRaises(ValueError):
                    workspace_snapshot.build_snapshot(home, base / "backup.tar.gz", policy=policy)
                self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
                self.assertFalse((base / "backup.tar.gz").exists())
            finally:
                db.close()

    def test_rebuildable_exclusion_refuses_even_a_clean_local_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            repo = home / "cache/local-repo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            policy = {"schema": 1, "include": ["."], "exclude": [exclusion("cache")]}
            preview = workspace_snapshot.preview(home, policy)
            self.assertFalse(preview["ready"])
            self.assertEqual(preview["excluded_repositories"], ["cache/local-repo"])
            policy["exclude"][0]["kind"] = "generated-validation"
            self.assertTrue(workspace_snapshot.preview(home, policy)["ready"])

    def test_remote_upstream_history_is_omitted_only_without_local_git_work(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            repo = home / ".hermes/hermes-agent"
            repo.mkdir(parents=True)
            git = ["git", "-C", str(repo)]
            for args in [
                ["init", "-q", "-b", "main"],
                ["config", "user.name", "fixture"],
                ["config", "user.email", "fixture@example.invalid"],
                [
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/NousResearch/hermes-agent.git",
                ],
            ]:
                subprocess.run([*git, *args], check=True, capture_output=True)
            (repo / "source.py").write_text("pinned upstream")
            subprocess.run([*git, "add", "."], check=True)
            subprocess.run([*git, "commit", "-qm", "published"], check=True)
            revision = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
            subprocess.run([*git, "update-ref", "refs/remotes/origin/main", revision], check=True)
            item = {
                "path": ".hermes/hermes-agent/.git",
                "kind": "upstream-git",
                "reason": "Remote source history",
                "rebuild": "Clone the pinned source",
                "repository": "https://github.com/NousResearch/hermes-agent",
                "revision": revision,
                "branch": "main",
            }
            policy = {"schema": 1, "include": [".hermes"], "exclude": [item]}
            report = workspace_snapshot.preview(home, policy)
            self.assertTrue(report["ready"])
            archive = backup_job.backup(home, base / "backups", kind="workspace", policy=policy)
            restore(
                base / "backups" / archive["archive"],
                base / "restored",
                sha256=archive["sha256"],
            )
            self.assertFalse((base / "restored/.hermes/hermes-agent/.git").exists())
            self.assertEqual(
                (base / "restored/.hermes/hermes-agent/source.py").read_text(),
                "pinned upstream",
            )
            # A tracked edit, staging, extra branch and local-only reflog must block omission.
            (repo / "source.py").write_text("local edit")
            self.assertFalse(workspace_snapshot.preview(home, policy)["ready"])
            subprocess.run([*git, "add", "."], check=True)
            self.assertFalse(workspace_snapshot.preview(home, policy)["ready"])
            subprocess.run([*git, "commit", "-qm", "unpublished"], check=True)
            subprocess.run([*git, "reset", "--hard", revision], check=True, capture_output=True)
            self.assertFalse(workspace_snapshot.preview(home, policy)["ready"])
            subprocess.run([*git, "reflog", "expire", "--expire=now", "--all"], check=True)
            self.assertTrue(workspace_snapshot.preview(home, policy)["ready"])
            subprocess.run([*git, "branch", "local-branch"], check=True)
            self.assertFalse(workspace_snapshot.preview(home, policy)["ready"])
            prior = (base / "backups/latest.json").read_bytes()
            with self.assertRaises(ValueError):
                backup_job.backup(home, base / "backups", kind="workspace", policy=policy)
            self.assertEqual((base / "backups/latest.json").read_bytes(), prior)

    def test_declared_binary_reconstruction_keeps_alias_without_copying_package_cache(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            (home / ".hermes/bin").mkdir(parents=True)
            (home / ".hermes/tools/python").mkdir(parents=True)
            (home / ".hermes/tools/python/python").write_text("downloaded executable")
            (home / ".hermes/bin/python").symlink_to("../tools/python/python")
            policy = {
                "schema": 1,
                "include": [".hermes"],
                "exclude": [exclusion(".hermes/tools")],
            }
            report = workspace_snapshot.preview(home, policy)
            self.assertTrue(report["ready"])
            self.assertEqual(
                report["rebuildable_link_targets"][0]["target"],
                ".hermes/tools/python/python",
            )
            (home / ".hermes/tools/.git").write_text("")  # uv uses non-repository sentinels
            self.assertTrue(workspace_snapshot.preview(home, policy)["ready"])

    def test_package_download_git_cache_requires_its_retained_recovery_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / "home"
            cache = home / ".hermes/cache/uv/git-v0/repository"
            cache.mkdir(parents=True)
            subprocess.run(["git", "-C", str(cache), "init", "-q", "--bare"], check=True)
            policy = {
                "schema": 1,
                "include": [".hermes"],
                "exclude": [
                    {
                        "path": ".hermes/cache/uv",
                        "kind": "package-cache",
                        "reason": "uv download storage",
                        "rebuild": "Download packages again from the retained lock",
                        "recipe": ".hermes/uv.lock",
                    }
                ],
            }
            self.assertFalse(workspace_snapshot.preview(home, policy)["ready"])
            (home / ".hermes/uv.lock").write_text("retained package recipe")
            self.assertTrue(workspace_snapshot.preview(home, policy)["ready"])
            policy["exclude"][0]["path"] = ".hermes/user-project"
            with self.assertRaises(ValueError):
                workspace_policy.normalize(policy)

    def test_omitted_remote_packs_recover_from_seed_with_native_backup_refs_and_local_objects(
        self,
    ):
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            repo = home / ".hermes/hermes-agent"
            repo.mkdir(parents=True)
            git = ["git", "-C", str(repo)]
            for args in [
                ["init", "-q", "-b", "main"],
                ["config", "user.name", "fixture"],
                ["config", "user.email", "fixture@example.invalid"],
                [
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/NousResearch/hermes-agent.git",
                ],
            ]:
                subprocess.run([*git, *args], check=True, capture_output=True)
            (repo / "source.py").write_text("upstream source")
            subprocess.run([*git, "add", "."], check=True)
            subprocess.run([*git, "commit", "-qm", "published upstream"], check=True)
            revision = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
            subprocess.run([*git, "update-ref", "refs/remotes/origin/main", revision], check=True)
            subprocess.run([*git, "tag", "-am", "published tag", "v1"], check=True)
            subprocess.run([*git, "gc", "--prune=now"], check=True, capture_output=True)
            seed = base / "seed"
            subprocess.run(
                ["git", "clone", "-q", "--no-checkout", str(repo), str(seed)],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(seed),
                    "remote",
                    "set-url",
                    "origin",
                    "https://github.com/NousResearch/hermes-agent.git",
                ],
                check=True,
            )
            (repo / "local.py").write_text("local native update backup")
            subprocess.run([*git, "add", "."], check=True)
            subprocess.run([*git, "commit", "-qm", "local-only backup"], check=True)
            local = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
            subprocess.run(
                [*git, "update-ref", "refs/hermes-update-backups/fixture", local],
                check=True,
            )
            subprocess.run([*git, "reset", "--hard", revision], check=True, capture_output=True)
            (repo / "source.py").write_text("staged edit")
            subprocess.run([*git, "add", "source.py"], check=True)
            (repo / "source.py").write_text("unstaged edit")
            (repo / "personal.txt").write_text("untracked work")
            tags = subprocess.check_output(
                [*git, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags"]
            )
            policy = {
                "schema": 1,
                "include": [".hermes"],
                "exclude": [
                    {
                        "path": ".hermes/hermes-agent/.git/objects/pack",
                        "kind": "upstream-git",
                        "repository": "https://github.com/NousResearch/hermes-agent",
                        "revision": revision,
                        "branch": "main",
                        "tags_sha256": hashlib.sha256(tags).hexdigest(),
                        "reason": "Remote packs only; keep local metadata and loose objects",
                        "rebuild": "Seed from upstream clone",
                    }
                ],
            }
            self.assertTrue(workspace_snapshot.preview(home, policy)["ready"])
            status = backup_job.backup(home, base / "backups", kind="workspace", policy=policy)
            restored = base / "restored"
            restore(base / "backups" / status["archive"], restored, sha256=status["sha256"])
            self.assertFalse((restored / ".hermes/hermes-agent/.git/objects/pack").exists())
            result = restore_git_cache(restored, policy, seed)
            self.assertTrue(result["repositories"][0]["git_fsck"])
            recovered = ["git", "-C", str(restored / ".hermes/hermes-agent")]
            for args in [
                ["show-ref"],
                ["status", "--porcelain"],
                ["reflog", "--format=%H"],
            ]:
                self.assertEqual(
                    subprocess.check_output([*git, *args]),
                    subprocess.check_output([*recovered, *args]),
                )
            self.assertEqual(
                subprocess.check_output(
                    [*recovered, "show", "refs/hermes-update-backups/fixture:local.py"]
                ),
                b"local native update backup",
            )
            with self.assertRaises(ValueError):
                restore_git_cache(restored, policy, seed)
            # Packing unpublished work makes its omission unsafe: reject, keep old backup.
            subprocess.run([*git, "gc", "--prune=now"], check=True, capture_output=True)
            self.assertFalse(workspace_snapshot.preview(home, policy)["ready"])

    def test_changes_in_explicitly_excluded_cache_do_not_abort_selected_data(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            (home / "cache").mkdir(parents=True)
            (home / "file").write_text("preserve")
            (home / "cache/download").write_text("before")
            policy = {"schema": 1, "include": ["."], "exclude": [exclusion("cache")]}

            def change_cache(path):
                (home / "cache/download").write_text("changed while copying")
                return digest(path)

            with patch.object(workspace_snapshot, "digest", side_effect=change_cache):
                workspace_snapshot.build_snapshot(home, base / "backup.tar.gz", policy=policy)

    def test_excluded_child_changes_directory_mtime_before_its_selected_sibling_is_copied(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            (home / "work").mkdir(parents=True)
            (home / "work/keep").write_text("preserved")
            policy = {
                "schema": 1,
                "include": ["."],
                "exclude": [exclusion("work/generated")],
            }
            original_review = workspace_snapshot.review

            def review_and_change_cache(*args):
                result = original_review(*args)
                (home / "work/generated").write_text("reconstructible cache")
                return result

            with patch.object(workspace_snapshot, "review", side_effect=review_and_change_cache):
                workspace_snapshot.build_snapshot(home, base / "backup.tar.gz", policy=policy)
            restored = base / "restored"
            restore(base / "backup.tar.gz", restored, sha256=digest(base / "backup.tar.gz"))
            self.assertEqual((restored / "work/keep").read_text(), "preserved")
            self.assertFalse((restored / "work/generated").exists())

    def test_archive_cannot_claim_a_selection_that_excludes_its_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            home = base / "home"
            home.mkdir()
            (home / "file").write_text("personal")
            workspace_snapshot.build_snapshot(
                home, base / "good.tar.gz", policy={"schema": 1, "include": ["."]}
            )
            with (
                tarfile.open(base / "good.tar.gz", "r:gz") as old,
                tarfile.open(base / "bad.tar.gz", "w:gz") as bad,
            ):
                for entry in old:
                    data = old.extractfile(entry).read() if entry.isfile() else None
                    if entry.name == MANIFEST:
                        manifest = json.loads(data)
                        manifest["selection"]["exclude"] = [exclusion("file")]
                        manifest["selection_sha256"] = workspace_policy.checksum(
                            manifest["selection"]
                        )
                        data = json.dumps(manifest).encode()
                        entry.size = len(data)
                    bad.addfile(entry, io.BytesIO(data) if data is not None else None)
            with self.assertRaises(ValueError):
                restore(
                    base / "bad.tar.gz",
                    base / "restored",
                    sha256=digest(base / "bad.tar.gz"),
                )
            self.assertFalse((base / "restored").exists())

    def test_policy_rejects_traversal_globs_broad_cache_and_git_exclusions(self):
        for name in [
            "../outside",
            "/outside",
            ".",
            "a/../b",
            "a//b",
            "a/*",
            ".cache",
            ".hermes/.git",
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                workspace_policy.normalize(
                    {"schema": 1, "include": ["."], "exclude": [exclusion(name)]}
                )
