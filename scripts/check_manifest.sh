#!/bin/sh
# Refuse to publish an image index: clusters pull these digests directly, and an index forces a
# platform match that rejects the amd64/v2 entry.
set -eu
media_type=$(docker buildx imagetools inspect --raw "$1" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("mediaType", ""))')
case "$media_type" in
    application/vnd.oci.image.manifest.v1+json|application/vnd.docker.distribution.manifest.v2+json) ;;
    *) echo "$1 is $media_type, not a single-platform image manifest" >&2; exit 1 ;;
esac
