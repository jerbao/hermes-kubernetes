import configparser
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('configure_drive_backup',
    Path(__file__).resolve().parents[1] / 'vm/configure_drive_backup.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class DriveConfigurationTests(unittest.TestCase):
    def test_initial_configuration_uses_owned_client_and_private_generated_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            client, config = root / 'client.json', root / 'rclone.conf'
            client.write_text(json.dumps({'installed': {'client_id': 'owned-client', 'client_secret': 'secret'}}))
            generated = []
            def execute(command, **kwargs):
                if command[1] == 'obscure':
                    generated.append(kwargs['input'])
                    return subprocess.CompletedProcess(command, 0, stdout='obscured-' + str(len(generated)))
                candidate = Path(command[command.index('--config') + 1])
                value = configparser.RawConfigParser()
                value.read(candidate)
                self.assertEqual(value['hermes-drive']['client_id'], 'owned-client')
                value['hermes-drive']['token'] = 'synthetic-token'
                with candidate.open('w') as output:
                    value.write(output)
                return subprocess.CompletedProcess(command, 0)
            with patch.object(configure.subprocess, 'run', side_effect=execute):
                configure.configure(config, oauth_client=client)
            value = configparser.RawConfigParser()
            value.read(config)
            self.assertEqual(value['hermes-backup']['password'], 'obscured-1')
            self.assertEqual(value['hermes-backup']['password2'], 'obscured-2')
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(generated), 2)
            self.assertNotEqual(generated[0], generated[1])

    def test_reauthorization_preserves_recovery_key_and_previous_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            client, config = root / 'client.json', root / 'rclone.conf'
            client.write_text(json.dumps({'installed': {'client_id': 'owned-client', 'client_secret': 'owned-secret'}}))
            value = configparser.RawConfigParser()
            value['hermes-drive'] = {'type': 'drive', 'token': 'previous-token'}
            crypt = {'type': 'crypt', 'remote': 'hermes-drive:hermes-backups/example',
                     'password': 'existing-recovery-key', 'password2': 'existing-salt'}
            value['hermes-backup'] = crypt
            with config.open('w') as output:
                value.write(output)
            config.chmod(0o600)
            original = config.read_bytes()
            def authorize(command, **kwargs):
                self.assertNotIn('owned-secret', command)
                candidate = Path(command[command.index('--config') + 1])
                parsed = configparser.RawConfigParser()
                parsed.read(candidate)
                self.assertEqual(parsed['hermes-drive']['scope'], 'drive.file')
                parsed['hermes-drive']['token'] = 'renewed-token'
                with candidate.open('w') as output:
                    parsed.write(output)
                return subprocess.CompletedProcess(command, 0)
            with patch.object(configure.subprocess, 'run', side_effect=authorize):
                configure.configure(config, oauth_client=client, reauthorize=True)
            value.read(config)
            self.assertEqual(dict(value['hermes-backup']), crypt)
            self.assertEqual(value['hermes-drive']['token'], 'renewed-token')
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(next(root.glob('before-oauth-*.conf')).read_bytes(), original)

    def test_failed_authorization_does_not_replace_current_config_or_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            client, config = root / 'client.json', root / 'rclone.conf'
            client.write_text(json.dumps({'installed': {'client_id': 'client', 'client_secret': 'secret'}}))
            config.write_text('[hermes-backup]\ntype=crypt\npassword=unchanged\n')
            original = config.read_bytes()
            with patch.object(configure.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaises(RuntimeError):
                    configure.configure(config, oauth_client=client, reauthorize=True)
            self.assertEqual(config.read_bytes(), original)
            self.assertFalse(list(root.glob('before-oauth-*')))
            self.assertFalse(list(root.glob('.drive-auth-*')))

    def test_service_account_json_and_unrequested_replacement_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            client, config = root / 'client.json', root / 'rclone.conf'
            client.write_text('{"type":"service_account"}')
            with self.assertRaises(ValueError):
                configure.configure(config, oauth_client=client)
            self.assertFalse(config.exists())
            config.write_text('existing')
            with self.assertRaises(FileExistsError):
                configure.configure(config, oauth_client=client)
            self.assertEqual(config.read_text(), 'existing')
