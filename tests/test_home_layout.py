import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from prepare_image_context import prepare as image_context
from prepare_source import prepare as prepare_source
from project_files import ignore_text, layout
from source_tree import source_files


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "vm" / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


adoption = module("home_adoption", "adopt_home.py")
synchronization = module("home_sync", "sync_workspace.py")
bootstrap = module("home_bootstrap", "prepare_workspace.py")
reviewer = module("layout_review", "review_layout.py")
sys.modules['review_layout']=reviewer
workspace_reviewer = module('workspace_review','review_workspace.py')


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE, text=True).strip()


def project(root):
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "fixture")
    git(root, "config", "user.email", "fixture@example.test")
    value = {"schema": 1, "runtime_files": ["start.py"], "skills": ["hermes-self-management"]}
    files = {"project-files.json": json.dumps(value), ".gitignore": ignore_text(value),
             "README.md": "original", "Dockerfile": "FROM scratch\n", ".dockerignore": "*\n!runtime/**\n",
             "runtime/start.py": "print('runtime')\n", "config/hermes.yaml": "model: {}\n",
             "skills/hermes-self-management/SKILL.md": "distribution skill\n",
             "upstream.lock.json": json.dumps({"repository": "fixture", "revision": "a" * 40}),
             "hermes-agent/code.py": "print('native')\n"}
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return value


class HomeLayoutTests(unittest.TestCase):
    def test_controller_upgrade_preserves_unpublished_edits_and_adopts_known_published_copies(self):
        with tempfile.TemporaryDirectory() as temporary:
            source,home=Path(temporary)/'source',Path(temporary)/'home'
            project(source)
            subprocess.run(['git','clone','-q',str(source),str(home)],check=True)
            (source/'README.md').write_text('already propagated')
            git(source,'commit','-qam','intermediate')
            (source/'README.md').write_text('latest documentation')
            git(source,'commit','-qam','latest')
            (home/'README.md').write_text('already propagated')
            personal=home/'customizations/default/persona/USER.md'
            personal.parent.mkdir(parents=True);personal.write_text('my unpublished preference')
            git(home,'add',str(personal.relative_to(home)))
            git(home,'fetch','origin')
            report=workspace_reviewer.upgrade(home,git(source,'rev-parse','HEAD'))
            self.assertEqual(report['status'],'upgraded')
            self.assertEqual((home/'README.md').read_text(),'latest documentation')
            self.assertEqual(personal.read_text(),'my unpublished preference')
            self.assertEqual(report['pending_files'],1)

    def test_controller_upgrade_conflict_keeps_pending_edits_and_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            source,home=Path(temporary)/'source',Path(temporary)/'home'
            project(source)
            subprocess.run(['git','clone','-q',str(source),str(home)],check=True)
            old=git(home,'rev-parse','HEAD')
            (source/'README.md').write_text('remote conflicting edit')
            git(source,'commit','-qam','remote')
            (home/'README.md').write_text('personal conflicting edit')
            git(home,'fetch','origin')
            report=workspace_reviewer.upgrade(home,git(source,'rev-parse','HEAD'))
            self.assertEqual(report['status'],'conflict')
            self.assertEqual(git(home,'rev-parse','HEAD'),old)
            self.assertEqual((home/'README.md').read_text(),'personal conflicting edit')

    def test_controller_upgrade_recognizes_published_blob_shared_by_multiple_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            source,home=Path(temporary)/'source',Path(temporary)/'home'
            project(source)
            names=['skills/hermes-self-management/SKILL.md','customizations/second/skills/hermes-self-management/SKILL.md']
            (source/names[1]).parent.mkdir(parents=True)
            for name in names:(source/name).write_text('old')
            git(source,'add','.');git(source,'commit','-qm','base copies')
            subprocess.run(['git','clone','-q',str(source),str(home)],check=True)
            for name in names:(source/name).write_text('intermediate')
            git(source,'commit','-qam','published copies')
            (source/names[0]).write_text('latest')
            git(source,'rm',names[1]);git(source,'commit','-qam','deduplicate')
            for name in names:(home/name).write_text('intermediate')
            git(home,'fetch','origin')
            report=workspace_reviewer.upgrade(home,git(source,'rev-parse','HEAD'))
            self.assertEqual(report['status'],'upgraded')
            self.assertEqual((home/names[0]).read_text(),'latest')
            self.assertFalse((home/names[1]).exists())
            self.assertEqual(git(home,'status','--porcelain'),'')
            # An interrupted older upgrade may leave an already published copy untracked.
            (home/names[1]).write_text('intermediate\n\n')
            report=workspace_reviewer.upgrade(home,git(source,'rev-parse','HEAD'))
            self.assertEqual(report['status'],'upgraded')
            self.assertFalse((home/names[1]).exists())
            self.assertEqual(git(home,'status','--porcelain'),'')

    def test_review_retires_projection_without_removing_native_skill_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, home = Path(temporary)/'source',Path(temporary)/'home'
            project(source)
            projection = 'customizations/default/skills/hermes-self-management/SKILL.md'
            (source/projection).parent.mkdir(parents=True)
            (source/projection).write_text('old projected copy')
            git(source,'add','customizations');git(source,'commit','-qm','old projection')
            subprocess.run(['git','clone','-q',str(source),str(home)],check=True)
            native = home/'skills/personal/SKILL.md'
            native.parent.mkdir(parents=True);native.write_text('active personal instructions')
            git(source,'rm',projection);git(source,'commit','-qm','deduplicate projection')
            git(home,'fetch','origin');target=git(source,'rev-parse','HEAD')
            real_git=reviewer.git
            def fail_metadata(root,*args):
                if args[0]=='update-ref':raise OSError('metadata failure')
                return real_git(root,*args)
            with patch.object(reviewer,'git',side_effect=fail_metadata):
                with self.assertRaises(OSError):reviewer.review(home,target)
            self.assertTrue((home/projection).is_file())
            self.assertEqual(git(home,'status','--porcelain'),'')
            result=reviewer.review(home,target)
            self.assertEqual(result['retired_projection_files'],1)
            self.assertFalse((home/projection).exists())
            self.assertEqual(native.read_text(),'active personal instructions')
            self.assertEqual(git(home,'status','--porcelain'),'')

    def test_remote_review_checks_only_paths_the_merge_can_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'project';project(root)
            before=git(root,'rev-parse','HEAD')
            (root/'README.md').write_text('incoming change')
            git(root,'commit','-qam','incoming')
            after=git(root,'rev-parse','HEAD')
            original=synchronization.assert_paths
            checked=[]
            def observe(home,names,value):
                checked.extend(names);return original(home,names,value)
            with patch.object(synchronization,'assert_paths',side_effect=observe):
                self.assertIsNone(synchronization.review_remote(root,before,after))
            self.assertEqual(set(checked),{'README.md'})
            (root/'README.md').unlink();(root/'README.md').symlink_to(root/'config/hermes.yaml')
            with self.assertRaisesRegex(ValueError,'symlink'):synchronization.review_remote(root,before,after)
    def test_adoption_preserves_large_database_wal_personal_skills_and_nested_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, home = root / "source", root / "home"
            project(source)
            home.mkdir(mode=0o700)
            (home / "hermes-agent").mkdir()
            (home / "hermes-agent/code.py").write_bytes((source / "hermes-agent/code.py").read_bytes())
            git(home / "hermes-agent", "init", "-q")
            nested_config = (home / "hermes-agent/.git/config").read_bytes()
            db = sqlite3.connect(home / "state.db")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE memories (text)")
            db.execute("INSERT INTO memories VALUES ('remember')")
            db.commit()
            (home / "state.db").open("ab").close()
            # A separate sparse file exercises the real 300 MB scale without
            # generating data; adoption/build must not even copy this payload.
            with (home / "plugin-state.sqlite3").open("wb") as sparse:
                sparse.truncate(320479232)
            (home / "config.yaml").write_text("personal: preserved\n")
            (home / ".env").write_text("TOKEN=private-fixture\n")
            personal = home / "skills/my-private-skill"
            personal.mkdir(parents=True)
            (personal / "SKILL.md").write_text("private skill")
            (home / "runtime").mkdir()
            (home / "runtime/private.json").write_text("private runtime")
            before = {name: (home / name).read_bytes() for name in ("state.db", "state.db-wal", "state.db-shm", ".env", "config.yaml")}
            report = adoption.adopt(source, home, check=True)
            self.assertGreater(report["new_files"], 0)
            self.assertFalse((home / ".git").exists())
            adoption.adopt(source, home)
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            for name, contents in before.items():
                self.assertEqual((home / name).read_bytes(), contents)
            self.assertEqual((home / "plugin-state.sqlite3").stat().st_size, 320479232)
            self.assertEqual((home / "hermes-agent/.git/config").read_bytes(), nested_config)
            self.assertEqual(git(home, "status", "--porcelain"), "")
            self.assertEqual(git(home, "ls-files", "hermes-agent"), "")
            (home / "hermes-agent/new.py").write_text("print('new-code')\n")
            self.assertIn(Path("new.py"), set(source_files(home / "hermes-agent")))
            self.assertEqual(git(home, "status", "--porcelain"), "")
            self.assertIn("new.py", git(home / "hermes-agent", "status", "--porcelain"))
            context = root / "context"
            selected = image_context(home, context)
            self.assertIn("runtime/start.py", selected)
            self.assertTrue((context / "skills/hermes-self-management/SKILL.md").exists())
            self.assertFalse((context / "skills/my-private-skill").exists())
            self.assertFalse((context / "runtime/private.json").exists())
            self.assertFalse((context / "state.db").exists())
            db.close()

    def test_review_untracks_native_code_without_deleting_checkout_venv_or_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, home = root / "source", root / "home"
            project(source)
            git(source, "add", "-f", "hermes-agent/code.py")
            git(source, "commit", "-qm", "legacy vendor inventory")
            subprocess.run(["git", "clone", "-q", str(source), str(home)], check=True)
            native = home / "hermes-agent"
            git(native, "init", "-q")
            nested_config = (native / ".git/config").read_bytes()
            (native / "venv").mkdir()
            (native / "venv/marker").write_text("keep interpreter")
            (home / "state.db").write_text("keep database")
            git(source, "rm", "--cached", "hermes-agent/code.py")
            (source / "README.md").write_text("new source model")
            git(source, "commit", "-qam", "pinned model")
            git(home, "fetch", "origin")
            target = git(source, "rev-parse", "HEAD")
            before = git(home, "rev-parse", "HEAD")
            self.assertEqual(reviewer.review(home, target, check=True)["retained_native_files"], 1)
            self.assertEqual(git(home, "rev-parse", "HEAD"), before)
            real_git = reviewer.git
            def fail_metadata(path, *arguments):
                if arguments[0] == "update-ref":
                    raise OSError("fixture metadata failure")
                return real_git(path, *arguments)
            with patch.object(reviewer, "git", side_effect=fail_metadata):
                with self.assertRaises(OSError):
                    reviewer.review(home, target)
            self.assertEqual(git(home, "rev-parse", "HEAD"), before)
            self.assertEqual(git(home, "status", "--porcelain"), "")
            self.assertEqual((home / "README.md").read_text(), "original")
            reviewer.review(home, target)
            self.assertEqual(git(home, "rev-parse", "HEAD"), target)
            self.assertEqual(git(home, "status", "--porcelain"), "")
            self.assertEqual((native / "code.py").read_text(), "print('native')\n")
            self.assertEqual((native / ".git/config").read_bytes(), nested_config)
            self.assertEqual((native / "venv/marker").read_text(), "keep interpreter")
            self.assertEqual((home / "state.db").read_text(), "keep database")
            self.assertEqual((home / "README.md").read_text(), "new source model")

    def test_offline_bootstrap_checks_and_adopts_at_the_configured_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, home, bundle = root / "source", root / "home", root / "project.bundle"
            project(source)
            for name in ("vm/adopt_home.py", "scripts/project_files.py"):
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            git(source, "add", ".")
            git(source, "commit", "-qm", "bootstrap helpers")
            git(source, "bundle", "create", str(bundle), "main")
            home.mkdir()
            (home / ".env").write_text("existing private environment")
            repo = "git@github.com:fixture/hermes.git"
            bootstrap.prepare(repo, home=home, check=True, bundle=bundle)
            self.assertFalse((home / ".git").exists())
            self.assertEqual(list(home.iterdir()), [home / ".env"])
            bootstrap.prepare(repo, home=home, bundle=bundle)
            self.assertEqual(git(home, "remote", "get-url", "origin"), repo)
            self.assertEqual(git(home, "status", "--porcelain"), "")
            self.assertEqual((home / ".env").read_text(), "existing private environment")
            self.assertEqual((home / ".local/dev-home").stat().st_mode & 0o777, 0o700)
            self.assertEqual(bootstrap.prepare(repo, home=home, bundle=bundle), home)

    def test_failed_metadata_install_rolls_back_only_new_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, home = root / "source", root / "home"
            project(source)
            home.mkdir()
            (home / ".env").write_text("preserved private data")
            with patch.object(adoption.shutil, "copytree", side_effect=OSError("fixture failure")):
                with self.assertRaises(OSError):
                    adoption.adopt(source, home)
            self.assertEqual(list(home.iterdir()), [home / ".env"])
            self.assertEqual((home / ".env").read_text(), "preserved private data")

    def test_collision_refuses_every_write_and_keeps_existing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, home = root / "source", root / "home"
            project(source)
            home.mkdir()
            (home / "README.md").write_text("local work")
            with self.assertRaisesRegex(ValueError, "Adoption refused"):
                adoption.adopt(source, home)
            self.assertEqual(list(home.iterdir()), [home / "README.md"])
            self.assertEqual((home / "README.md").read_text(), "local work")

    def test_symlink_parent_cannot_write_outside_the_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, home, outside = root / "source", root / "home", root / "outside"
            project(source)
            home.mkdir()
            outside.mkdir()
            (home / "runtime").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                adoption.adopt(source, home)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((home / ".git").exists())

    def test_image_context_rejects_force_tracked_private_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            project(source)
            (source / "state.db").write_text("private")
            git(source, "add", "-f", "state.db")
            with self.assertRaisesRegex(ValueError, "outside the project layout"):
                image_context(source, root / "context")
            self.assertFalse((root / "context").exists())

    def test_remote_state_and_layout_changes_require_manual_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publisher, remote, home = root / "publisher", root / "remote", root / "home"
            project(publisher)
            remote.mkdir()
            git(remote, "init", "--bare", "-q")
            git(publisher, "remote", "add", "origin", str(remote))
            git(publisher, "push", "-u", "origin", "main")
            subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(home)], check=True)
            (home / "state.db").write_text("local private database")
            (publisher / "README.md").write_text("published code")
            git(publisher, "commit", "-qam", "code update")
            git(publisher, "push")
            self.assertEqual(synchronization.sync(home), "updated")
            self.assertEqual((home / "state.db").read_text(), "local private database")
            stable_head = git(home, "rev-parse", "HEAD")
            (publisher / "state.db").write_text("remote database")
            git(publisher, "add", "-f", "state.db")
            git(publisher, "commit", "-qm", "bad state commit")
            git(publisher, "push")
            self.assertEqual(synchronization.sync(home), "protected-paths")
            self.assertEqual(git(home, "rev-parse", "HEAD"), stable_head)
            self.assertEqual((home / "state.db").read_text(), "local private database")
            git(publisher, "rm", "state.db")
            (publisher / ".gitignore").write_text("changed policy\n")
            git(publisher, "commit", "-qam", "layout change")
            git(publisher, "push")
            self.assertEqual(synchronization.sync(home), "layout-review-required")
            self.assertEqual(git(home, "rev-parse", "HEAD"), stable_head)

    def test_upstream_update_requests_review_and_preserves_native_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publisher, home = root / "publisher", root / "home"
            project(publisher)
            subprocess.run(["git", "clone", "-q", str(publisher), str(home)], check=True)
            native = home / "hermes-agent"
            native.mkdir()
            (native / "code.py").write_text("running source")
            original = git(home, "rev-parse", "HEAD")
            lock = publisher / "upstream.lock.json"
            value = json.loads(lock.read_text())
            value["revision"] = "b" * 40
            lock.write_text(json.dumps(value))
            git(publisher, "commit", "-qam", "update upstream")
            self.assertEqual(synchronization.sync(home), "source-review-required")
            self.assertEqual(git(home, "rev-parse", "HEAD"), original)
            self.assertEqual((native / "code.py").read_text(), "running source")

    def test_new_remote_file_cannot_overwrite_an_ignored_existing_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publisher, remote, home = root / "publisher", root / "remote", root / "home"
            project(publisher)
            (publisher / "skills/hermes-self-management/.gitignore").write_text("asset.txt\n")
            git(publisher, "add", ".")
            git(publisher, "commit", "-qm", "asset ignore")
            remote.mkdir()
            git(remote, "init", "--bare", "-q")
            git(publisher, "remote", "add", "origin", str(remote))
            git(publisher, "push", "-u", "origin", "main")
            subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(home)], check=True)
            (home / "skills/hermes-self-management/asset.txt").write_text("user asset")
            (publisher / "skills/hermes-self-management/asset.txt").write_text("remote asset")
            git(publisher, "add", "-f", "skills/hermes-self-management/asset.txt")
            git(publisher, "commit", "-qm", "new asset")
            git(publisher, "push")
            self.assertEqual(synchronization.sync(home), "existing-files")
            self.assertEqual((home / "skills/hermes-self-management/asset.txt").read_text(), "user asset")


if __name__ == "__main__":
    unittest.main()
