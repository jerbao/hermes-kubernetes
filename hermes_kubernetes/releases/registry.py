"""Verify that public release images are anonymously downloadable by digest."""

import json
import urllib.parse
import urllib.request
from hermes_kubernetes.releases.manifest import validate


def public_images(manifest):
    validate(manifest)
    reviewed = []
    for component, image in manifest["images"].items():
        name, digest = image.removeprefix("ghcr.io/").split("@")
        query = urllib.parse.urlencode(
            {"service": "ghcr.io", "scope": "repository:" + name + ":pull"}
        )
        try:
            with urllib.request.urlopen("https://ghcr.io/token?" + query, timeout=30) as response:
                token = json.load(response)["token"]
            request = urllib.request.Request(
                "https://ghcr.io/v2/" + name + "/manifests/" + digest,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                actual = response.headers.get("Docker-Content-Digest")
                if actual != digest:
                    raise ValueError("Registry digest mismatch")
        except (OSError, KeyError, ValueError) as error:
            raise ValueError(
                "Image is not anonymously accessible: "
                + name
                + ". Set the GHCR package visibility to Public, then rerun only failed jobs."
            ) from None
        reviewed.append(component)
    return {"anonymous_pull": True, "components": sorted(reviewed)}
