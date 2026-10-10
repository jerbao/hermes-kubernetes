"""Adopt a populated HERMES_HOME without replacing existing files or repositories."""
import argparse
import filecmp
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from project_files import assert_paths, ignore_text, layout, tracked_files


def plan(source, home):
    if home.is_symlink() or (home.exists() and not home.is_dir()):
        raise ValueError("HERMES_HOME must be a real directory")
    if (home / ".git").exists():
        raise ValueError("HERMES_HOME already contains a Git repository")
    value = layout(source)
    files = tracked_files(source)
    assert_paths(source, files, value)
    assert_paths(home, files, value)
    if (source / ".gitignore").read_text() != ignore_text(value):
        raise ValueError("Git ignore rules do not match the project layout")
    missing, conflicts = [], []
    for name in files:
        original, destination = source / name, home / name
        if not original.is_file():
            raise ValueError("Incomplete source checkout")
        # Check directories too: opening a missing leaf must not cross a file.
        for parent in destination.parents:
            if parent == home.parent:
                break
            if parent.exists() and not parent.is_dir():
                conflicts.append(name)
                break
        else:
            raise ValueError("Invalid project path")
        if destination.exists():
            if not destination.is_file() or not filecmp.cmp(original, destination, shallow=False):
                conflicts.append(name)
        else:
            missing.append(name)
    return {"tracked_files": len(files), "new_files": len(missing),
            "conflicts": sorted(set(conflicts)), "missing": missing}


def adopt(source, home, check=False):
    report = plan(source, home)
    if report["conflicts"]:
        raise ValueError(f"Adoption refused: {len(report['conflicts'])} existing project files differ")
    if check:
        return report
    created_files, created_directories = [], []
    created_git = False
    try:
        for name in report["missing"]:
            original, destination = source / name, home / name
            missing_parents = []
            parent = destination.parent
            while not parent.exists():
                missing_parents.append(parent)
                parent = parent.parent
            for parent in reversed(missing_parents):
                parent.mkdir(mode=0o700)
                created_directories.append(parent)
            # Exclusive creation preserves a file created after the preflight.
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created_files.append(destination)
            with os.fdopen(descriptor, "wb") as output, original.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            destination.chmod(original.stat().st_mode & 0o777)
        final_report = plan(source, home)
        if final_report["conflicts"] or final_report["missing"]:
            raise RuntimeError("The home changed during adoption; no Git repository was installed")
        # The clone's index keeps the vendored source as ordinary parent files.
        # An existing hermes-agent/.git is intentionally left untouched.
        (home / ".git").mkdir(mode=0o700)
        created_git = True
        shutil.copytree(source / ".git", home / ".git", dirs_exist_ok=True)
    except BaseException:
        if created_git:
            shutil.rmtree(home / ".git")
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        for path in reversed(created_directories):
            path.rmdir()
        raise
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    report = adopt(args.source.resolve(), args.home.expanduser(), args.check)
    print(json.dumps({key: report[key] for key in ("tracked_files", "new_files")}))
