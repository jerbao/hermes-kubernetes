"""Repair missing profile SSH settings and check the actual Desktop file-list API."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
from pathlib import Path
import subprocess

REMOTE = r"""
import json, os, sys
from pathlib import Path
options = json.load(sys.stdin)
namespace = {}
exec(options['configuration_source'], namespace)
from hermes_cli.config import atomic_config_replace
from utils import load_yaml_file_readonly
root = Path(os.environ.get('HERMES_HOME', '/opt/data'))
instance = json.loads(Path('/etc/hermes-policy/instance.json').read_text())
homes = [('default', root)]
if (root/'profiles').is_dir():
    homes += [(p.name, p) for p in sorted((root/'profiles').iterdir())
              if not p.name.startswith('.') and p.is_dir() and not p.is_symlink()
              and (p/'config.yaml').is_file()]
def listing(profile):
    # Exercise the unchanged HTTP route and its real SSH adapter against durable
    # profiles. The production OAuth gate requires the user's browser session;
    # an isolated ASGI transport supplies only the local server session token.
    import asyncio, httpx
    from hermes_cli.web_server import app, _SESSION_TOKEN, _SESSION_HEADER_NAME
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            return await client.get('/api/fs/list', params={'path':'~/.hermes','profile':profile},
                                    headers={_SESSION_HEADER_NAME:_SESSION_TOKEN}, timeout=60)
    try:
        response = asyncio.run(request())
        value = response.json()
        return {'status':response.status_code, 'entries':len(value.get('entries',[])),
                'error':value.get('error')}
    except Exception as error:
        return {'status':500, 'error_type':type(error).__name__}
report = {'before': listing('default'), 'profiles': [], 'applied': options['apply']}
for profile, home in homes:
    config = load_yaml_file_readonly(home/'config.yaml')
    updated = namespace['ssh_workspace_configuration'](config, home, root, instance)
    changed = updated != config
    if options['apply'] and changed:
        atomic_config_replace(home/'config.yaml', updated)
    report['profiles'].append({'profile': profile, 'changed': changed})
if options['apply']:
    report['after'] = {profile: listing(profile) for profile, _ in homes}
    report['passed'] = all(v['status'] == 200 and not v.get('error') for v in report['after'].values())
print(json.dumps(report))
raise SystemExit(0 if not options['apply'] or report['passed'] else 1)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--namespace", default="hermes")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    payload = {
        "apply": args.apply,
        "configuration_source": (root / "hermes_kubernetes/runtime/configuration.py").read_text(),
    }
    command = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "-n",
        args.namespace,
        "exec",
        "-i",
        "deployment/hermes",
        "-c",
        "dashboard",
        "--",
        "/opt/hermes/.venv/bin/python",
        "-c",
        REMOTE,
    ]
    result = subprocess.run(command, input=json.dumps(payload), text=True, timeout=600)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
