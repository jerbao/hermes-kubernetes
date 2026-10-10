"""Fill omitted remote Git objects without replacing restored refs or work files."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

from hermes_kubernetes.backup.state import digest
from hermes_kubernetes.backup.policy import normalize


def restore(home, policy, seed):
    home, seed = Path(home).resolve(), Path(seed).resolve()
    policy = normalize(policy)
    source_git = seed / ".git"
    if not source_git.is_dir() or source_git.is_symlink():
        raise ValueError("Provide a complete upstream clone as the seed")
    source = source_git / "objects"
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Seed object storage must be a real directory")
    seed_command = ["git", "--no-optional-locks", "-C", str(seed)]
    remote = (
        subprocess.check_output([*seed_command, "remote", "get-url", "origin"], text=True)
        .strip()
        .removesuffix(".git")
    )
    recovered = []
    for item in policy["exclude"]:
        if item["kind"] != "upstream-git" or not item["path"].endswith("/.git/objects/pack"):
            continue
        repo = home / PurePosixPath(item["path"]).parents[2]
        objects, pack = repo / ".git/objects", repo / ".git/objects/pack"
        if (
            remote != item["repository"].removesuffix(".git")
            or not objects.is_dir()
            or pack.exists()
            or any(p.is_symlink() for p in [objects, *objects.parents])
        ):
            raise ValueError("Expected a freshly restored Git directory with no pack cache")
        subprocess.run(
            [*seed_command, "cat-file", "-e", item["revision"] + "^{commit}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        command = ["git", "--no-optional-locks", "-C", str(repo)]
        environment = dict(os.environ, GIT_ALTERNATE_OBJECT_DIRECTORIES=str(source))
        # Check every restored ref/reflog against the seed before creating anything.
        subprocess.run(
            [*command, "fsck", "--full"],
            env=environment,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        count = 0
        with tempfile.TemporaryDirectory(prefix=".receiving-objects-", dir=objects) as temporary:
            staged = Path(temporary)
            for directory in source.iterdir():
                if (
                    directory.is_symlink()
                    or not directory.is_dir()
                    or directory.name != "pack"
                    and not re.fullmatch("[0-9a-f]{2}", directory.name)
                ):
                    continue
                for path in directory.iterdir():
                    if not path.is_file() or path.is_symlink():
                        raise ValueError("Unexpected seed object type")
                    target = objects / directory.name / path.name
                    if target.exists():
                        if target.is_symlink() or digest(target) != digest(path):
                            raise ValueError("Existing Git object differs from its seed")
                        continue
                    candidate = staged / directory.name / path.name
                    candidate.parent.mkdir(mode=0o700, exist_ok=True)
                    shutil.copyfile(path, candidate)
                    candidate.chmod(0o600)
                    count += 1
            for directory in staged.iterdir():
                target = objects / directory.name
                if directory.name == "pack":
                    directory.rename(target)
                else:
                    target.mkdir(mode=0o700, exist_ok=True)
                    for path in directory.iterdir():
                        os.link(path, target / path.name)
        subprocess.run(
            [*command, "fsck", "--full"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        recovered.append(
            {
                "path": str(repo.relative_to(home)),
                "objects_restored": count,
                "git_fsck": True,
            }
        )
    return {"repositories": recovered, "refs_index_work_files_unchanged": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--seed", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(restore(args.home, json.loads(args.policy.read_text()), args.seed)))
    except Exception as error:
        raise SystemExit("Private Git recovery failed: " + type(error).__name__)
