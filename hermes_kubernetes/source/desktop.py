"""Build and verify the Desktop client from the distribution's patched source."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


from hermes_kubernetes.source.editable import ensure
from hermes_kubernetes.source.prepare import project_revision
from hermes_kubernetes.source.upstream import specification

TESTS = [
    "src/components/provider-status-chip.test.tsx",
    "src/lib/provider-limit.test.ts",
    "src/app/shell/model-menu-panel.test.tsx",
]


def build(root, source):
    """Prepare the native build with a resolvable upstream bootstrap identity."""
    app = source / "apps/desktop"

    def run(path, *args):
        subprocess.run(["node", str(source / path), *map(str, args)], cwd=app, check=True)

    run("apps/desktop/scripts/write-build-stamp.mjs")
    stamp_path = app / "build/install-stamp.json"
    stamp = json.loads(stamp_path.read_text())
    # Editable checkouts have synthetic patch commits which do not exist in
    # upstream. Bootstrap must pin the real upstream base, with local changes
    # explicitly declared and the distribution revision recorded separately.
    stamp.update(
        commit=specification(root)["revision"],
        dirty=True,
        platformRevision=project_revision(root),
    )
    stamp_path.write_text(json.dumps(stamp, indent=2) + "\n")
    run("apps/desktop/scripts/stage-native-deps.mjs")
    run(
        "scripts/build/desktop.mjs",
        "--source",
        source,
        "--icons",
        source,
        "--stamp",
        stamp_path,
        "--native-deps",
        app / "build/native-deps",
        "--out",
        app / "dist",
    )


def install_app(source, destination):
    """Replace a macOS application transactionally, retaining all user data."""
    if sys.platform != "darwin" or destination.suffix != ".app":
        raise ValueError("--install-app requires a macOS .app destination")
    candidates = list((source / "apps/desktop/release").glob("mac*/Hermes.app"))
    if len(candidates) != 1:
        raise ValueError("Pack exactly one native Hermes.app before installing")
    with tempfile.TemporaryDirectory(prefix=".hermes-install-", dir=destination.parent) as folder:
        staged = Path(folder) / destination.name
        previous = Path(folder) / "previous.app"
        subprocess.run(["ditto", str(candidates[0]), str(staged)], check=True)
        # Source builds have no Apple Developer identity. Seal the locally built
        # bundle with an ad-hoc signature and upstream's Electron entitlements.
        subprocess.run(
            [
                "codesign",
                "--force",
                "--deep",
                "--sign",
                "-",
                "--options",
                "runtime",
                "--entitlements",
                str(source / "apps/desktop/electron/entitlements.mac.plist"),
                str(staged),
            ],
            check=True,
        )
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(staged)], check=True)
        subprocess.run(["osascript", "-e", 'tell application "Hermes" to quit'], check=True)
        if destination.exists():
            destination.rename(previous)
        try:
            staged.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    subprocess.run(["open", str(destination)], check=True)


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root / "hermes-agent")
    parser.add_argument("--install", action="store_true", help="Install locked npm dependencies")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--pack", action="store_true", help="Create a native unpacked application")
    parser.add_argument("--install-app", type=Path, help="Install a packed .app and reopen it")
    args = parser.parse_args()
    if not any((args.install, args.test, args.build, args.pack, args.install_app)):
        parser.error("Select --install, --test, --build, --pack or --install-app")
    source = args.source.resolve()
    ensure(root, source, allow_local_edits=True)
    app = source / "apps/desktop"
    if args.install:
        subprocess.run(
            [
                "npm",
                "ci",
                "--workspace",
                "apps/desktop",
                "--include-workspace-root",
                "--ignore-scripts",
            ],
            cwd=source,
            check=True,
        )
    if args.test:
        subprocess.run(["npm", "run", "test:ui", "--", *TESTS], cwd=app, check=True)
    if args.build or args.pack:
        build(root, source)
    if args.pack:
        subprocess.run(
            ["npm", "run", "builder", "--", "--dir", "--publish", "never"],
            cwd=app,
            check=True,
        )
    if args.install_app:
        install_app(source, args.install_app.resolve())


if __name__ == "__main__":
    main()
