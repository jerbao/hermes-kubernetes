#!/usr/bin/env bash
set -euo pipefail
HERMES_PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export HERMES_HOME="$HERMES_PROJECT_ROOT/.local/dev-home"
export HERMES_RUNTIME_DIR="$HERMES_PROJECT_ROOT/.local/dev-runtime"
mkdir -p "$HERMES_HOME" "$HERMES_RUNTIME_DIR"
chmod 700 "$HERMES_HOME" "$HERMES_RUNTIME_DIR"
HERMES_SOURCE_DIR="${HERMES_SOURCE_DIR:-$HERMES_PROJECT_ROOT/hermes-agent}"
python3 "$HERMES_PROJECT_ROOT/scripts/prepare_source.py" --checkout \
    --output "$HERMES_SOURCE_DIR" --allow-local-edits
cd "$HERMES_SOURCE_DIR"
source ./activate --
hermes "$@"
