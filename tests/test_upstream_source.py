import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from prepare_source import prepare
from upstream_source import checkout, inputs_digest
from export_patch import export
from source_tree import fingerprint


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE, text=True).strip()


def fixture(root):
    upstream, project = root / 'upstream', root / 'project'
    upstream.mkdir()
    project.mkdir()
    git(upstream, 'init', '-q', '-b', 'main')
    git(upstream, 'config', 'user.name', 'fixture')
    git(upstream, 'config', 'user.email', 'fixture@example.test')
    (upstream / 'agent.py').write_text("print('pinned-source')\n")
    (upstream / '.gitignore').write_text('.cache/\nvenv/\n')
    (upstream / 'tests').mkdir()
    (upstream / 'tests/test_agent.py').write_text('def test_agent(): pass\n')
    (upstream / 'tests/test_extra.py').write_text('def test_extra(): pass\n')
    (upstream / 'extra.py').write_text("print('pinned-extra')\n")
    git(upstream, 'add', '.')
    git(upstream, 'commit', '-qm', 'pinned upstream')
    revision = git(upstream, 'rev-parse', 'HEAD')
    patches = project / 'patches'
    patches.mkdir()
    (patches / 'series.json').write_text(json.dumps({
        'schema': 1,
        'groups': [
            {'path': 'patches/0001-agent.patch', 'description': 'Agent runtime fixture',
             'files': ['agent.py'], 'native_tests': ['tests/test_agent.py']},
            {'path': 'patches/0002-extra.patch', 'description': 'Additional source fixture',
             'files': ['extra.py', 'new.py'], 'native_tests': ['tests/test_extra.py']},
        ],
    }))
    (project / 'upstream.lock.json').write_text(json.dumps({'schema': 1, 'repository': str(upstream),
        'revision': revision, 'patches': [], 'native_tests': []}))
    return upstream, project, revision


def locked_artifacts(project):
    lock_bytes = (project / 'upstream.lock.json').read_bytes()
    lock = json.loads(lock_bytes)
    patches = {entry['path']: (project / entry['path']).read_bytes()
               for entry in lock['patches']}
    return lock_bytes, patches


class UpstreamTests(unittest.TestCase):
    def test_build_uses_exact_revision_and_preserves_unpublished_checkout_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream, project, revision = fixture(root)
            checkout(project, project / 'hermes-agent')
            (project / 'hermes-agent/agent.py').write_text("print('unpublished-edit')\n")
            (project / 'hermes-agent/.cache').mkdir()
            (project / 'hermes-agent/.cache/private.json').write_text('private fixture')
            (upstream / 'agent.py').write_text("print('new-main')\n")
            git(upstream, 'commit', '-qam', 'upstream moved')
            output = root / 'build'
            provenance = prepare(project, output)
            self.assertEqual(provenance['upstream_revision'], revision)
            self.assertEqual(subprocess.check_output([sys.executable, str(output / 'agent.py')], text=True).strip(), 'pinned-source')
            self.assertEqual((project / 'hermes-agent/agent.py').read_text(), "print('unpublished-edit')\n")
            self.assertFalse((output / '.cache').exists())
            self.assertFalse((output / '.git').exists())
            with self.assertRaises(FileExistsError):
                checkout(project, project / 'hermes-agent')
            with self.assertRaises(FileExistsError):
                prepare(project, output)

    def test_grouped_patches_checksums_test_union_and_fresh_build_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, _ = fixture(root)
            source = project / 'hermes-agent'
            checkout(project, source)
            (source / 'agent.py').write_text("print('patched-source')\n")
            (source / 'new.py').write_text("print('new-code')\n")
            git(source, 'add', 'new.py')
            original_inputs = inputs_digest(project)
            lock = export(project, source, ['tests/test_extra.py'])
            self.assertNotEqual(inputs_digest(project), original_inputs)
            self.assertEqual([entry['path'] for entry in lock['patches']], [
                'patches/0001-agent.patch', 'patches/0002-extra.patch'])
            self.assertEqual(lock['native_tests'], ['tests/test_agent.py', 'tests/test_extra.py'])
            for entry in lock['patches']:
                data = (project / entry['path']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])
                self.assertEqual(entry['native_tests'], [
                    'tests/test_agent.py' if entry['path'].endswith('0001-agent.patch')
                    else 'tests/test_extra.py'])

            before_lock, before_patches = locked_artifacts(project)
            with self.assertRaisesRegex(ValueError, 'Declare --test paths'):
                export(project, source, ['tests/test_undeclared.py'])
            self.assertEqual(locked_artifacts(project), (before_lock, before_patches))

            output = root / 'build'
            provenance = prepare(project, output)
            self.assertEqual(fingerprint(source), fingerprint(output))
            self.assertEqual(subprocess.check_output([sys.executable, str(output / 'agent.py')], text=True).strip(), 'patched-source')
            self.assertTrue((output / 'new.py').exists())
            self.assertEqual([entry['path'] for entry in provenance['patches']], [
                'patches/0001-agent.patch', 'patches/0002-extra.patch'])

    def test_unclassified_edit_and_duplicate_owner_fail_before_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, _ = fixture(root)
            source = project / 'hermes-agent'
            checkout(project, source)
            (source / 'agent.py').write_text("print('classified-source')\n")
            export(project, source)
            before = locked_artifacts(project)

            (source / 'unclassified.py').write_text("print('unowned-source')\n")
            git(source, 'add', 'unclassified.py')
            with self.assertRaisesRegex(ValueError, 'Classify core changes'):
                export(project, source)
            self.assertEqual(locked_artifacts(project), before)

            series_path = project / 'patches/series.json'
            series = json.loads(series_path.read_text())
            series['groups'][1]['files'].append('agent.py')
            series_path.write_text(json.dumps(series))
            with self.assertRaisesRegex(ValueError, 'more than one patch group'):
                export(project, source)
            self.assertEqual(locked_artifacts(project), before)

    def test_reexport_stays_grouped_removes_inactive_locked_patch_and_keeps_unmanaged_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, _ = fixture(root)
            source = project / 'hermes-agent'
            checkout(project, source)
            (source / 'agent.py').write_text("print('agent-change')\n")
            (source / 'extra.py').write_text("print('extra-change')\n")
            first = export(project, source)
            self.assertEqual(len(first['patches']), 2)
            unmanaged = project / 'patches/operator-owned.patch'
            unmanaged.write_text('retain unmanaged data\n')

            (source / 'extra.py').write_text("print('pinned-extra')\n")
            second = export(project, source)
            self.assertEqual([entry['path'] for entry in second['patches']], ['patches/0001-agent.patch'])
            self.assertTrue((project / 'patches/0001-agent.patch').is_file())
            self.assertFalse((project / 'patches/0002-extra.patch').exists())
            self.assertEqual(unmanaged.read_text(), 'retain unmanaged data\n')
            self.assertIn('diff --git a/agent.py b/agent.py',
                          (project / 'patches/0001-agent.patch').read_text())
            self.assertNotIn('diff --git a/extra.py b/extra.py',
                             (project / 'patches/0001-agent.patch').read_text())

            output = root / 'reexported-build'
            prepare(project, output)
            self.assertEqual(fingerprint(source), fingerprint(output))

    def test_modified_patch_or_private_export_fails_before_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, project, _ = fixture(root)
            source = project / 'hermes-agent'
            checkout(project, source)
            (source / '.env').write_text('PRIVATE_FIXTURE=value')
            git(source, 'add', '-f', '.env')
            with self.assertRaisesRegex(ValueError, 'Private state'):
                export(project, source, ['tests/test_agent.py'])
            git(source, 'reset', '-q')
            (source / 'agent.py').write_text("print('patched-source')\n")
            export(project, source, ['tests/test_agent.py'])
            (project / 'patches/0001-agent.patch').write_text('changed after review')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                prepare(project, root / 'build')
            self.assertFalse((root / 'build').exists())
