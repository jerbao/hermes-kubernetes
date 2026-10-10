import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from editable_source import SourceUpdateError, ensure, running_processes, status
from export_patch import export
from source_tree import fingerprint
from upstream_source import checkout, git as source_git
from test_upstream_source import fixture, git


def advance(upstream, project):
    (upstream / 'extra.py').write_text("print('new-release')\n")
    git(upstream, 'commit', '-qam', 'new upstream release')
    lock = json.loads((project / 'upstream.lock.json').read_text())
    lock['revision'] = git(upstream, 'rev-parse', 'HEAD')
    (project / 'upstream.lock.json').write_text(json.dumps(lock))
    return lock['revision']


class EditableSourceTests(unittest.TestCase):
    def test_managed_update_archives_old_git_environment_and_keeps_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, project, revision = fixture(Path(temporary))
            source = project / 'hermes-agent'
            ensure(project)
            original_inode = source.stat().st_ino
            (source / 'venv').mkdir()
            (source / 'venv/marker').write_text('preserve dependencies')
            (source / '.env').write_text('private environment')
            (project / 'state.db').write_bytes(b'private state')
            next_revision = advance(upstream, project)
            self.assertEqual(status(project)['state'], 'outdated')
            result = ensure(project)
            self.assertEqual(result['state'], 'current')
            self.assertEqual(result['actual_revision'], next_revision)
            self.assertEqual(source.stat().st_ino, original_inode)
            old = Path(result['previous_source'])
            self.assertEqual(git(old, 'rev-parse', 'HEAD'), revision)
            self.assertEqual((old / 'venv/marker').read_text(), 'preserve dependencies')
            self.assertEqual((old / '.env').read_text(), 'private environment')
            self.assertEqual((project / 'state.db').read_bytes(), b'private state')

    def test_patched_source_is_clean_and_updating_preserves_its_derived_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _ = fixture(Path(temporary))
            editor = project / 'editor'
            checkout(project, editor)
            (editor / 'agent.py').write_text("print('patched')\n")
            export(project, editor)
            source = project / 'hermes-agent'
            ensure(project)
            self.assertEqual(git(source, 'status', '--porcelain'), '')
            derived = git(source, 'rev-parse', 'HEAD')
            self.assertEqual(git(source, 'rev-parse', 'HEAD^'), json.loads((project / 'upstream.lock.json').read_text())['revision'])
            before = (source / '.git/index').read_bytes()
            self.assertEqual(ensure(project)['state'], 'current')
            self.assertEqual((source / '.git/index').read_bytes(), before)
            (editor / 'agent.py').write_text("print('updated-patch')\n")
            export(project, editor)
            self.assertEqual(status(project)['state'], 'patches-outdated')
            result = ensure(project)
            self.assertEqual(result['state'], 'current')
            self.assertEqual(git(Path(result['previous_source']), 'rev-parse', 'HEAD'), derived)
            self.assertEqual(git(source, 'status', '--porcelain'), '')
            self.assertEqual((source / 'agent.py').read_text(), "print('updated-patch')\n")

    def test_existing_staged_patch_checkout_migrates_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, revision = fixture(Path(temporary))
            editor = project / 'editor'
            checkout(project, editor)
            (editor / 'agent.py').write_text("print('patched')\n")
            export(project, editor)
            source = project / 'hermes-agent'
            ensure(project)
            expected = git(source, 'rev-parse', 'HEAD')
            git(source, 'update-ref', 'HEAD', revision, expected)
            metadata = source / '.git/hermes-personal-source.json'
            record = json.loads(metadata.read_text());record.pop('derived_revision');metadata.write_text(json.dumps(record))
            original = fingerprint(source)
            self.assertTrue(git(source, 'status', '--porcelain'))
            self.assertEqual(ensure(project)['state'], 'current')
            self.assertEqual(fingerprint(source), original)
            self.assertEqual(git(source, 'status', '--porcelain'), '')
            self.assertEqual(git(source, 'rev-parse', 'HEAD'), expected)

    def test_old_revision_with_local_edits_cannot_be_used_silently(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, project, _ = fixture(Path(temporary))
            source = project / 'hermes-agent'
            ensure(project)
            (source / 'agent.py').write_text("print('unpublished')\n")
            old_tree = fingerprint(source)
            advance(upstream, project)
            with self.assertRaisesRegex(SourceUpdateError, 'Local work'):
                ensure(project, allow_local_edits=True)
            self.assertEqual(fingerprint(source), old_tree)
            with self.assertRaisesRegex(ValueError, 'locked revision'):
                export(project, source)
            result = ensure(project, preserve_existing=True)
            self.assertEqual((Path(result['previous_source']) / 'agent.py').read_text(), "print('unpublished')\n")
            self.assertEqual(result['state'], 'current')

    def test_intentional_edits_allowed_only_on_current_locked_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _ = fixture(Path(temporary))
            source = project / 'hermes-agent'
            ensure(project)
            (source / 'agent.py').write_text("print('developing')\n")
            self.assertEqual(status(project)['state'], 'local-edits')
            self.assertEqual(ensure(project, allow_local_edits=True)['state'], 'local-edits')
            export(project, source)
            self.assertEqual(ensure(project)['state'], 'current')

    def test_local_commits_on_the_current_base_remain_editable_and_exportable(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _ = fixture(Path(temporary))
            source = project / 'hermes-agent'
            ensure(project)
            git(source, 'config', 'user.name', 'fixture')
            git(source, 'config', 'user.email', 'fixture@example.test')
            (source / 'agent.py').write_text("print('local-commit')\n")
            git(source, 'commit', '-qam', 'local development')
            self.assertEqual(ensure(project, allow_local_edits=True)['state'], 'local-edits')
            export(project, source)
            self.assertEqual(ensure(project, allow_local_edits=True)['state'], 'local-edits')
            (source / 'agent.py').write_text("print('continue-developing')\n")
            self.assertEqual(ensure(project, allow_local_edits=True)['state'], 'local-edits')

    def test_active_source_is_not_switched_even_with_preserve_existing(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, project, revision = fixture(Path(temporary))
            source = project / 'hermes-agent'
            ensure(project)
            advance(upstream, project)
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=source)
            try:
                # Linux detects cwd as well as command paths. Mock the same
                # positive detection on macOS, where /proc is unavailable.
                active = running_processes(source)
                with patch('editable_source.running_processes', return_value=active or [process.pid]):
                    with self.assertRaisesRegex(SourceUpdateError, 'in use'):
                        ensure(project, preserve_existing=True)
                self.assertEqual(git(source, 'rev-parse', 'HEAD'), revision)
                alternate = project / '.local/new-source'
                self.assertEqual(ensure(project, alternate)['state'], 'current')
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_failed_candidate_keeps_old_source_and_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, project, revision = fixture(Path(temporary))
            source = project / 'hermes-agent'
            ensure(project)
            old = fingerprint(source)
            advance(upstream, project)
            with patch('editable_source.checkout', side_effect=OSError('fetch failure')):
                with self.assertRaises(OSError):
                    ensure(project)
            self.assertEqual(git(source, 'rev-parse', 'HEAD'), revision)
            self.assertEqual(fingerprint(source), old)

    def test_process_started_during_fetch_aborts_before_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, project, revision = fixture(Path(temporary))
            source = project / 'hermes-agent'
            ensure(project)
            advance(upstream, project)
            with patch('editable_source.running_processes', side_effect=[[], [12345]]):
                with self.assertRaisesRegex(SourceUpdateError, 'entered use'):
                    ensure(project)
            self.assertEqual(git(source, 'rev-parse', 'HEAD'), revision)

    def test_switch_failure_restores_the_original_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, project, revision = fixture(Path(temporary))
            source = project / 'hermes-agent'
            ensure(project)
            advance(upstream, project)
            rename = Path.rename
            old_tree = fingerprint(source)
            moved = []
            def fail_candidate(path, target):
                if path.parent.name == 'checkout':
                    moved.append(path.name)
                    if len(moved) == 2:
                        raise OSError('fixture switch failure')
                return rename(path, target)
            with patch.object(Path, 'rename', fail_candidate):
                with self.assertRaisesRegex(OSError, 'switch failure'):
                    ensure(project)
            self.assertEqual(git(source, 'rev-parse', 'HEAD'), revision)
            self.assertEqual(fingerprint(source), old_tree)

    def test_changed_lock_during_fetch_never_switches_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, project, revision = fixture(Path(temporary))
            source = project / 'hermes-agent'
            ensure(project)
            advance(upstream, project)
            def prepare_then_change(*args):
                result = checkout(*args)
                lock = project / 'upstream.lock.json'
                lock.write_text(lock.read_text() + '\n')
                return result
            with patch('editable_source.checkout', side_effect=prepare_then_change):
                with self.assertRaisesRegex(SourceUpdateError, 'lock or patches changed'):
                    ensure(project)
            self.assertEqual(git(source, 'rev-parse', 'HEAD'), revision)

    def test_symlink_and_parent_repository_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, project, _ = fixture(Path(temporary))
            ensure(project)
            alias = project / 'alias'
            alias.symlink_to(project / 'hermes-agent')
            with self.assertRaisesRegex(SourceUpdateError, 'symlink'):
                ensure(project, alias)
            with self.assertRaisesRegex(SourceUpdateError, 'project root'):
                ensure(project, project)

    def test_locked_release_tag_is_installed_and_missing_tag_is_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            upstream, project, revision = fixture(Path(temporary))
            git(upstream, 'tag', 'v1.0.0')
            lock = project / 'upstream.lock.json'
            value = json.loads(lock.read_text())
            value['release'] = {'tag': 'v1.0.0', 'commit': revision,
                                'url': str(upstream) + '/releases/tag/v1.0.0'}
            lock.write_text(json.dumps(value))
            source = project / 'hermes-agent'
            ensure(project)
            self.assertEqual(git(source, 'describe', '--tags', '--exact-match', 'HEAD'), 'v1.0.0')
            git(source, 'tag', '-d', 'v1.0.0')
            self.assertEqual(status(project)['state'], 'release-tag-missing')
            with patch('upstream_source.git', wraps=source_git) as operations:
                self.assertEqual(ensure(project)['state'], 'current')
            self.assertFalse(any(call.args[1] == 'fetch' for call in operations.call_args_list))
            self.assertEqual(git(source, 'describe', '--tags', '--exact-match', 'HEAD'), 'v1.0.0')
            (upstream / 'extra.py').write_text('another upstream commit\n')
            git(upstream, 'commit', '-qam', 'different commit')
            git(source, 'fetch', '--depth', '1', 'origin', 'main')
            different = git(source, 'rev-parse', 'FETCH_HEAD')
            git(source, 'tag', '-f', 'v1.0.0', different)
            self.assertEqual(status(project)['state'], 'release-tag-mismatch')
            with self.assertRaisesRegex(SourceUpdateError, 'Local release tag differs'):
                ensure(project)
            self.assertEqual(git(source, 'rev-parse', 'refs/tags/v1.0.0'), different)
            self.assertEqual(git(source, 'rev-parse', 'HEAD'), revision)


if __name__ == '__main__':
    unittest.main()
