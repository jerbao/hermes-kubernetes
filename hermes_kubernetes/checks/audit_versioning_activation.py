"""Audit actual Git/native agreement, complete install recipes and independent profile envs."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import base64
import io
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
from hermes_kubernetes.terminal.versioning import SocketRuntime, projection
from hermes_kubernetes.terminal.guard import guard


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=root)
    p.add_argument("--expect-profiles", type=int)
    a = p.parse_args()
    directory = a.root.resolve()
    backend = SocketRuntime(directory, directory / ".local/runtime/control.sock")
    with guard(directory / "state/workspace-sync.lock", create=True, blocking=False):
        local = projection(directory)
        native = backend.capture()
        tracked = set(
            subprocess.check_output(
                ["git", "-C", str(directory), "ls-files", "-z", "customizations"],
                text=True,
            ).split("\0")
        ) - {""}
        different = sorted(
            name
            for name in set(local) | set(native["files"])
            if local.get(name, {}).get("sha256") != native["files"].get(name, {}).get("sha256")
        )
        missing = sorted(set(local) - tracked)
        installs = {}
        configured = set()
        for name, item in local.items():
            if name.endswith("/config.json") and json.loads(base64.b64decode(item["data"])).get(
                "mcp_servers"
            ):
                configured.add(name.split("/")[1])
            if name.endswith("/mcp-lock.json"):
                record = json.loads(base64.b64decode(item["data"]))
                installs[name.split("/")[1]] = {
                    "complete": record["complete"],
                    "recipes": sum(
                        "installation_recipe" in row for row in record["servers"].values()
                    ),
                    "servers": len(record["servers"]),
                }
        code = r"""
import json,os,pathlib
home=pathlib.Path(os.environ['HERMES_HOME']);roots={'default':home}
roots.update({p.name:p for p in (home/'profiles').iterdir() if p.is_dir() and not p.is_symlink() and (p/'config.yaml').is_file()})
identities=[];valid=True
for root in roots.values():
 path=root/'.env'
 valid=valid and path.is_file() and not path.is_symlink()
 if path.is_file():identities.append((path.stat().st_dev,path.stat().st_ino))
print(json.dumps({'profiles':sorted(roots),'independent_envs':valid and len(set(identities))==len(roots),'private_vault_present':(home/'state/versioning/secrets.json').is_file()}))
"""
        out, err = io.BytesIO(), io.BytesIO()
        result = backend.client.execute(
            ["/opt/hermes/.venv/bin/python", "-c", code], out, err, timeout=30
        )
        if result["returncode"]:
            raise RuntimeError("Native environment inventory failed")
        environment = json.loads(out.getvalue())
        status = subprocess.check_output(
            ["git", "-C", str(directory), "status", "--porcelain"], text=True
        ).strip()
        head = subprocess.check_output(
            ["git", "-C", str(directory), "rev-parse", "HEAD"], text=True
        ).strip()
        remote = subprocess.check_output(
            ["git", "-C", str(directory), "ls-remote", "origin", "refs/heads/main"],
            text=True,
        ).split()[0]
        timer = subprocess.run(
            ["systemctl", "--user", "is-active", "hermes-workspace-sync.timer"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        passed = (
            not different
            and not missing
            and not status
            and head == remote
            and timer == "active"
            and environment["independent_envs"]
            and environment["private_vault_present"]
            and configured <= set(installs)
            and all(row["complete"] for row in installs.values())
        )
        if a.expect_profiles is not None:
            passed = passed and len(environment["profiles"]) == a.expect_profiles
        report = {
            "passed": passed,
            "revision": head,
            "published": head == remote,
            "timer": timer,
            "clean": not status,
            "projected_files": len(local),
            "untracked_projection": missing,
            "native_differences": different,
            "environment": environment,
            "mcp_installations": installs,
        }
    print(json.dumps(report))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
