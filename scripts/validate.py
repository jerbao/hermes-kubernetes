"""Validate source and runtime files without depending on a cluster repository."""
import json
import re
from pathlib import Path

import yaml

from project_files import assert_paths, ignore_text, layout, tracked_files
from upstream_source import specification


def validate(root):
    value = layout(root)
    assert (root / ".gitignore").read_text() == ignore_text(value), "Regenerate .gitignore with scripts/project_files.py"
    assert_paths(root, tracked_files(root), value)
    lock = specification(root)
    assert lock['repository'] == 'https://github.com/NousResearch/hermes-agent'
    assert lock.get('release'), 'The distribution must pin an official stable release'
    initial = yaml.safe_load((root / 'config/hermes.yaml').read_text())
    assert initial['model']['provider'] == 'custom'
    assert initial['model']['key_env'] == 'CLI_PROXY_API_KEY'
    assert (root / 'runtime/start.py').is_file()
    assert (root / 'skills/hermes-self-management/SKILL.md').is_file()
    print('Validado: upstream fixado, runtime, configuração inicial e skill de autogestão')


if __name__ == '__main__':
    validate(Path(__file__).resolve().parents[1])
