"""Validate source and runtime files without depending on a cluster repository."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import json
import re
from pathlib import Path

import yaml

from hermes_kubernetes.project.files import (
    assert_paths,
    ignore_text,
    layout,
    tracked_files,
)
from hermes_kubernetes.source.upstream import specification


def validate(root):
    value = layout(root)
    assert (root / ".gitignore").read_text() == ignore_text(value), (
        "Regenerate .gitignore with hermes_kubernetes/project/files.py"
    )
    assert_paths(root, tracked_files(root), value)
    lock = specification(root)
    assert lock["repository"] == "https://github.com/NousResearch/hermes-agent"
    assert lock.get("release"), "The distribution must pin an official stable release"
    initial = yaml.safe_load((root / "config/hermes.yaml").read_text())
    assert initial["model"]["provider"] == "custom"
    assert initial["model"]["key_env"] == "CLI_PROXY_API_KEY"
    assert (root / "hermes_kubernetes/runtime/bootstrap.py").is_file()
    assert (root / "skills/hermes-self-management/SKILL.md").is_file()
    print("Validated: pinned upstream, runtime, initial configuration and self-management skill")


if __name__ == "__main__":
    validate(Path(__file__).resolve().parents[2])
