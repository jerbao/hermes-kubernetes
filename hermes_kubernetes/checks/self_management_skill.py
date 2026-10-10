"""Check the published skill and optionally its live files and documented plugin CLI."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys

REMOTE = r"""
import hashlib, io, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, '/opt/hermes-personal')
from hermes_kubernetes.operations.protocol import Client, OperationError
options = json.load(sys.stdin)
client = Client('/tmp/hermes-operations/control.sock', timeout=180)
info = client.request('info')
changes = []
for relative, data in options['files'].items():
    path = 'skills/hermes-self-management/' + relative
    wanted = hashlib.sha256(data.encode()).hexdigest()
    try:
        actual = client.request('stat', scope='state', path=path, sha256=True)['sha256']
    except OperationError as error:
        if str(error) != 'FileNotFoundError': raise
        actual = 'absent'
    if actual == wanted: continue
    if not options['publish']: raise ValueError('Live skill differs: '+relative)
    if actual != options['baseline'].get(relative, 'absent'):
        raise ValueError('Preserve concurrent skill edit: '+relative)
    changes.append((path, data, actual))
if changes:
    stdout, stderr = io.BytesIO(), io.BytesIO()
    setup = client.execute(['/opt/hermes/.venv/bin/python','-c',
        'import os; from pathlib import Path; p=Path(os.environ["HERMES_HOME"])/"skills/hermes-self-management/references"; '
        'assert not any(x.is_symlink() for x in [p,*p.parents]); p.mkdir(parents=True,exist_ok=True)'], stdout, stderr)
    if setup['returncode'] or setup['timed_out']: raise ValueError('Cannot prepare skill reference directory')
    for path, data, actual in changes:
        with tempfile.NamedTemporaryFile() as temporary:
            temporary.write(data.encode()); temporary.flush()
            client.upload(temporary.name, path, expected=actual)
for relative, data in options['files'].items():
    current = client.request('stat', scope='state', path='skills/hermes-self-management/'+relative, sha256=True)
    if current['sha256'] != hashlib.sha256(data.encode()).hexdigest():
        raise ValueError('Live skill differs after publication: '+relative)
stdout, stderr = io.BytesIO(), io.BytesIO()
result = client.execute(['hermes','--profile','default','plugins','install','--help'],stdout,stderr,timeout=120)
if result['returncode'] or result['timed_out']: raise ValueError('Documented native plugin CLI failed')
if not all(flag in stdout.getvalue().decode() for flag in ('--yes-deps','--enable','--no-enable','--ref')):
    raise ValueError('Native CLI does not support the documented flags')
print(json.dumps({'passed':True,'instance':info['instance'],'live_hashes_match':True,
    'native_plugin_cli':True,'updated_files':len(changes),'plugin_installations':0,'model_calls':0}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--socket", type=Path)
    parser.add_argument(
        "--kubeconfig",
        type=Path,
        help="Use the pod-local operations socket when the terminal is unavailable",
    )
    parser.add_argument("--namespace", default="hermes")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish with compare-and-swap against --base-revision",
    )
    parser.add_argument("--base-revision", help="Project revision before the skill edit")
    args = parser.parse_args()
    if args.publish and (not args.kubeconfig or not args.base_revision):
        parser.error("--publish requires --kubeconfig and --base-revision")
    root = args.root.resolve()
    source = root / "skills/hermes-self-management"
    projected = root / "customizations/default/skills/hermes-self-management"
    files = sorted(p.relative_to(source) for p in source.rglob("*") if p.is_file())
    if not files:
        raise ValueError("Missing self-management skill")
    expected = {}
    for relative in files:
        path = source / relative
        data = path.read_bytes()
        # Identical distribution files are omitted from the Git projection.
        # A present override is legitimate and is reconciled by skill_updates.
        for target in re.findall(r"\]\(([^)]+)\)", data.decode()):
            if "://" not in target and not (path.parent / target.split("#", 1)[0]).is_file():
                raise ValueError("Missing skill reference: " + target)
        installed = (
            (projected / relative).read_bytes() if (projected / relative).is_file() else data
        )
        expected[relative.as_posix()] = hashlib.sha256(installed).hexdigest()
    report = {"passed": True, "matching_files": len(files), "live": args.live}
    if args.kubeconfig:
        payload = {"publish": args.publish, "files": {}, "baseline": {}}
        for relative in files:
            name = relative.as_posix()
            payload["files"][name] = (source / relative).read_text()
            if args.publish:
                previous = subprocess.run(
                    [
                        "git",
                        "show",
                        args.base_revision
                        + ":customizations/default/skills/hermes-self-management/"
                        + name,
                    ],
                    cwd=root,
                    capture_output=True,
                    check=False,
                )
                if previous.returncode == 0:
                    payload["baseline"][name] = hashlib.sha256(previous.stdout).hexdigest()
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(args.kubeconfig),
                "-n",
                args.namespace,
                "exec",
                "-i",
                "deployment/hermes",
                "-c",
                "operations",
                "--",
                "/opt/hermes/.venv/bin/python",
                "-c",
                REMOTE,
            ],
            input=json.dumps(payload),
            text=True,
            timeout=360,
        )
        raise SystemExit(result.returncode)
    if args.live:
        from hermes_kubernetes.operations.protocol import Client

        client = Client(str(args.socket or root / ".local/runtime/control.sock"), timeout=180)
        info = client.request("info")
        for relative, checksum in expected.items():
            current = client.request(
                "stat",
                scope="state",
                path="skills/hermes-self-management/" + relative,
                sha256=True,
            )
            if current["sha256"] != checksum:
                raise ValueError("Live skill differs: " + relative)
        stdout, stderr = io.BytesIO(), io.BytesIO()
        result = client.execute(
            ["hermes", "--profile", "default", "plugins", "install", "--help"],
            stdout,
            stderr,
            timeout=120,
        )
        if result["returncode"] or result["timed_out"]:
            raise ValueError(
                "Documented native plugin CLI failed; exit=" + str(result["returncode"])
            )
        help_text = stdout.getvalue().decode()
        if not all(
            flag in help_text for flag in ("--yes-deps", "--enable", "--no-enable", "--ref")
        ):
            raise ValueError("Installed plugin CLI does not support the documented options")
        report.update(
            instance=info["instance"],
            live_hashes_match=True,
            native_plugin_cli=True,
            plugin_installations=0,
            model_calls=0,
        )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
