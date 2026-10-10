"""Run the required project checks once, without production integrations or full upstream tests."""

import argparse
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runner-only", action="store_true")
    p.add_argument("--test", help="Run only this unittest filename pattern")
    a = p.parse_args()
    commands = (
        [
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-t",
                ".",
                "-s",
                "tests/executors",
                "-p",
                "test_runner.py",
                "-v",
            ]
        ]
        if a.runner_only
        else [
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
                a.test,
                "-v",
            ]
        ]
        if a.test
        else [
            [sys.executable, "hermes_kubernetes/project/validation.py"],
            [sys.executable, "-m", "unittest", "discover", "-t", ".", "-s", "tests", "-v"],
        ]
    )
    for command in commands:
        result = subprocess.run(command, cwd=root)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
