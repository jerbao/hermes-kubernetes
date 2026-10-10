#!/bin/sh
# Thin development entry point; private release consumers use hermes.py setup.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
setup_python="$project_root/.local/setup-venv/bin/python"
if [ ! -x "$setup_python" ]; then
    python3 -m venv "$project_root/.local/setup-venv"
    "$setup_python" -m pip install --disable-pip-version-check 'PyYAML==6.0.3'
fi
cd "$project_root"
exec "$setup_python" -m hermes_kubernetes.installation "$@"
