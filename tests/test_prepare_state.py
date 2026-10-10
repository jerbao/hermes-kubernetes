from pathlib import Path
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from backup_job import backup
from drive_backup import restore_state
from prepare_state import prepare, separate_env_files


class StatePreparationTests(unittest.TestCase):
    def test_moa_history_survives_adoption_and_subsequent_state_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / 'recovered'
            home = workspace / '.hermes'
            traces = {
                'moa-traces/session.jsonl': b'{"phase":"synthesis","text":"original"}\n',
                'profiles/research/moa-traces/session.jsonl': b'{"phase":"proposal"}\n',
            }
            for name, contents in traces.items():
                path = home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
                path.chmod(0o600)
            (home / 'cache').mkdir()
            (home / 'cache/rebuildable').write_text('generated')
            prepared = base / 'prepared'
            prepare(workspace, Path('/home/example'), prepared)
            # New traces written by the runtime must be protected too.
            traces['moa-traces/new-session.jsonl'] = b'{"phase":"critique"}\n'
            (prepared / 'moa-traces/new-session.jsonl').write_bytes(traces['moa-traces/new-session.jsonl'])
            (prepared / 'moa-traces/new-session.jsonl').chmod(0o600)
            status = backup(prepared, base / 'backups')
            recovered = base / 'restored'
            restore_state(base / 'backups' / status['archive'], recovered, sha256=status['sha256'])
            for name, contents in traces.items():
                self.assertEqual((recovered / name).read_bytes(), contents)
                self.assertEqual((recovered / name).stat().st_mode & 0o777, 0o600)
            self.assertFalse((recovered / 'cache').exists())

    def test_recovered_plugins_skills_and_independent_profile_environments(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / 'recovered'
            state = workspace / '.hermes'
            (state / 'plugins').mkdir(parents=True)
            (state / 'skills').mkdir()
            (state / 'profiles/research').mkdir(parents=True)
            (state / 'profiles/analyst/lsp').mkdir(parents=True)
            (state / 'profiles/analyst/lsp/rebuildable').write_text('generated package')
            env = state / 'profiles/research/.env'
            env.write_text('shared private fixture')
            env.chmod(0o600)
            (state / 'profiles/analyst/.env').symlink_to('/home/example/.hermes/profiles/research/.env')
            (state / 'CLAUDE.md').write_text('agent instructions')
            (state / 'AGENTS.md').symlink_to('CLAUDE.md')
            plugin = workspace / 'code-repos/plugin'
            plugin.mkdir(parents=True)
            (plugin / 'main.py').write_text('plugin implementation')
            (state / 'plugins/custom').symlink_to('/home/example/code-repos/plugin')
            external_skill = workspace / '.agents/skills/custom'
            external_skill.mkdir(parents=True)
            (external_skill / 'SKILL.md').write_text('personal skill')
            (state / 'skills/custom').symlink_to('../../.agents/skills/custom')
            (state / '.hermes').symlink_to('.')
            target = base / 'prepared'
            before = os.readlink(state / 'profiles/analyst/.env')
            self.assertTrue(prepare(workspace, Path('/home/example'), target, check=True)['check'])
            self.assertFalse(target.exists())
            report = prepare(workspace, Path('/home/example'), target)
            self.assertEqual(report['symlinks'], 1)
            self.assertFalse((target / 'plugins/custom').is_symlink())
            self.assertEqual((target / 'plugins/custom/main.py').read_text(), 'plugin implementation')
            self.assertEqual((target / 'skills/custom/SKILL.md').read_text(), 'personal skill')
            self.assertFalse((target / 'profiles/analyst/lsp').exists())
            self.assertEqual(os.readlink(state / 'profiles/analyst/.env'), before)
            status = backup(target, base / 'backups')
            restored = base / 'restored'
            restore_state(base / 'backups' / status['archive'], restored, sha256=status['sha256'])
            alias = restored / 'profiles/analyst/.env'
            self.assertFalse(alias.is_symlink())
            self.assertTrue((restored / 'AGENTS.md').is_symlink())
            (restored / 'profiles/research/.env').write_text('updated shared environment')
            self.assertEqual(alias.read_text(), 'shared private fixture')

    def test_default_only_and_arbitrary_profile_counts_have_independent_environment_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            self.assertEqual(separate_env_files(home), {'profiles': 1, 'copied': 0, 'created': 1})
            (home / '.env').write_text('preserved original credentials')
            for index in range(7):
                profile = home / 'profiles' / f'bot-{index}'
                profile.mkdir(parents=True)
                (profile / 'config.yaml').write_text('{}')
                (profile / '.env').symlink_to('../../.env')
            report = separate_env_files(home)
            self.assertEqual(report, {'profiles': 8, 'copied': 7, 'created': 0})
            for index in range(7):
                path = home / 'profiles' / f'bot-{index}' / '.env'
                self.assertFalse(path.is_symlink())
                self.assertEqual(path.read_text(), 'preserved original credentials')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            (home / 'profiles/bot-0/.env').write_text('only this bot changes')
            self.assertEqual((home / '.env').read_text(), 'preserved original credentials')
            self.assertEqual((home / 'profiles/bot-1/.env').read_text(), 'preserved original credentials')
            self.assertEqual(separate_env_files(home), {'profiles': 8, 'copied': 0, 'created': 0})

    def test_environment_link_outside_bot_home_is_refused_before_other_profiles_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            profile = home / 'profiles/example'
            profile.mkdir(parents=True)
            (profile / 'config.yaml').write_text('{}')
            (profile / '.env').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                separate_env_files(home)
            self.assertFalse((home / '.env').exists())
            self.assertTrue((profile / '.env').is_symlink())

    def test_missing_legacy_profile_environment_gets_an_independent_copy_of_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            (home / '.env').write_text('existing default credentials')
            profile = home / 'profiles/example'
            profile.mkdir(parents=True)
            (profile / 'config.yaml').write_text('{}')
            self.assertEqual(separate_env_files(home), {'profiles': 2, 'copied': 0, 'created': 1})
            self.assertEqual((profile / '.env').read_text(), 'existing default credentials')
            self.assertEqual((profile / '.env').stat().st_mode & 0o777, 0o600)
            (profile / '.env').write_text('profile credentials')
            self.assertEqual((home / '.env').read_text(), 'existing default credentials')

    def test_hardlinked_environments_are_separated_and_keep_current_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            default = home / '.env'
            default.write_text('existing credentials')
            profile = home / 'profiles/example'
            profile.mkdir(parents=True)
            (profile / 'config.yaml').write_text('{}')
            os.link(default, profile / '.env')
            self.assertEqual(separate_env_files(home), {'profiles': 2, 'copied': 2, 'created': 0})
            self.assertNotEqual(default.stat().st_ino, (profile / '.env').stat().st_ino)
            (profile / '.env').write_text('profile credentials')
            self.assertEqual(default.read_text(), 'existing credentials')
            self.assertEqual(separate_env_files(home), {'profiles': 2, 'copied': 0, 'created': 0})

    def test_escapes_missing_dependencies_cycles_and_existing_destination_do_not_modify_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / 'recovered'
            state = workspace / '.hermes'
            state.mkdir(parents=True)
            (state / 'SOUL.md').write_text('preserve')
            link = state / 'unsafe'
            for target in ['/etc/passwd', '../../outside', '/home/example/missing', 'unsafe']:
                with self.subTest(target=target):
                    link.symlink_to(target)
                    destination = base / 'prepared'
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        prepare(workspace, Path('/home/example'), destination)
                    self.assertFalse(destination.exists())
                    self.assertEqual(os.readlink(link), target)
                    link.unlink()
            destination = base / 'existing'
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                prepare(workspace, Path('/home/example'), destination)
            self.assertEqual((state / 'SOUL.md').read_text(), 'preserve')
