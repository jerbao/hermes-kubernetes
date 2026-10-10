#!/bin/sh
set -eu
HERMES_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HERMES_IMAGE=${1:?Informe a imagem a testar}
HERMES_PLATFORM=${HERMES_PLATFORM:-linux/amd64}
HERMES_REVISION=$(git -C "$HERMES_PROJECT_DIR" rev-parse --verify HEAD 2>/dev/null || printf %s local-uncommitted)
HERMES_IMAGE_PLATFORM=$(docker image inspect --format '{{.Os}}/{{.Architecture}}{{if .Variant}}/{{.Variant}}{{end}}' "$HERMES_IMAGE")
HERMES_RUN_PLATFORM=$HERMES_PLATFORM
# Docker Engine 28 treats a config-only amd64/v2 image as incompatible with
# the unqualified linux/amd64 request; the fallback used for OCI indexes is
# unavailable after a local Docker build. Match the local config explicitly.
# Keep explicit variants as requested, and only infer v2 for this known image.
if [ "$HERMES_PLATFORM" = linux/amd64 ] && [ "$HERMES_IMAGE_PLATFORM" = linux/amd64/v2 ]; then
    HERMES_RUN_PLATFORM=linux/amd64/v2
fi
printf 'Image smoke platform: requested=%s image=%s runtime=%s\n' \
    "$HERMES_PLATFORM" "$HERMES_IMAGE_PLATFORM" "$HERMES_RUN_PLATFORM"
docker run --rm --pull=never --platform "$HERMES_RUN_PLATFORM" --network none --read-only --tmpfs /tmp \
    --entrypoint /opt/hermes/.venv/bin/hermes "$HERMES_IMAGE" --version
docker run --rm --pull=never --platform "$HERMES_RUN_PLATFORM" --network none --read-only --tmpfs /tmp \
    --entrypoint /usr/local/bin/rclone "$HERMES_IMAGE" version
HERMES_FIXTURE_DIR=$(mktemp -d)
trap 'rm -rf "$HERMES_FIXTURE_DIR"' EXIT
chmod 755 "$HERMES_FIXTURE_DIR"
printf 'fixture-only-no-real-key\n' > "$HERMES_FIXTURE_DIR/id_ed25519"
printf 'fixture-only-no-real-host\n' > "$HERMES_FIXTURE_DIR/known_hosts"
docker run --rm --pull=never --platform "$HERMES_RUN_PLATFORM" --network none --read-only \
    --env "HERMES_EXPECTED_REVISION=$HERMES_REVISION" \
    --tmpfs /tmp --tmpfs /opt/data:rw,uid=10000,gid=10000,mode=0700 \
    --mount "type=bind,src=$HERMES_FIXTURE_DIR,dst=/opt/hermes-ssh,readonly" \
    --mount "type=bind,src=$HERMES_PROJECT_DIR/tests/image_bootstrap.py,dst=/tmp/bootstrap-check.py,readonly" \
    --entrypoint /usr/bin/tini "$HERMES_IMAGE" -g -- \
    /opt/hermes/.venv/bin/python /tmp/bootstrap-check.py
