import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from dependencies import prepare_store
from configuration import restore_mcp, BRIDGE


class DependenciesTests(unittest.TestCase):
    def test_native_migration_preserves_live_credentials_toggles_and_comments_inputs(self):
        config = {'personal': ['keep'], 'mcp_servers': {
            'one': {'command': 'python', 'args': [BRIDGE, 'one'], 'enabled': False, 'env': {'KEY': 'updated'}},
            'new': {'command': 'uvx', 'args': ['new-mcp']}}}
        restored = restore_mcp(config, {'one': {'command': 'npx', 'args': ['original'], 'cwd': '/project'}})
        self.assertEqual(restored['mcp_servers']['one'], {'command': 'npx', 'args': ['original'],
            'enabled': False, 'env': {'KEY': 'updated'}, 'cwd': '/project'})
        self.assertEqual(restored['mcp_servers']['new'], config['mcp_servers']['new'])
        self.assertEqual(config['mcp_servers']['one']['args'], [BRIDGE, 'one'])

    def test_seed_reuses_packaged_tools_without_overwriting_pm_installed_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packaged, home = root / 'image', root / 'home'
            packaged.mkdir(); home.mkdir()
            baseline = {'schema': 1, 'extras': ['all', 'messaging']}
            (root / 'enabled-features.json').write_text(json.dumps(baseline))
            (packaged / 'uv-v1').mkdir()
            (packaged / 'uv-v1/uv').write_text('image binary')
            (packaged / 'facts.json').write_text(json.dumps({'schema': 1, 'packages': {
                'uv': {'entry': 'uv-v1', 'version': '1'}}}))
            store = prepare_store(home, packaged)
            self.assertEqual(json.loads((home / 'enabled-features.json').read_text()), baseline)
            self.assertEqual((home / 'enabled-features.json').stat().st_mode & 0o777, 0o600)
            self.assertTrue((store / 'uv-v1').is_symlink())
            self.assertEqual((store / 'uv-v1/uv').read_text(), 'image binary')
            (store / 'facts.json').write_text(json.dumps({'schema': 1, 'packages': {'uv': {'version': '2'}}}))
            user_features = {'schema': 1, 'extras': ['mcp']}
            (home / 'enabled-features.json').write_text(json.dumps(user_features))
            prepare_store(home, packaged)
            self.assertEqual(json.loads((home / 'enabled-features.json').read_text()), user_features)
            self.assertEqual(json.loads((store / 'facts.json').read_text())['packages']['uv']['version'], '2')
