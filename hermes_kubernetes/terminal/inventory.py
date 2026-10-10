"""Read-only inventory for adopting a home; the report itself is private."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time


def command(argv, timeout=15, env=None):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return {"returncode": result.returncode, "stdout": result.stdout}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": type(error).__name__}


def tool_inventory(home):
    tools = {}
    for name in [
        "python3",
        "python3.11",
        "git",
        "node",
        "npm",
        "uv",
        "google-chrome",
        "docker",
        "claude",
    ]:
        executable = shutil.which(name)
        tools[name] = {
            "path": executable,
            "version": command([executable, "--version"]) if executable else None,
        }
    # A noninteractive SSH PATH does not necessarily contain user installations.
    user_tools = {}
    user_environment = os.environ.copy()
    user_environment["PATH"] = os.pathsep.join(
        [
            str(home / ".local/bin"),
            str(home / ".hermes/node/bin"),
            user_environment.get("PATH", "/usr/bin:/bin"),
        ]
    )
    for name, relative in {
        "hermes_python": ".hermes/hermes-agent/venv/bin/python",
        "node": ".local/bin/node",
        "npm": ".local/bin/npm",
        "uv": ".local/bin/uv",
        "claude": ".local/bin/claude",
    }.items():
        path = home / relative
        user_tools[name] = {
            "path": str(path),
            "available": path.is_file(),
            "version": command([str(path), "--version"], env=user_environment)
            if path.is_file()
            else None,
        }
    return {"process_path": tools, "user_installations": user_tools}


def inventory(home):
    if home.is_symlink() or not home.is_dir():
        raise ValueError("Select a real home directory")
    home = home.resolve()
    started = time.monotonic()
    device = home.stat().st_dev
    groups, owners = {}, Counter()
    links, errors, boundaries, repositories, special = [], [], [], [], []
    pending = [home]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = path.relative_to(home)
                    group = groups.setdefault(
                        relative.parts[0],
                        {
                            "files": 0,
                            "directories": 0,
                            "symlinks": 0,
                            "bytes": 0,
                            "allocated_bytes": 0,
                        },
                    )
                    try:
                        info = entry.stat(follow_symlinks=False)
                        owners[f"{info.st_uid}:{info.st_gid}"] += 1
                        if stat.S_ISLNK(info.st_mode):
                            group["symlinks"] += 1
                            target = os.readlink(path)
                            try:
                                resolved = path.resolve()
                                external = not resolved.is_relative_to(home)
                                exists = resolved.exists()
                                resolution_error = None
                            except (OSError, RuntimeError) as error:
                                resolved, external, exists = None, None, None
                                resolution_error = type(error).__name__
                            links.append(
                                {
                                    "path": str(relative),
                                    "target": target,
                                    "resolved": str(resolved) if resolved else None,
                                    "external": external,
                                    "exists": exists,
                                    "resolution_error": resolution_error,
                                }
                            )
                        elif stat.S_ISDIR(info.st_mode):
                            group["directories"] += 1
                            if info.st_dev != device:
                                boundaries.append(str(relative))
                            else:
                                pending.append(path)
                            if path.name == ".git":
                                repositories.append(str(relative.parent))
                        elif stat.S_ISREG(info.st_mode):
                            group["files"] += 1
                            group["bytes"] += info.st_size
                            group["allocated_bytes"] += info.st_blocks * 512
                            if path.name == ".git":
                                repositories.append(str(relative.parent))
                        else:
                            special.append(
                                {
                                    "path": str(relative),
                                    "type": stat.S_IFMT(info.st_mode),
                                }
                            )
                    except OSError as error:
                        errors.append({"path": str(relative), "error": type(error).__name__})
        except OSError as error:
            errors.append(
                {
                    "path": str(directory.relative_to(home)),
                    "error": type(error).__name__,
                }
            )
    tools = tool_inventory(home)
    packages = command(
        [
            "dpkg-query",
            "--show",
            "--showformat=${binary:Package}\t${Version}\t${db:Status-Status}\n",
        ]
    )
    manual_packages = command(["apt-mark", "showmanual"])
    mounts = command(["findmnt", "--json", "--output", "TARGET,SOURCE,FSTYPE"])
    report = {
        "schema": 1,
        "home": str(home),
        "uid": home.stat().st_uid,
        "gid": home.stat().st_gid,
        "groups": groups,
        "owners": dict(owners),
        "links": links,
        "errors": errors,
        "mount_boundaries": boundaries,
        "git_repositories": sorted(repositories),
        "special_files": special,
        "tools": tools,
        "packages": packages,
        "manual_packages": manual_packages,
        "mounts": mounts,
        "seconds": round(time.monotonic() - started, 2),
        "complete": not errors and not boundaries,
    }
    report["totals"] = {
        key: sum(g[key] for g in groups.values())
        for key in ("files", "directories", "symlinks", "bytes", "allocated_bytes")
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = inventory(args.home.expanduser())
    # Do not expose filenames, symlink targets, package lists or tool output.
    with os.fdopen(
        os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as output:
        json.dump(report, output, indent=2)
    print(
        json.dumps(
            {
                "complete": report["complete"],
                "totals": report["totals"],
                "external_links": sum(link["external"] is True for link in report["links"]),
                "dangling_links": sum(link["exists"] is False for link in report["links"]),
                "errors": len(report["errors"]),
                "mount_boundaries": len(report["mount_boundaries"]),
                "git_repositories": len(report["git_repositories"]),
                "seconds": report["seconds"],
            }
        )
    )
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
