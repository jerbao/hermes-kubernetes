import configparser
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from backup_job import backup
from drive_backup import Remote, restore_state
from restore_workspace import restore

RCLONE = os.environ.get('HERMES_RCLONE', shutil.which('rclone'))


@unittest.skipUnless(RCLONE, 'Install the pinned rclone to exercise encrypted transport')
class EncryptedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'SOUL.md').write_text('private personality')
        (self.source / 'SOUL.md').chmod(0o600)
        self.database = sqlite3.connect(self.source / 'state.db')
        self.database.execute('PRAGMA journal_mode=WAL')
        self.database.execute('CREATE TABLE messages (body TEXT)')
        self.database.execute("INSERT INTO messages VALUES ('committed in WAL')")
        self.database.commit()
        self.addCleanup(self.database.close)
        value = configparser.RawConfigParser()
        password = subprocess.run([RCLONE, 'obscure', '-'], input='test recovery key',
            capture_output=True, text=True, check=True).stdout.strip()
        value['hermes-backup'] = {'type': 'crypt', 'remote': str(self.base / 'encrypted'),
            'password': password, 'filename_encryption': 'standard'}
        self.config = self.base / 'rclone.conf'
        with self.config.open('w') as output:
            value.write(output)
        self.config.chmod(0o600)
        self.remote = Remote(self.config, rclone=RCLONE)
        self.addCleanup(self.remote.__exit__, None, None, None)

    def archive(self, kind='state'):
        status = backup(self.source, self.base / 'backups', keep=10, kind=kind)
        return self.base / 'backups' / status['archive']

    def test_encrypted_roundtrip_preserves_wal_and_rejects_overwrite(self):
        archive = self.archive()
        original = self.config.read_bytes()
        status = self.remote.upload(archive, 'state')
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.remote.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.remote.list('state'), [status])
        encrypted = [p for p in (self.base / 'encrypted').rglob('*') if p.is_file()]
        self.assertEqual(len(encrypted), 2)
        for path in encrypted:
            self.assertNotIn(b'private personality', path.read_bytes())
            self.assertNotIn(b'committed in WAL', path.read_bytes())
            self.assertNotIn('receipt', path.name)
        downloaded = self.base / 'download.tar.gz'
        self.remote.download('state', 'latest', downloaded)
        self.assertEqual(downloaded.read_bytes(), archive.read_bytes())
        self.assertEqual(downloaded.stat().st_mode & 0o777, 0o600)
        restored = self.base / 'restored'
        restore_state(downloaded, restored, sha256=status['sha256'])
        with closing(sqlite3.connect(restored / 'state.db')) as database:
            self.assertEqual(database.execute('SELECT body FROM messages').fetchone()[0], 'committed in WAL')
        self.assertEqual((restored / 'SOUL.md').read_text(), 'private personality')
        self.assertEqual((restored / 'SOUL.md').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            restore_state(downloaded, restored, sha256=status['sha256'])
        with self.assertRaises(FileExistsError):
            self.remote.download('state', 'latest', downloaded)
        with self.assertRaises(FileExistsError):
            self.remote.upload(archive, 'state')

    def test_workspace_roundtrip_preserves_links_and_uncommitted_work(self):
        (self.source / 'skill').symlink_to('SOUL.md')
        subprocess.run(['git', 'init', '-q', str(self.source)], check=True)
        subprocess.run(['git', '-C', str(self.source), 'add', 'SOUL.md'], check=True)
        archive = self.archive('workspace')
        status = self.remote.upload(archive, 'workspace')
        downloaded = self.base / 'workspace.tar.gz'
        self.remote.download('workspace', status['snapshot'], downloaded)
        restored = self.base / 'restored'
        restore(downloaded, restored, sha256=status['sha256'])
        self.assertEqual(os.readlink(restored / 'skill'), 'SOUL.md')
        result = subprocess.run(['git', '-C', str(restored), 'diff', '--cached', '--name-only'],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), 'SOUL.md')

    def test_failed_verification_never_commits_or_prunes_previous_backups(self):
        first = self.remote.upload(self.archive(), 'state', keep=1)
        second = self.archive()
        run = self.remote.run
        def corrupt_download(command, *args, **kwargs):
            result = run(command, *args, **kwargs)
            if command == 'copyto' and str(args[0]).startswith('hermes-backup:'):
                Path(args[1]).write_bytes(b'corrupted transfer')
            return result
        with patch.object(self.remote, 'run', side_effect=corrupt_download):
            with self.assertRaises(ValueError):
                self.remote.upload(second, 'state', keep=1)
        self.assertEqual(self.remote.list('state'), [first])
        self.assertIsNone(self.remote.receipt('state', second.name.removesuffix('.tar.gz')))
        self.remote.download('state', first['snapshot'], self.base / 'still-restorable.tar.gz')

    def test_retention_is_collection_scoped_and_preserves_unknown_objects(self):
        workspace = self.remote.upload(self.archive('workspace'), 'workspace')
        self.remote.run('mkdir', self.remote.path('state', 'personal-folder'))
        first = self.remote.upload(self.archive(), 'state', keep=1)
        second = self.remote.upload(self.archive(), 'state', keep=1)
        self.assertEqual(self.remote.list('state'), [second])
        self.assertIsNone(self.remote.receipt('state', first['snapshot']))
        self.assertEqual(self.remote.list('workspace'), [workspace])
        objects = json.loads(self.remote.run('lsjson', self.remote.path('state'), '--dirs-only'))
        self.assertIn('personal-folder', [entry['Name'] for entry in objects])

    def test_corrupted_encrypted_object_is_rejected_without_publishing_download(self):
        status = self.remote.upload(self.archive(), 'state')
        encrypted = sorted((p for p in (self.base / 'encrypted').rglob('*') if p.is_file()),
                           key=lambda p: p.stat().st_size, reverse=True)[0]
        content = bytearray(encrypted.read_bytes())
        content[-1] ^= 1
        encrypted.write_bytes(content)
        destination = self.base / 'download.tar.gz'
        with self.assertRaises(RuntimeError):
            self.remote.download('state', status['snapshot'], destination)
        self.assertFalse(destination.exists())

    def test_unencrypted_remote_and_path_injection_are_refused(self):
        with self.assertRaises(ValueError):
            Remote(self.config, remote='hermes-backup:../other', rclone=RCLONE)
        with self.assertRaises(ValueError):
            self.remote.path('../other')
        with self.assertRaises(ValueError):
            self.remote.receipt('state', '../other')
        self.config.write_text('[plain]\ntype=local\n')
        with self.assertRaises(ValueError):
            Remote(self.config, remote='plain:', rclone=RCLONE)

    def test_explicit_recovery_retries_temporarily_invisible_completion_receipt(self):
        status = self.remote.upload(self.archive(), 'state')
        run = self.remote.run
        misses = 0
        def delayed_receipt(command, *args, **kwargs):
            nonlocal misses
            if command == 'cat' and misses < 2:
                misses += 1
                return None
            return run(command, *args, **kwargs)
        with patch.object(self.remote, 'run', side_effect=delayed_receipt), \
                patch('drive_backup.time') as clock:
            self.remote.download('state', status['snapshot'], self.base / 'download.tar.gz')
        # Mock only this module's clock; subprocess uses time.sleep too.
        self.assertEqual(clock.sleep.call_args_list, [unittest.mock.call(1), unittest.mock.call(2)])


if __name__ == '__main__':
    unittest.main()
