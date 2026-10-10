"""Download the locked upstream and apply reviewed patches for a reproducible build."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from source_tree import fingerprint, source_files
from upstream_source import checkout, inputs_digest


def project_revision(root):
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'],
                            text=True, capture_output=True)
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', revision) else 'local-uncommitted'


def prepare(root, output):
    if output.exists():
        raise FileExistsError('Choose a new build directory; an existing checkout is never overwritten')
    with tempfile.TemporaryDirectory(prefix='hermes-upstream-') as temporary:
        source = Path(temporary) / 'hermes-agent'
        lock = checkout(root, source)
        # Keep executor routing aligned with the exact source tree used for this
        # build. Tiny upstream fixtures omit this catalog and skip the gate.
        if (source / 'optional-mcps').is_dir():
            from map_mcp_catalog import DESTINATION, generate
            expected = generate(source)
            try:
                mapped = json.loads(DESTINATION.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError('MCP executor map is missing or invalid; run scripts/map_mcp_catalog.py --write') from exc
            if mapped != expected:
                raise ValueError('MCP executor map drifted; review it and run scripts/map_mcp_catalog.py --write')
        commit_date = int(subprocess.check_output(
            ['git', '-C', str(source), 'show', '-s', '--format=%ct', lock['revision']], text=True).strip())
        digest = fingerprint(source)
        output.mkdir(parents=True)
        for relative in source_files(source):
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, destination)
    revision = project_revision(root)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'runtime'))
    from skill_updates import distribution_digest
    contract = json.loads((root/'project-files.json').read_text()) if (root/'project-files.json').exists() else {'skills':[]}
    provenance = {'repository': 'hermes-kubernetes', 'project_revision': revision,
                  'upstream_repository': lock['repository'], 'upstream_revision': lock['revision'],
                  'upstream_release': lock.get('release'),
                  'patches': lock['patches'], 'source_inputs_sha256': inputs_digest(root),
                  'source_tree_sha256': digest,
                  'distribution_skills_sha256': distribution_digest(root/'skills',contract['skills'])}
    (output / 'source-provenance.json').write_text(json.dumps(provenance) + '\n')
    stamp = {'schemaVersion': 2, 'commit': lock['revision'], 'distribution': 'docker',
             'source': 'docker', 'updateMechanism': 'external', 'pmRuntime': '/opt/hermes/pm-runtime',
             'commitDate': commit_date, 'dirty': bool(lock['patches'])}
    if lock.get('release'):
        version = lock['release']['tag'].removeprefix('v')
        stamp.update(baseVersion=version, displayVersion=version, distance=0)
    (output / 'install-stamp.json').write_text(json.dumps(stamp) + '\n')
    return provenance


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    parser.add_argument('--checkout', action='store_true', help='Ensure editable source matches the lock; preserve previous installations')
    parser.add_argument('--allow-local-edits', action='store_true', help='Allow intentional edits on the current locked source')
    parser.add_argument('--preserve-existing', action='store_true', help='Archive an idle existing checkout intact before switching')
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    output = (args.output or root / ('hermes-agent' if args.checkout else '.build/hermes-agent')).absolute()
    if args.checkout:
        from editable_source import SourceUpdateError, ensure
        try:
            report = ensure(root, output, allow_local_edits=args.allow_local_edits,
                            preserve_existing=args.preserve_existing)
        except SourceUpdateError as error:
            parser.exit(1, str(error) + '\n')
        print(json.dumps(report))
    else:
        provenance = prepare(root, output)
        if args.github_output:
            with args.github_output.open('a') as handle:
                handle.write(f"tree_sha256={provenance['source_tree_sha256']}\n")
        print(f"Pinned upstream prepared: {provenance['source_tree_sha256'][:12]}")
