#!/bin/sh
set -eu
HERMES_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HERMES_IMAGE=${1:?Specify the final image}
HERMES_BASE_IMAGE=${HERMES_BASE_IMAGE:-hermes-personal-base:local}
mkdir -p "$HERMES_PROJECT_DIR/.build"
HERMES_SNAPSHOT=$(mktemp -d "$HERMES_PROJECT_DIR/.build/build-XXXXXX")
trap 'rm -rf "$HERMES_SNAPSHOT"' EXIT
if [ -z "${HERMES_BUILD_SOURCE:-}" ]; then
    HERMES_BUILD_SOURCE=$HERMES_SNAPSHOT/hermes-agent
fi
HERMES_PLATFORM=${HERMES_PLATFORM:-linux/amd64}
if [ ! -f "$HERMES_BUILD_SOURCE/source-provenance.json" ]; then
    python3 "$HERMES_PROJECT_DIR/hermes_kubernetes/source/prepare.py" --output "$HERMES_BUILD_SOURCE"
fi
HERMES_TREE=$(python3 - "$HERMES_PROJECT_DIR" "$HERMES_BUILD_SOURCE" <<'CHECK'
import json, sys
from pathlib import Path
root, source = map(Path, sys.argv[1:])
sys.path.insert(0, str(root))
from hermes_kubernetes.source.tree import fingerprint
from hermes_kubernetes.source.prepare import project_revision
from hermes_kubernetes.source.upstream import inputs_digest
provenance = json.loads((source / 'source-provenance.json').read_text())
if (provenance['source_inputs_sha256'] != inputs_digest(root)
        or provenance['source_tree_sha256'] != fingerprint(source)
        or provenance['project_revision'] != project_revision(root)):
    sys.exit('Prepared source is stale; set HERMES_BUILD_SOURCE to a new directory')
print(provenance['source_tree_sha256'])
CHECK
)
HERMES_DAEMON_BUILDER=$(docker context show)
if [ "${HERMES_SKIP_BASE_BUILD:-0}" != 1 ]; then
    docker build --builder "$HERMES_DAEMON_BUILDER" --platform "$HERMES_PLATFORM" --label "io.hermes-kubernetes.source-tree=$HERMES_TREE" \
        --tag "$HERMES_BASE_IMAGE" "$HERMES_BUILD_SOURCE"
else
    if ! docker image inspect "$HERMES_BASE_IMAGE" >/dev/null 2>&1; then
        docker pull --platform "$HERMES_PLATFORM" "$HERMES_BASE_IMAGE"
    fi
    HERMES_BASE_TREE=$(docker image inspect --format '{{index .Config.Labels "io.hermes-kubernetes.source-tree"}}' \
        "$HERMES_BASE_IMAGE")
    [ "$HERMES_TREE" = "$HERMES_BASE_TREE" ] || { echo 'Base image does not match the prepared upstream source and patches' >&2; exit 1; }
fi
python3 "$HERMES_PROJECT_DIR/hermes_kubernetes/project/image_context.py" --output "$HERMES_SNAPSHOT/image"
cp "$HERMES_BUILD_SOURCE/source-provenance.json" "$HERMES_BUILD_SOURCE/install-stamp.json" "$HERMES_SNAPSHOT/image/"
HERMES_BASE_PLATFORM=$(docker image inspect --format '{{.Os}}/{{.Architecture}}{{if .Variant}}/{{.Variant}}{{end}}' \
    "$HERMES_BASE_IMAGE")
HERMES_WRAPPER_PLATFORM=$HERMES_PLATFORM
if [ "$HERMES_PLATFORM" = linux/amd64 ] && [ "$HERMES_BASE_PLATFORM" = linux/amd64/v2 ]; then
    HERMES_WRAPPER_PLATFORM=linux/amd64/v2
fi
printf 'Image build platform: requested=%s base=%s wrapper=%s\n' \
    "$HERMES_PLATFORM" "$HERMES_BASE_PLATFORM" "$HERMES_WRAPPER_PLATFORM"
docker build --builder "$HERMES_DAEMON_BUILDER" --platform "$HERMES_WRAPPER_PLATFORM" \
    --build-arg "HERMES_BASE_IMAGE=$HERMES_BASE_IMAGE" --tag "$HERMES_IMAGE" "$HERMES_SNAPSHOT/image"
sh "$HERMES_PROJECT_DIR/scripts/test_image.sh" "$HERMES_IMAGE"
