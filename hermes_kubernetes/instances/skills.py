"""Review managed skill changes against a candidate shared release in isolation."""

import base64
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def reconcile_projection(root, old_bundle, new_bundle):
    from hermes_kubernetes.terminal.versioning import projection
    from hermes_kubernetes.source.skills import reconcile

    files = projection(root)
    if not files:
        return {"ready": True, "profiles": 0, "conflicts": []}, {}
    with tempfile.TemporaryDirectory(prefix="hermes-release-review-") as temporary:
        stage = Path(temporary) / "review"
        subprocess.run(["git", "clone", "-q", "--local", str(old_bundle), str(stage)], check=True)
        for name, item in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(item["data"]))
            path.chmod(item["mode"])

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(stage), *args], stderr=subprocess.PIPE, text=True
            ).strip()

        git("config", "user.name", "Hermes release review")
        git("config", "user.email", "review@example.invalid")
        git("add", "customizations")
        git("commit", "--allow-empty", "-qm", "Private candidate baseline")
        base = git("rev-parse", "HEAD")
        previous = set(git("ls-files", "-z").split("\0")) - {""}
        current = set(
            subprocess.check_output(
                ["git", "-C", str(new_bundle), "ls-files", "-z"], text=True
            ).split("\0")
        ) - {""}
        for name in previous - current:
            if not name.startswith("customizations/"):
                (stage / name).unlink(missing_ok=True)
        for name in current:
            source = new_bundle / name
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (stage / "customizations").mkdir(exist_ok=True)
        source = stage / "hermes-agent"
        subprocess.run(
            [
                sys.executable,
                str(new_bundle / "hermes_kubernetes/source/prepare.py"),
                "--checkout",
                "--output",
                str(source),
            ],
            check=True,
            cwd=new_bundle,
            stdout=subprocess.DEVNULL,
        )
        report = reconcile(stage, source, base, write=True)
        if not report["ready"]:
            return report, {}
        after = {}
        for path in (stage / "customizations").rglob("*"):
            if path.is_file():
                after[path.relative_to(stage).as_posix()] = (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                )
        changes = {}
        for name in files.keys() | after.keys():
            before = base64.b64decode(files[name]["data"]) if name in files else None
            value = after.get(name)
            if before != (value[0] if value else None):
                changes[name] = (before, value)
        return report, changes
