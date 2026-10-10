"""Compare the editable Hermes source with the running image's provenance."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import json
import argparse
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]

from hermes_kubernetes.source.prepare import project_revision
from hermes_kubernetes.source.editable import running_processes, status
from hermes_kubernetes.source.upstream import git


def published_source(root, report):
    try:
        published = json.loads(git(root, "show", "origin/main:upstream.lock.json"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    local = json.loads((root / "upstream.lock.json").read_text())
    report.update(
        published_revision=published["revision"],
        published_release=published.get("release", {}).get("tag"),
        workspace_tracks_published_source=all(
            local.get(key) == published.get(key) for key in ["revision", "patches"]
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=root / "hermes-agent")
    args = parser.parse_args()
    report = status(root, args.source)
    report["processes_using_source"] = (
        running_processes(args.source) if args.source.exists() else []
    )
    published_source(root, report)
    diagnostics = root / ".local/diagnostics/provenance.json"
    if not diagnostics.exists():
        report["diagnostics"] = "not-yet-synchronized"
    else:
        running = json.loads(diagnostics.read_text())
        report.update(
            running_revision=running["project_revision"],
            workspace_revision=project_revision(root),
            running_upstream_revision=running.get("upstream_revision"),
            hermes_source_matches=report.get("actual_tree_sha256") == running["source_tree_sha256"],
        )
    print(json.dumps(report))
    raise SystemExit(
        0
        if report["state"] == "current" and report.get("workspace_tracks_published_source", True)
        else 1
    )
