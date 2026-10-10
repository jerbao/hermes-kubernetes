"""Exercise real SSH sessions, profile separation and process cleanup."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import asyncssh
from runner import Runner, child_configuration, listen, package_command, packaged_command
from packages import has_active_override, package_identity, prepare as prepare_package


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.host = asyncssh.generate_private_key('ssh-ed25519')
        self.client = asyncssh.generate_private_key('ssh-ed25519')
        self.host.write_private_key(self.root / 'host')
        self.client.write_private_key(self.root / 'client')
        (self.root / 'host').chmod(0o600)
        (self.root / 'client').chmod(0o600)
        (self.root / 'authorized').write_bytes(self.client.export_public_key())
        self.runner = Runner(self.root / 'data')
        self.server = await listen(self.runner, self.root / 'host', self.root / 'authorized',
                                   host='127.0.0.1', port=0)
        self.port = self.server.get_port()
        self.known = self.root / 'known_hosts'
        self.known.write_bytes(f'[127.0.0.1]:{self.port} '.encode() + self.host.export_public_key())
        self.connections = []

    async def asyncTearDown(self):
        for conn in self.connections:
            conn.close()
            await conn.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        await self.runner.close()
        self.temporary.cleanup()

    async def connect(self):
        conn = await asyncssh.connect('127.0.0.1', self.port, username='mcp',
            client_keys=[self.client], known_hosts=str(self.known), agent_path=None,
            encoding=None)
        self.connections.append(conn)
        return conn

    async def test_bytes_and_credentials_remain_private_between_concurrent_profiles(self):
        program = "import os,sys,json,tempfile; print(json.dumps({'home':os.environ['HOME'],'token':os.environ['TOKEN'],'cache':os.environ['NPM_CONFIG_CACHE'],'temporary':tempfile.gettempdir()}),flush=True); sys.stderr.buffer.write(b'diagnostic\\n'); sys.stderr.flush(); [sys.stdout.buffer.write(line) or None for line in sys.stdin.buffer]; sys.stdout.flush()"
        async def run(profile):
            conn = await self.connect()
            definition = {'profile': profile, 'command': sys.executable,
                          'args': ['-u', '-c', program], 'env': {'TOKEN': profile + '-secret', 'TMPDIR': '/host-only/scratch/path'}}
            rpc = b'{"jsonrpc":"2.0","id":1,"text":"a; $(b)"}\n' + bytes(range(256)) + b'\n'
            result = await conn.run('hermes-mcp', input=json.dumps(definition).encode() + b'\n' + rpc,
                                    encoding=None, check=True, timeout=10)
            metadata, payload = result.stdout.split(b'\n', 1)
            metadata = json.loads(metadata)
            self.assertEqual(payload, rpc)
            self.assertEqual(metadata['token'], profile + '-secret')
            self.assertEqual(Path(metadata['home']), self.root / 'data/profiles' / profile)
            self.assertNotIn(profile + '-secret', result.stderr.decode())
            self.assertEqual(result.stderr, b'diagnostic\n')
            return metadata
        a, b = await asyncio.gather(run('lima'), run('pesquisa'))
        self.assertNotEqual(a['cache'], b['cache'])
        self.assertNotEqual(a['temporary'], b['temporary'])
        self.assertEqual(Path(a['temporary']), Path('/tmp/mcp-cache/lima/tmp'))
        self.assertTrue(Path(a['temporary']).is_dir())

    async def test_wrong_key_and_wrong_host_key_are_rejected(self):
        with self.assertRaises(asyncssh.PermissionDenied):
            await asyncssh.connect('127.0.0.1', self.port, username='mcp',
                client_keys=[asyncssh.generate_private_key('ssh-ed25519')],
                known_hosts=str(self.known), agent_path=None)
        bad = self.root / 'bad_hosts'
        bad.write_bytes(f'[127.0.0.1]:{self.port} '.encode() +
                        asyncssh.generate_private_key('ssh-ed25519').export_public_key())
        with self.assertRaises(asyncssh.HostKeyNotVerifiable):
            await asyncssh.connect('127.0.0.1', self.port, username='mcp',
                client_keys=[self.client], known_hosts=str(bad), agent_path=None)

    async def test_no_shell_and_invalid_definitions_do_not_start_a_process(self):
        conn = await self.connect()
        result = await conn.run('bash', input=b'', encoding=None)
        self.assertEqual(result.exit_status, 126)
        for definition in ({'profile': '../other', 'command': sys.executable},
                           {'command': sys.executable, 'args': [False]},
                           {'command': sys.executable, 'cwd': str(self.root)}):
            result = await conn.run('hermes-mcp', input=json.dumps(definition).encode() + b'\n',
                                    encoding=None, timeout=10)
            self.assertEqual(result.exit_status, 1)
        self.assertFalse(self.runner.children)

    async def test_disconnect_terminates_only_the_owned_process_group(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        pidfile = self.root / 'owned-pids'
        program = "import os,sys,subprocess,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); open(sys.argv[1],'w').write(str(os.getpid())+' '+str(p.pid)); time.sleep(30)"
        conn = await self.connect()
        channel = await conn.create_process('hermes-mcp', encoding=None)
        channel.stdin.write(json.dumps({'command': sys.executable,
            'args': ['-u', '-c', program, str(pidfile)]}).encode() + b'\n')
        try:
            for _ in range(100):
                if pidfile.exists():
                    break
                await asyncio.sleep(.05)
            self.assertTrue(pidfile.exists())
            owned = list(map(int, pidfile.read_text().split()))
            conn.close()
            await conn.wait_closed()
            for _ in range(120):
                if not self.runner.children:
                    break
                await asyncio.sleep(.05)
            self.assertFalse(self.runner.children)
            for pid in owned:
                if sys.platform.startswith('linux'):
                    status = Path(f'/proc/{pid}/stat')
                    if status.exists():
                        self.assertEqual(status.read_text().rsplit(')', 1)[1].split()[0], 'Z')
                else:
                    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
                    self.assertTrue(result.returncode or result.stdout.strip().startswith('Z'), result.stdout)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)

    async def test_openssh_preserves_the_existing_stdio_transport(self):
        definition = {'command': sys.executable, 'args': ['-u', '-c',
                      'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())']}
        payload = json.dumps(definition).encode() + b'\n{"id":42}\n'
        process = await asyncio.create_subprocess_exec('ssh', '-T', '-F', '/dev/null',
            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes',
            '-o', f'UserKnownHostsFile={self.known}', '-i', str(self.root / 'client'),
            '-p', str(self.port), 'mcp@127.0.0.1', 'hermes-mcp',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(payload), 10)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(stdout, b'{"id":42}\n')

    async def test_openssh_checks_the_literal_host_alias_on_a_nonstandard_port(self):
        known = self.root / 'alias_hosts'
        definition = {'command': sys.executable, 'args': ['-u', '-c',
                      'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())']}
        payload = json.dumps(definition).encode() + b'\n{"id":43}\n'
        for entry, expected in ((f'[hermes-mcp]:{self.port}', 255), ('hermes-mcp', 0)):
            known.write_bytes(entry.encode() + b' ' + self.host.export_public_key())
            process = await asyncio.create_subprocess_exec('ssh', '-T', '-F', '/dev/null',
                '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'HostKeyAlias=hermes-mcp', '-o', f'UserKnownHostsFile={known}',
                '-i', str(self.root / 'client'), '-p', str(self.port), 'mcp@127.0.0.1', 'hermes-mcp',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(payload), 10)
            self.assertEqual(process.returncode, expected, stderr.decode())
            if expected == 0:
                self.assertEqual(stdout, b'{"id":43}\n')
            else:
                self.assertIn(b'Host key verification failed', stderr)

    async def test_management_json_lists_stdio_and_reports_native_http_as_skipped(self):
        conn = await self.connect()
        request = {'operation': 'manage', 'action': 'plan', 'profile': 'default', 'servers': [
            {'name': 'fixture-npm', 'definition': {'command': 'npx', 'args': ['-y', 'fixture-mcp@latest']}},
            {'name': 'provider-http', 'definition': {'url': 'https://example.invalid/mcp'}},
        ]}
        result = await conn.run('hermes-mcp', input=json.dumps(request).encode() + b'\n',
                                encoding=None, check=True, timeout=10)
        response = json.loads(result.stdout)
        self.assertTrue(response['ok'])
        self.assertEqual([row['status'] for row in response['results']], ['would-update', 'skipped-http'])
        self.assertEqual(response['results'][0]['package_policy'], 'floating')

        request = {'operation': 'manage', 'action': 'list', 'profile': 'default', 'servers': [
            {'name': 'codegraph', 'definition': {'command': 'codegraph', 'args': []}},
        ]}
        result = await conn.run('hermes-mcp', input=json.dumps(request).encode() + b'\n',
                                encoding=None, check=True, timeout=10)
        listed = json.loads(result.stdout)['results'][0]
        pinned = json.loads(Path(__file__).with_name('package.json').read_text())['dependencies'][
            '@colbymchenry/codegraph']
        self.assertFalse(listed['override_active'])
        self.assertEqual(listed['active_versions'], [f'@colbymchenry/codegraph@{pinned}'])

    async def test_ssh_management_updates_known_codegraph_smokes_rolls_back_and_rebuilds(self):
        package = '@colbymchenry/codegraph'
        tarballs = {}
        for version in ('1.0.0', '2.0.0', '3.0.0'):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w:gz') as archive:
                if version in {'1.0.0', '3.0.0'}:
                    source = '''#!/usr/bin/env node
const readline=require('readline');
 (async()=>{for await(const line of readline.createInterface({input:process.stdin})){
 const m=JSON.parse(line);
 if(m.method==='initialize') console.log(JSON.stringify({jsonrpc:'2.0',id:m.id,result:{protocolVersion:'2024-11-05',capabilities:{tools:{}},serverInfo:{name:'codegraph',version:'__VERSION__'}}}));
 if(m.method==='tools/list') console.log(JSON.stringify({jsonrpc:'2.0',id:m.id,result:{tools:[]}}));
}})();
'''
                    source = source.replace('__VERSION__', version)
                else:
                    source = '''#!/usr/bin/env node
const readline=require('readline');
 (async()=>{for await(const line of readline.createInterface({input:process.stdin})){
 const m=JSON.parse(line);
 if(m.method==='initialize') console.log(JSON.stringify({jsonrpc:'2.0',id:m.id,result:{protocolVersion:'2024-11-05',capabilities:{tools:{}},serverInfo:{name:'codegraph',version:'2.0.0'}}}));
 if(m.method==='tools/list') console.log(JSON.stringify({jsonrpc:'2.0',id:m.id,error:{code:-32601,message:'fixture rejected tools/list'}}));
}})();
'''
                files = {'package.json': json.dumps({'name': package, 'version': version,
                          'bin': {'codegraph': 'index.js'}}), 'index.js': source}
                for name, value in files.items():
                    info = tarfile.TarInfo('package/' + name)
                    data = value.encode(); info.size = len(data); info.mode = 0o755
                    archive.addfile(info, io.BytesIO(data))
            tarballs[version] = output.getvalue()
        current = ['1.0.0']

        class Registry(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_HEAD(self): self.do_GET()
            def do_GET(self):
                path = self.path.split('?', 1)[0]
                if path.startswith('/pkg-'):
                    version = path.removeprefix('/pkg-').removesuffix('.tgz')
                    data = tarballs[version]
                elif 'colbymchenry' in path:
                    base = f'http://127.0.0.1:{self.server.server_port}'
                    data = json.dumps({'name': package, 'dist-tags': {'latest': current[0]}, 'versions': {
                        version: {'name': package, 'version': version, 'bin': {'codegraph': 'index.js'},
                                  'dist': {'tarball': base + '/pkg-' + version + '.tgz',
                                           'shasum': hashlib.sha1(content).hexdigest()}}
                        for version, content in tarballs.items()}}).encode()
                else:
                    self.send_error(404); return
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers()
                if self.command != 'HEAD': self.wfile.write(data)

        registry = ThreadingHTTPServer(('127.0.0.1', 0), Registry)
        registry_thread = threading.Thread(target=registry.serve_forever, daemon=True)
        registry_thread.start()
        wheel_output = io.BytesIO()
        dist_info = 'fixture_ui_mcp-1.0.0.dist-info'
        wheel_files = {
            'fixture_ui_mcp.py': '''import json,sys
def main():
 for line in sys.stdin:
  m=json.loads(line)
  if m.get("method")=="initialize": print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"fixture-ui-mcp","version":"1.0.0"}}}),flush=True)
  elif m.get("method")=="tools/list": print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":{"tools":[]}}),flush=True)
''',
            f'{dist_info}/METADATA': 'Metadata-Version: 2.1\nName: fixture-ui-mcp\nVersion: 1.0.0\n',
            f'{dist_info}/WHEEL': 'Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
            f'{dist_info}/entry_points.txt': '[console_scripts]\nfixture-ui-mcp = fixture_ui_mcp:main\n',
        }
        with zipfile.ZipFile(wheel_output, 'w') as archive:
            for filename, content in wheel_files.items():
                archive.writestr(filename, content)
            archive.writestr(f'{dist_info}/RECORD', ''.join(f'{filename},,\n' for filename in wheel_files))
        wheel_data = wheel_output.getvalue()

        class PythonIndex(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_HEAD(self): self.do_GET()
            def do_GET(self):
                if self.path.startswith('/simple/'):
                    data = f'<a href="http://127.0.0.1:{self.server.server_port}/fixture_ui_mcp-1.0.0-py3-none-any.whl">wheel</a>'.encode()
                    content_type = 'text/html'
                elif self.path.endswith('.whl'):
                    data, content_type = wheel_data, 'application/octet-stream'
                else:
                    self.send_error(404); return
                self.send_response(200); self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Type', content_type); self.end_headers()
                if self.command != 'HEAD': self.wfile.write(data)

        python_index = ThreadingHTTPServer(('127.0.0.1', 0), PythonIndex)
        python_index_thread = threading.Thread(target=python_index.serve_forever, daemon=True)
        python_index_thread.start()
        profile = 'update-' + uuid.uuid4().hex[:12]
        profile_home = self.root / 'data/profiles' / profile
        cache = Path('/tmp/mcp-packages') / profile
        npm_cache = Path('/tmp/mcp-cache') / profile
        registry_env = {'NPM_CONFIG_REGISTRY': f'http://127.0.0.1:{registry.server_port}'}
        uvx_definition = {'command': 'uvx', 'args': ['fixture-ui-mcp'],
                          'env': {'UV_INDEX_URL': f'http://127.0.0.1:{python_index.server_port}/simple'}}
        definition = {'command': 'codegraph', 'args': ['--root', '/workspace'],
                      'env': registry_env}
        try:
            conn = await self.connect()
            current[0] = '1.0.0'
            request = {'operation': 'manage', 'action': 'apply', 'profile': profile,
                       'servers': [{'name': 'codegraph', 'definition': definition},
                                   {'name': 'custom-npm', 'definition': {
                                       'command': 'npx', 'args': ['--package', '@colbymchenry/codegraph@latest',
                                                                  '--', 'codegraph', '--root', '/workspace'],
                                       'env': registry_env}},
                                   {'name': 'custom-uvx', 'definition': uvx_definition}]}
            result = await conn.run('hermes-mcp', input=json.dumps(request).encode() + b'\n',
                                    encoding=None, check=False, timeout=60)
            response = json.loads(result.stdout)
            failure_logs = list(cache.glob('*.failure.log')) if cache.exists() else []
            self.assertEqual(result.exit_status, 0, (response, [path.read_text(errors='replace') for path in failure_logs]))
            self.assertEqual([row['status'] for row in response['results']], ['updated', 'updated', 'updated'])
            self.assertEqual([row['activation'] for row in response['results']],
                             ['next-connection'] * 3)
            self.assertIn('@colbymchenry/codegraph@1.0.0', response['results'][0]['versions'])
            self.assertIn('fixture-ui-mcp==1.0.0', response['results'][2]['versions'])
            codegraph_identity = package_identity(
                ['npx', '-y', '@colbymchenry/codegraph@latest'])[2]
            pointer = profile_home / 'package-pointers' / (codegraph_identity + '.json')
            active_generation = json.loads(pointer.read_text())['generation']

            current[0] = '2.0.0'
            failed = await conn.run('hermes-mcp', input=json.dumps(request).encode() + b'\n',
                                    encoding=None, check=False, timeout=60)
            failed_response = json.loads(failed.stdout)
            self.assertEqual([row['status'] for row in failed_response['results']], ['failed', 'failed', 'updated'])
            self.assertEqual(json.loads(pointer.read_text())['generation'], active_generation)

            shutil.rmtree(cache, ignore_errors=True)
            shutil.rmtree(npm_cache, ignore_errors=True)
            rpc = (json.dumps({'jsonrpc': '2.0', 'id': 31, 'method': 'initialize', 'params': {
                'protocolVersion': '2024-11-05', 'capabilities': {},
                'clientInfo': {'name': 'fixture-test', 'version': '1'}}}) + '\n' +
                json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n' +
                json.dumps({'jsonrpc': '2.0', 'id': 32, 'method': 'tools/list', 'params': {}}) + '\n')
            runtime_definition = {**definition, 'profile': profile}
            recovered = await conn.run('hermes-mcp', input=json.dumps(runtime_definition).encode() + b'\n' + rpc.encode(),
                                       encoding=None, check=True, timeout=60)
            messages = [json.loads(line) for line in recovered.stdout.splitlines() if line.startswith(b'{')]
            self.assertEqual([message['id'] for message in messages if 'id' in message], [31, 32])
            self.assertEqual(messages[0]['result']['serverInfo']['version'], '1.0.0')

            current[0] = '3.0.0'
            single = {**request, 'servers': [{'name': 'codegraph', 'definition': definition}]}
            successful = await conn.run('hermes-mcp', input=json.dumps(single).encode() + b'\n',
                                        encoding=None, check=True, timeout=60)
            successful_row = json.loads(successful.stdout)['results'][0]
            self.assertEqual(successful_row['status'], 'updated')
            self.assertEqual(successful_row['activation'], 'next-connection')
            self.assertIn('@colbymchenry/codegraph@3.0.0', successful_row['versions'])
            self.assertNotEqual(json.loads(pointer.read_text())['generation'], active_generation)

            shutil.rmtree(cache, ignore_errors=True)
            activated = await conn.run('hermes-mcp', input=json.dumps(runtime_definition).encode() + b'\n' + rpc.encode(),
                                       encoding=None, check=True, timeout=60)
            activated_messages = [json.loads(line) for line in activated.stdout.splitlines()
                                  if line.startswith(b'{')]
            self.assertEqual(activated_messages[0]['result']['serverInfo']['version'], '3.0.0')

            uvx_rpc = (json.dumps({'jsonrpc': '2.0', 'id': 41, 'method': 'initialize', 'params': {
                'protocolVersion': '2024-11-05', 'capabilities': {},
                'clientInfo': {'name': 'fixture-test', 'version': '1'}}}) + '\n' +
                json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n' +
                json.dumps({'jsonrpc': '2.0', 'id': 42, 'method': 'tools/list', 'params': {}}) + '\n')
            recovered_uvx = await conn.run('hermes-mcp', input=json.dumps({**uvx_definition, 'profile': profile}).encode() + b'\n' + uvx_rpc.encode(),
                                           encoding=None, check=True, timeout=60)
            uvx_messages = [json.loads(line) for line in recovered_uvx.stdout.splitlines() if line.startswith(b'{')]
            self.assertEqual(uvx_messages[0]['result']['serverInfo']['version'], '1.0.0')
        finally:
            registry.shutdown(); registry.server_close(); registry_thread.join(timeout=5)
            python_index.shutdown(); python_index.server_close(); python_index_thread.join(timeout=5)
            shutil.rmtree(cache, ignore_errors=True)
            shutil.rmtree(npm_cache, ignore_errors=True)


class WorkspaceTests(unittest.TestCase):
    def test_only_canonical_image_commands_become_package_override_identities(self):
        self.assertEqual(package_command(['codegraph', '--root', '/workspace/repo']),
                         ['npx', '-y', '@colbymchenry/codegraph@latest', '--root', '/workspace/repo'])
        self.assertEqual(package_command(['/opt/mcp/node_modules/.bin/codegraph', '--root', 'repo']),
                         ['npx', '-y', '@colbymchenry/codegraph@latest', '--root', 'repo'])
        self.assertEqual(package_command(['/custom/bin/codegraph']), ['/custom/bin/codegraph'])

    def test_latest_and_unversioned_commands_use_the_image_package(self):
        for package in ('@upstash/context7-mcp', '@upstash/context7-mcp@latest',
                        '@upstash/context7-mcp@4.2.0'):
            command = packaged_command(['npx', '-y', package, '--api-key', 'fixture-private'])
            self.assertEqual(command, ['/opt/mcp/node_modules/.bin/context7-mcp', '--api-key', 'fixture-private'])
        with self.assertRaises(ValueError):
            packaged_command(['npx', '-y', 'not-installed@latest'])
        with self.assertRaises(ValueError):
            packaged_command(['npx', '-y', '@upstash/context7-mcp@0.1.0'])

    def test_active_profile_generation_overrides_an_image_known_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private, cache = root / 'profile', root / 'cache'
            argv = ['npx', '-y', '@upstash/context7-mcp@latest']
            identity = package_identity(argv)[2]
            generation = 'a' * 32
            record_dir = private / 'package-generations' / identity
            record_dir.mkdir(parents=True)
            record = {'identity': identity, 'generation': generation, 'binary': 'bin/context7-mcp'}
            (record_dir / (generation + '.json')).write_text(json.dumps(record))
            pointer_dir = private / 'package-pointers'
            pointer_dir.mkdir()
            (pointer_dir / (identity + '.json')).write_text(json.dumps({'identity': identity, 'generation': generation}))
            binary = cache / (identity + '-' + generation) / 'bin/context7-mcp'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o700)
            self.assertTrue(has_active_override(argv, private))
            overridden = prepare_package(argv, private, os.environ.copy(), cache=cache)
            self.assertEqual(overridden[0], str(binary))
            self.assertNotEqual(overridden, packaged_command(argv))

    def test_only_explicit_projects_can_be_used_as_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'projects/one'
            project.mkdir(parents=True)
            definition = {'command': 'codegraph', 'cwd': str(project)}
            _, cwd, _ = child_configuration(definition, root / 'private', root / 'projects')
            self.assertEqual(cwd, project.resolve())
            with self.assertRaises(ValueError):
                child_configuration({**definition, 'cwd': str(root)}, root / 'private', root / 'projects')
            with self.assertRaises(ValueError):
                child_configuration(definition, root / 'private')


if __name__ == '__main__':
    unittest.main()
