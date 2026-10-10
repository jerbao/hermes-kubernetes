"""Inspect an official Hermes release and prepare a lockfile candidate.

This command only changes the local lockfile when explicitly asked. It never
builds, publishes, or promotes an image.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from upstream_source import specification

API = "https://api.github.com/repos/NousResearch/hermes-agent/releases?per_page=100"
TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ReleaseUnavailable(RuntimeError):
    pass


class CandidateRejected(RuntimeError):
    pass


def _run_git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, timeout=300).stdout.strip()


def fetch_releases(url: str = API) -> list[dict]:
    releases = []
    page = 1
    while True:
        separator = "&" if "?" in url else "?"
        request = urllib.request.Request(f"{url}{separator}page={page}", headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-kubernetes-upstream-release",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
                has_next = 'rel="next"' in response.headers.get("Link", "")
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise ReleaseUnavailable(f"Official release list unavailable: {error}") from error
        if not isinstance(payload, list):
            raise ReleaseUnavailable("Official release API returned an invalid payload")
        releases.extend(payload)
        if not payload or not has_next:
            return releases
        page += 1


def select_release(releases: list[dict]) -> dict:
    eligible = []
    for item in releases:
        if (not isinstance(item, dict) or item.get("draft") is not False
                or item.get("prerelease") is not False):
            continue
        tag = item.get("tag_name")
        match = TAG.fullmatch(tag) if isinstance(tag, str) else None
        published = item.get("published_at")
        published_at = None
        if isinstance(published, str) and published:
            try:
                parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    published_at = parsed.astimezone(timezone.utc)
            except ValueError:
                pass
        if match and published_at is not None:
            eligible.append((published_at, tuple(map(int, match.groups())), item))
    if not eligible:
        raise ReleaseUnavailable("No published, non-draft, non-prerelease official release was found")
    # Official release publication time is authoritative. Version ordering is
    # only a deterministic tie-breaker because Hermes tags include CalVer and
    # SemVer series whose numeric components are not directly comparable.
    return max(eligible, key=lambda row: (row[0], row[1]))[2]


def resolve_tag_commit(repository: str, tag: str) -> str:
    """Resolve lightweight or annotated tag refs to the commit they name."""
    result = subprocess.run(
        ["git", "ls-remote", "--tags", repository, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        check=True, capture_output=True, text=True, timeout=60,
    )
    refs = {line.split("\t", 1)[1]: line.split("\t", 1)[0]
            for line in result.stdout.splitlines() if "\t" in line}
    commit = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise CandidateRejected(f"Official tag {tag} did not resolve to a full commit SHA")
    return commit


def patch_paths(patch_file: Path) -> set[str]:
    paths = set()
    for line in patch_file.read_text(errors="replace").splitlines():
        if not line.startswith(("+++ ", "--- ")):
            continue
        raw = line[4:].split("\t", 1)[0]
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        paths.add(raw)
    return paths


def _changed_paths(repository: str, old: str, new: str) -> set[str]:
    with tempfile.TemporaryDirectory(prefix="hermes-release-diff-") as temporary:
        checkout = Path(temporary) / "source"
        checkout.mkdir()
        _run_git(checkout, "init", "-q")
        _run_git(checkout, "remote", "add", "origin", repository)
        _run_git(checkout, "fetch", "--depth", "1", "origin", old, new)
        output = _run_git(checkout, "diff", "--name-only", old, new)
        return {line for line in output.splitlines() if line}


def evaluate_candidate(root: Path, release: dict, commit: str,
                       *, changed_paths: Callable[[str, str, str], set[str]] = _changed_paths) -> dict:
    lock = specification(root)
    tag = release["tag_name"]
    if not TAG.fullmatch(tag):
        raise CandidateRejected(f"Unsupported release tag: {tag}")
    old_revision = lock["revision"]
    overlaps = sorted(set().union(*(patch_paths(root / item["path"])
                                    for item in lock["patches"])) &
                      changed_paths(lock["repository"], old_revision, commit))
    patches = []
    with tempfile.TemporaryDirectory(prefix="hermes-release-check-") as temporary:
        checkout = Path(temporary) / "source"
        checkout.mkdir()
        _run_git(checkout, "init", "-q")
        _run_git(checkout, "remote", "add", "origin", lock["repository"])
        _run_git(checkout, "fetch", "--depth", "1", "origin", commit)
        _run_git(checkout, "checkout", "--detach", "-q", "FETCH_HEAD")
        blocked = False
        for item in lock["patches"]:
            name = item["path"]
            if blocked:
                patches.append({"path": name, "status": "not-tested-after-prior-conflict"})
                continue
            patch = str((root / name).resolve())
            result = subprocess.run(["git", "-C", str(checkout), "apply", "--check", "--index", patch],
                                    capture_output=True, text=True, timeout=300)
            if result.returncode:
                blocked = True
                patches.append({"path": name, "status": "conflict",
                                "detail": (result.stderr or result.stdout).strip()[-2000:]})
            else:
                subprocess.run(["git", "-C", str(checkout), "apply", "--index", patch],
                               check=True, capture_output=True, text=True, timeout=300)
                patches.append({"path": name, "status": "applies"})
    return {
        "id": release.get("id"),
        "tag": tag,
        "commit": commit,
        "published_at": release.get("published_at"),
        "url": release.get("html_url") or
               f"https://github.com/NousResearch/hermes-agent/releases/tag/{tag}",
        "previous_revision": old_revision,
        "patches": patches,
        "upstream_patch_overlap": overlaps,
        "ready": all(row["status"] == "applies" for row in patches),
    }


def update_lock(root: Path, report: dict) -> None:
    if not report["ready"]:
        raise CandidateRejected("Cannot update lock: one or more patches do not apply sequentially")
    path = root / "upstream.lock.json"
    value = json.loads(path.read_text())
    release = {"tag": report["tag"], "commit": report["commit"]}
    if report.get("id") is not None:
        release["id"] = report["id"]
    if report.get("published_at"):
        release["published_at"] = report["published_at"]
    if report.get("url"):
        release["url"] = report["url"]
    value["revision"] = report["commit"]
    value["release"] = release
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def inspect_latest(root: Path, *, releases: list[dict] | None = None,
                   tag_resolver: Callable[[str, str], str] = resolve_tag_commit) -> dict:
    lock = specification(root)
    repository = lock["repository"].removesuffix(".git").rstrip("/").lower()
    if repository != "https://github.com/nousresearch/hermes-agent":
        raise CandidateRejected("Release polling is restricted to the official NousResearch/hermes-agent repository")
    release = select_release(fetch_releases() if releases is None else releases)
    pinned_release = lock.get("release")
    if pinned_release and TAG.fullmatch(pinned_release.get("tag", "")):
        if tuple(map(int, TAG.fullmatch(release["tag_name"]).groups())) < tuple(
                map(int, TAG.fullmatch(pinned_release["tag"]).groups())):
            raise CandidateRejected("Latest published release is older than the release already pinned")
    commit = tag_resolver(lock["repository"], release["tag_name"])
    if pinned_release and pinned_release.get("tag") == release["tag_name"]:
        if (pinned_release.get("commit") != commit or lock["revision"] != commit
                or (pinned_release.get("id") is not None and pinned_release["id"] != release.get("id"))):
            raise CandidateRejected("The published tag diverges from release metadata in upstream.lock.json")
    if commit == lock["revision"]:
        return {"id": release.get("id"), "tag": release["tag_name"], "commit": commit, "already_locked": True,
                "ready": True, "patches": [], "upstream_patch_overlap": []}
    return evaluate_candidate(root, release, commit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, help="Write the JSON report here")
    parser.add_argument("--update-lock", action="store_true",
                        help="Update revision and release metadata after all patches apply")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        report = inspect_latest(root)
        if args.update_lock and not report.get("already_locked"):
            try:
                update_lock(root, report)
                report["lock_updated"] = True
            except CandidateRejected as error:
                # Keep the release identity and per-patch/overlap evidence when
                # an invalid candidate is refused; CI uploads this report for review.
                report["lock_updated"] = False
                report["lock_update_error"] = str(error)
    except (CandidateRejected, ReleaseUnavailable, OSError, subprocess.SubprocessError, ValueError) as error:
        report = {"ready": False, "error": f"{type(error).__name__}: {error}"}
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if report.get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
