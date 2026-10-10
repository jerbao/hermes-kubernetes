"""Bootstrap the Git project at ~/.hermes without replacing personal state."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def prepare(repo, enable_sync=False, home=None, check=False, bundle=None):
    if not re.fullmatch(r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", repo):
        raise ValueError("Use a GitHub SSH repository URL")
    workspace = Path(home).expanduser().absolute() if home else Path.home() / ".hermes"
    if workspace.is_symlink() or (workspace / ".git").is_symlink():
        raise ValueError("Repository paths must not be symlinks")
    if (workspace / ".git").exists():
        if git(workspace, "remote", "get-url", "origin") != repo:
            raise ValueError("Existing workspace belongs to another repository")
    else:
        # Staging stays outside HERMES_HOME and does not enter native SSH sync.
        with tempfile.TemporaryDirectory(prefix="hermes-project-") as temporary:
            staging = Path(temporary) / "project"
            subprocess.run(["git", "clone", "--branch", "main", str(bundle or repo), str(staging)],
                           capture_output=True, check=True)
            git(staging, "remote", "set-url", "origin", repo)
            command = [sys.executable, str(staging / "vm/adopt_home.py"),
                       "--source", str(staging), "--home", str(workspace)]
            if check:
                command.append("--check")
            subprocess.run(command, check=True)
    if check:
        return workspace
    for folder in (".local", ".local/dev-home", ".local/dev-runtime", ".local/diagnostics"):
        target = workspace / folder
        if target.is_symlink():
            raise ValueError("Private workspace directories must not be symlinks")
        target.mkdir(mode=0o700, exist_ok=True)
        target.chmod(0o700)
    if enable_sync:
        units = Path.home() / ".config/systemd/user"
        units.mkdir(parents=True, exist_ok=True)
        for filename in ("hermes-workspace-sync.service", "hermes-workspace-sync.timer"):
            shutil.copy2(workspace / "vm" / filename, units / filename)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "hermes-workspace-sync.timer"], check=True)
    return workspace


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--bundle", type=Path, help="Offline Git bundle; origin remains the GitHub repository")
    parser.add_argument("--check", action="store_true", help="Preflight only; do not change HERMES_HOME or services")
    parser.add_argument("--enable-sync", action="store_true")
    args = parser.parse_args()
    workspace = prepare(args.repo, args.enable_sync, args.home, args.check, args.bundle)
    print(json.dumps({"workspace": str(workspace), "mode": "check" if args.check else "ready"}))
