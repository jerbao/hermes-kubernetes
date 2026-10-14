"""Validate the immutable protocol shared by releases and private consumers."""

import json
import os
import re
from pathlib import Path
import urllib.request

COMPONENTS = ("hermes", "mcp", "mcp-codegraph")


def validate(value, repository=None):
    if value.get("schema") != 1:
        raise ValueError("Unsupported distribution manifest")
    repo = value.get("repository", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid distribution repository")
    if repository is not None and repo.lower() != repository.lower():
        raise ValueError("Release belongs to another distribution")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", value.get("version", "")):
        raise ValueError("Only stable version tags are accepted")
    if not re.fullmatch(r"[0-9a-f]{40}", value.get("revision", "")):
        raise ValueError("Release must pin an exact source commit")
    images = value.get("images", {})
    if set(images) != set(COMPONENTS):
        raise ValueError("Release requires all three runtime images")
    for component in COMPONENTS:
        suffix = "" if component == "hermes" else "-" + component
        expected = "ghcr.io/" + repo.lower() + suffix + "@sha256:"
        image = images[component]
        if (
            not isinstance(image, str)
            or not image.startswith(expected)
            or not re.fullmatch(r"[0-9a-f]{64}", image[len(expected) :])
        ):
            raise ValueError("Invalid immutable image for " + component)
    return value


def request(url):
    if not url.startswith("https://github.com/") and not url.startswith("https://api.github.com/"):
        raise ValueError("Only GitHub release sources are accepted")
    headers = {"User-Agent": "hermes-kubernetes-release"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    # Anonymous API calls share a 60/hour quota per IP, which hosted runners exhaust.
    # Asset downloads redirect to another host, so the token stays on the API.
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Release response is unexpectedly large")
    return data


def resolve(repository, version="latest"):
    # Validate before interpolating the repository or tag into a URL.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid distribution repository")
    if version != "latest" and not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ValueError("Invalid stable version")
    endpoint = "latest" if version == "latest" else "tags/" + version
    release = json.loads(request(f"https://api.github.com/repos/{repository}/releases/{endpoint}"))
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("Distribution release is not stable")
    asset = next((a for a in release.get("assets", []) if a["name"] == "distribution.json"), None)
    if asset is None:
        raise ValueError("Release has no distribution.json")
    value = validate(json.loads(request(asset["browser_download_url"])), repository)
    if value["version"] != release["tag_name"]:
        raise ValueError("Release tag and manifest disagree")
    tag = json.loads(
        request(f"https://api.github.com/repos/{repository}/git/ref/tags/{value['version']}")
    )["object"]
    for _ in range(4):
        if tag["type"] == "commit":
            break
        if tag["type"] != "tag":
            raise ValueError("Invalid release tag object")
        tag = json.loads(
            request(f"https://api.github.com/repos/{repository}/git/tags/{tag['sha']}")
        )["object"]
    if tag["type"] != "commit" or tag["sha"] != value["revision"]:
        raise ValueError("Release manifest does not match the tagged source")
    return value


def dump(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"
