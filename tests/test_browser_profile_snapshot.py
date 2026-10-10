import sqlite3
from contextlib import closing
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'vm'), str(ROOT / 'runtime')]
from browser_profile_snapshot import retained
from state_snapshot import build_snapshot
from backup_runtime import verify_archive


class BrowserSnapshotTests(unittest.TestCase):
    def test_persistent_browser_state_and_live_cookie_wal_survive_without_process_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            profile = base / 'profile'
            for name in ['Default/Local Storage/leveldb', 'Default/Sessions', 'Default/Cache']:
                (profile / name).mkdir(parents=True)
            (profile / 'Local State').write_text('private browser settings')
            (profile / 'Default/Preferences').write_text('private preferences')
            (profile / 'Default/Sessions/Session_123').write_bytes(b'private session')
            (profile / 'Default/Local Storage/leveldb/000123.log').write_bytes(b'persistent storage')
            (profile / 'Default/Local Storage/leveldb/LOCK').write_bytes(b'')
            (profile / 'Default/Cache/discard').write_bytes(b'cache')
            (profile / 'SingletonSocket').symlink_to('/tmp/nonexistent-browser-socket')
            db = sqlite3.connect(profile / 'Default/Cookies')
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE cookies (value TEXT)')
                db.execute("INSERT INTO cookies VALUES ('committed WAL cookie')")
                db.commit()
                (profile / 'Default/Cookies').chmod(0o600)
                output = base / 'profile.tar.gz'
                build_snapshot(profile, output, filter_func=retained)
                self.assertEqual(verify_archive(output)['databases'], 1)
                restored = base / 'restored'
                with tarfile.open(output) as archive:
                    archive.extractall(restored, filter='data')
                    for name in ['SingletonSocket', 'Default/Cookies-wal', 'Default/Cookies-shm',
                                 'Default/Cache', 'Default/Local Storage/leveldb/LOCK']:
                        self.assertNotIn(name, archive.getnames())
                self.assertEqual((restored / 'Default/Cookies').stat().st_mode & 0o777, 0o600)
                with closing(sqlite3.connect(restored / 'Default/Cookies')) as copy:
                    self.assertEqual(copy.execute('SELECT value FROM cookies').fetchone()[0], 'committed WAL cookie')
                for name in ['Local State', 'Default/Preferences', 'Default/Sessions/Session_123',
                             'Default/Local Storage/leveldb/000123.log']:
                    self.assertEqual((restored / name).read_bytes(), (profile / name).read_bytes())
            finally:
                db.close()

    def test_unexpected_persistent_symlink_aborts_instead_of_losing_personal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            profile = base / 'profile'
            profile.mkdir()
            (profile / 'Local State').symlink_to('/tmp/missing-private-state')
            output = base / 'profile.tar.gz'
            with self.assertRaises(RuntimeError):
                build_snapshot(profile, output, filter_func=retained)
            self.assertFalse(output.exists())
