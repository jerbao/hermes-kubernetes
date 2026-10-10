"""A newly added registry package installs, pins and rebuilds outside Hermes."""
import functools
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile

from packages import (active_package_versions, baked_package_versions, npm_request, python_request,
                      prepare, update_package, smoke_mcp, installation_recipe, restore_package)


class PackagesTests(unittest.TestCase):
    def test_versioned_recipe_survives_a_new_profile_and_failed_restore_keeps_pointer(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();old=root/'old';new=root/'new'
            argv=['npx','-y','fixture@1.0.0'];kind,request,key=__import__('packages').package_identity(argv)
            record={'binary':'node_modules/.bin/fixture','manifest':{'dependencies':{'fixture':'1.0.0'}},'lock':{'lockfileVersion':3,'packages':{'node_modules/fixture':{'version':'1.0.0'}}}}
            path=old/'package-recipes'/(key+'.json');path.parent.mkdir(parents=True);path.write_text(json.dumps(record))
            exported=installation_recipe(argv,old,baked=root/'no-image')
            self.assertEqual(exported['recipe']['lock'],record['lock'])
            def install(stage, request, recipe, env):
                self.assertEqual(recipe['lock'],record['lock'])
                binary=stage/recipe['binary'];binary.parent.mkdir(parents=True);binary.write_text('fixture');return recipe
            with patch('packages.install_npm',side_effect=install):
                result=restore_package(argv,new,{},exported,cache=root/'cache',smoke=lambda *_:None)
                self.assertEqual(result['status'],'restored')
                before=next((new/'package-pointers').glob('*.json')).read_bytes()
                def fail(*_):raise ValueError('invalid handshake')
                with self.assertRaises(ValueError):restore_package(argv,new,{},exported,cache=root/'cache',smoke=fail)
                self.assertEqual(next((new/'package-pointers').glob('*.json')).read_bytes(),before)
                self.assertEqual(installation_recipe(argv,new,baked=root/'no-image')['recipe'],record)

    @unittest.skipUnless(shutil.which('uv'), 'uv integration runs in the MCP image')
    def test_python_package_rebuild_uses_requirements_without_uv_diagnostics(self):
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, 'w') as archive:
            files = {'fixture_ui_mcp.py': 'import json,os\ndef main(): print(json.dumps({"value":os.environ["TOKEN"]}))\n',
                'fixture_ui_mcp-1.0.0.dist-info/METADATA': 'Metadata-Version: 2.1\nName: fixture-ui-mcp\nVersion: 1.0.0\n',
                'fixture_ui_mcp-1.0.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
                'fixture_ui_mcp-1.0.0.dist-info/entry_points.txt': '[console_scripts]\nfixture-ui-mcp = fixture_ui_mcp:main\n'}
            for name, value in files.items():
                archive.writestr(name, value)
            archive.writestr('fixture_ui_mcp-1.0.0.dist-info/RECORD',
                            ''.join(name + ',,\n' for name in files))
        class Index(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_HEAD(self): self.do_GET()
            def do_GET(self):
                if self.path.startswith('/simple/'):
                    data = b'<a href="/fixture_ui_mcp-1.0.0-py3-none-any.whl">fixture</a>'
                elif self.path.endswith('.whl'):
                    data = wheel.getvalue()
                else:
                    self.send_error(404); return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if self.path.startswith('/simple/') else 'application/octet-stream')
                self.send_header('Content-Length', str(len(data))); self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Index)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                env = {**os.environ, 'TOKEN': 'python-profile-only', 'UV_INDEX_URL':
                       f'http://127.0.0.1:{server.server_port}/simple', 'UV_CACHE_DIR': str(home / 'uv-cache')}
                argv = ['uvx', 'fixture-ui-mcp']
                try:
                    command = prepare(argv, home / 'profile', env, cache=home / 'cache')
                except ValueError:
                    self.fail(next((home / 'cache').glob('*.failure.log')).read_text())
                self.assertEqual(json.loads(subprocess.check_output(command, env=env, text=True)),
                                 {'value': 'python-profile-only'})
                update = update_package(argv, home / 'profile', env, cache=home / 'cache', version='1.0.0',
                    smoke=lambda cmd, child_env: self.assertEqual(
                        json.loads(subprocess.check_output(cmd, env=child_env, text=True))['value'], 'python-profile-only'))
                self.assertEqual(update['status'], 'updated')
                shutil.rmtree(home / 'cache'); shutil.rmtree(home / 'uv-cache')
                command = prepare(argv, home / 'profile', env, cache=home / 'cache')
                self.assertEqual(json.loads(subprocess.check_output(command, env=env, text=True)),
                                 {'value': 'python-profile-only'})
                recipe = next((home / 'profile/package-recipes').glob('*.json'))
                lock = recipe.read_bytes()
                self.assertNotIn('Using Python', json.loads(lock)['requirements'])
                self.assertNotIn('python-profile-only', lock.decode())
                shutil.rmtree(home / 'cache'); shutil.rmtree(home / 'uv-cache')
                command = prepare(argv, home / 'profile', env, cache=home / 'cache')
                self.assertEqual(json.loads(subprocess.check_output(command, env=env, text=True)),
                                 {'value': 'python-profile-only'})
                self.assertEqual(recipe.read_bytes(), lock)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    @unittest.skipUnless(shutil.which('npm') and shutil.which('node'), 'npm integration runs in the MCP image')
    def test_new_package_is_locked_and_rebuilt_after_deleting_the_executor_cache(self):
        versions = {}
        for version in ['1.0.0', '2.0.0']:
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w:gz') as archive:
                files = {'package.json': json.dumps({'name': 'fixture-ui-mcp', 'version': version,
                          'bin': {'fixture-ui-mcp': 'index.js'}}),
                         'index.js': '#!/usr/bin/env node\nconsole.log(JSON.stringify({version:' + json.dumps(version) + ',token:process.env.TOKEN}));\n'}
                for name, value in files.items():
                    info = tarfile.TarInfo('package/' + name)
                    data = value.encode(); info.size = len(data); info.mode = 0o755
                    archive.addfile(info, io.BytesIO(data))
            versions[version] = output.getvalue()
        current = ['1.0.0']
        class Registry(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                if self.path == '/fixture-ui-mcp':
                    base = f'http://127.0.0.1:{self.server.server_port}'
                    data = json.dumps({'name': 'fixture-ui-mcp', 'dist-tags': {'latest': current[0]},
                        'versions': {v: {'name': 'fixture-ui-mcp', 'version': v,
                            'bin': {'fixture-ui-mcp': 'index.js'}, 'dist': {
                            'tarball': base + '/package-' + v + '.tgz',
                            'shasum': hashlib.sha1(t).hexdigest()}} for v, t in versions.items()}}).encode()
                elif self.path.startswith('/package-'):
                    data = versions[self.path.removeprefix('/package-').removesuffix('.tgz')]
                else:
                    self.send_error(404); return
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                env = {**os.environ, 'TOKEN': 'profile-only', 'NPM_CONFIG_REGISTRY': f'http://127.0.0.1:{server.server_port}',
                       'NPM_CONFIG_CACHE': str(home / 'npm-cache')}
                argv = ['npx', '-y', 'fixture-ui-mcp@latest']
                command = prepare(argv, home / 'profile', env, cache=home / 'cache')
                first = json.loads(subprocess.check_output(command, env=env, text=True))
                self.assertEqual(first, {'version': '1.0.0', 'token': 'profile-only'})
                current[0] = '2.0.0'
                update = update_package(argv, home / 'profile', env, cache=home / 'cache',
                    smoke=lambda cmd, child_env: self.assertEqual(
                        json.loads(subprocess.check_output(cmd, env=child_env, text=True))['version'], '2.0.0'))
                self.assertEqual(update['status'], 'updated')
                self.assertEqual(update['activation'], 'next-connection')
                self.assertIn('fixture-ui-mcp@2.0.0', update['versions'])
                self.assertEqual(active_package_versions(argv, home / 'profile'), update['versions'])
                pointer = next((home / 'profile/package-pointers').glob('*.json'))
                generation = json.loads(pointer.read_text())['generation']
                with self.assertRaisesRegex(ValueError, 'fixture smoke failure'):
                    update_package(argv, home / 'profile', env, cache=home / 'cache',
                        smoke=lambda *_: (_ for _ in ()).throw(ValueError('fixture smoke failure')))
                self.assertEqual(json.loads(pointer.read_text())['generation'], generation)
                recipe = next((home / 'profile/package-recipes').glob('*.json'))
                self.assertEqual(recipe.stat().st_mode & 0o777, 0o600)
                self.assertNotIn('profile-only', recipe.read_text())
                lock = recipe.read_bytes()
                shutil.rmtree(home / 'cache'); shutil.rmtree(home / 'npm-cache')
                command = prepare(argv, home / 'profile', env, cache=home / 'cache')
                self.assertEqual(json.loads(subprocess.check_output(command, env=env, text=True)),
                                 {'version': '2.0.0', 'token': 'profile-only'})
                self.assertEqual(recipe.read_bytes(), lock)
                with self.assertRaises(ValueError):
                    prepare(['npx', '--package', 'fixture-ui-mcp@latest', 'missing-executable'],
                            home / 'profile', env, cache=home / 'cache')
                failure = next((home / 'cache').glob('*.failure.log'))
                self.assertEqual(failure.stat().st_mode & 0o777, 0o600)
                self.assertTrue(failure.read_bytes())
                self.assertFalse(list((home / 'cache').glob('install-*')))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_npm_and_uvx_install_options_keep_server_arguments_separate(self):
        self.assertEqual(npm_request(['npx', '--package', '@scope/server@1.0.0', '--', 'server', '--port', '123']),
                         (['@scope/server@1.0.0'], 'server', ['--port', '123']))
        self.assertEqual(python_request(['uvx', '--from', 'fixture==1.0', '--with', 'dependency>=1,<2', 'fixture', '--key', 'secret']),
                         (['fixture==1.0', 'dependency>=1,<2'], 'fixture', '/usr/bin/python3', ['--key', 'secret']))
        self.assertEqual(npm_request(['npx', 'github:example/server#0123456789abcdef']),
                         (['github:example/server#0123456789abcdef'], None, []))
        for argv in [['npx', '-p'], ['npx', '--unknown-option']]:
            with self.assertRaises(ValueError):
                npm_request(argv)

    def test_image_pinned_package_version_is_reported_before_profile_override(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'package.json'
            manifest.write_text(json.dumps({'dependencies': {
                '@scope/server': '1.2.3', 'plain-server': '4.5.6'}}))
            self.assertEqual(baked_package_versions(
                ['npx', '-y', '@scope/server@latest'], manifest), ['@scope/server@1.2.3'])
            self.assertEqual(baked_package_versions(
                ['npx', '--package', 'plain-server@latest', '--', 'server'], manifest),
                ['plain-server@4.5.6'])
            self.assertEqual(baked_package_versions(['uvx', 'plain-server'], manifest), [])

    def test_smoke_uses_initialize_then_tools_list_without_a_model(self):
        program = '''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if m.get("method")=="initialize":
  sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":"1"}}})+"\\n")
  sys.stdout.write(json.dumps({"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info","data":"prefetched"}})+"\\n")
  sys.stdout.flush()
 elif m.get("method")=="tools/list":
  print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"tools":[]}}),flush=True)
'''
        smoke_mcp([os.sys.executable, '-u', '-c', program], os.environ.copy(), timeout=3)

    def test_smoke_probes_resources_only_servers(self):
        program = '''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if m.get("method")=="initialize":
  print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{"resources":{}},"serverInfo":{"name":"fixture","version":"1"}}}),flush=True)
 elif m.get("method")=="resources/list":
  print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"resources":[]}}),flush=True)
'''
        smoke_mcp([os.sys.executable, '-u', '-c', program], os.environ.copy(), timeout=3)

    def test_corrupt_active_pointer_fails_closed_instead_of_using_image_package(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            argv = ['npx', '-y', '@upstash/context7-mcp@latest']
            identity = hashlib.sha256(json.dumps(['npx', (['@upstash/context7-mcp@latest'], None, [])[:-1]],
                                                  sort_keys=True).encode()).hexdigest()
            pointer_dir = home / 'package-pointers'
            pointer_dir.mkdir()
            (pointer_dir / (identity + '.json')).write_text('{bad json')
            with self.assertRaisesRegex(ValueError, 'refusing package fallback'):
                prepare(argv, home, os.environ.copy(), cache=home / 'cache')
