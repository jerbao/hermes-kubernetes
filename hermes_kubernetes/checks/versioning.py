"""Run versioning acceptance once, with isolated homes and a local Git remote."""

import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[2]
    raise SystemExit(
        subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-t",
                ".",
                "-s",
                "tests",
                "-p",
                "test_version_workspace.py",
                "-v",
            ],
            cwd=root,
        ).returncode
    )


if __name__ == "__main__":
    main()
