"""Manual acceptance for the unchanged Desktop MCP management API.

The fixture instance.json and SSH credentials point only to a test executor.
Run in an empty Hermes home; no channel or gateway is started.
"""
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import sys
sys.path.insert(0, '/tests')
from capabilities_gateway import NativeGateway

sys.path[:0] = ['/opt/hermes-personal', '/opt/hermes']
from start import atomic_write, ensure_home_alias
from dependencies import prepare_store
home = Path(os.environ.get('HERMES_HOME', '/opt/data'))
home.mkdir(mode=0o700, exist_ok=True)
ensure_home_alias(home)
os.environ['HERMES_RUNTIME_DIR'] = str(prepare_store(home))
keys = home / '.ssh/mcp'; keys.mkdir(mode=0o700, parents=True, exist_ok=True)
for name in ['id_ed25519', 'known_hosts']:
    atomic_write(keys / name, (Path('/fixtures/mcp-client') / name).read_text())


def call(method, **params):
    return rpc.call(method, **params)


def load_existing_servers():
    from hermes_cli.config import load_config_readonly
    return load_config_readonly().get('mcp_servers', {})


async def invoke(name, tool, arguments):
    from tools.mcp_tool_discovery import _connect_server
    from tools.mcp_tool_config import _load_mcp_config
    from tools.mcp_tool_common import mcp_field
    connection = await asyncio.wait_for(_connect_server(name, _load_mcp_config()[name]), 240)
    try:
        result = await asyncio.wait_for(connection.session.call_tool(tool, arguments), 60)
        assert not mcp_field(result, 'is_error', 'isError', False), result
        return result.model_dump_json()
    finally:
        await connection.shutdown()


rpc = NativeGateway(home)
report = {'passed': False, 'native_desktop_rpc': True, 'websocket': True, 'production_activated': False}
log = home / 'mcp-capabilities-functional.log'
with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as handle, \
        contextlib.redirect_stdout(handle), contextlib.redirect_stderr(handle):
    logging.basicConfig(stream=handle, level=logging.INFO)
    try:
        rpc.start()
        fixtures = [('acceptance-node', 'npx', ['-y', '@modelcontextprotocol/server-everything@latest',
                                                '--hermes-executor=api'], 'echo'),
                    ('acceptance-python', 'uvx', ['mcp-server-fetch', '--hermes-executor=api'], 'fetch')]
        for name, *_ in fixtures:
            if name in load_existing_servers():
                call('mcp.servers.remove', name=name)
        for name, command, args, expected in fixtures:
            definition = {'command': command, 'args': args, 'connect_timeout': 240, 'timeout': 120}
            assert call('mcp.servers.add', name=name, config=definition)['ok']
            assert call('mcp.servers.set_api_key', name=name, value='synthetic-profile-secret')['ok']
            tested = call('mcp.servers.test', name=name)
            assert tested['ok'], tested
            assert expected in {t['name'] for t in tested['tools']}
            disabled = call('tools.configure', action='disable', names=[name + ':*'])
            assert disabled['changed'] == [name + ':*'], disabled
            enabled = call('tools.configure', action='enable', names=[name + ':*'])
            assert enabled['changed'] == [name + ':*'], enabled
        assert 'example-validation' in asyncio.run(invoke('acceptance-node', 'echo', {'message': 'example-validation'}))
        fetched = asyncio.run(invoke('acceptance-python', 'fetch', {'url': 'https://example.com', 'raw': True}))
        logging.info('Synthetic fetch result: %s', fetched[:1000])
        assert 'Example Domain' in fetched
        from hermes_cli.config import load_config_readonly
        for name, command, args, _ in fixtures:
            original = load_config_readonly()['mcp_servers'][name]
            assert original['command'] == command and original['args'] == args
            assert call('mcp.servers.remove', name=name)['removed']
        assert not call('mcp.servers.list')['servers']
        report.update(passed=True, npm_new_package=True, python_new_package=True,
                      configure=True, disable=True, enable=True, remove=True,
                      original_definitions_preserved=True, tools_callable=True)
    except Exception as error:
        logging.exception('MCP capabilities acceptance failed')
        report['error_type'] = type(error).__name__
    finally:
        rpc.close()
print(json.dumps(report))
raise SystemExit(0 if report['passed'] else 1)
