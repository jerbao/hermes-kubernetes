"""Verify versioned MCP locks using an isolated package fixture, without external MCP calls."""

from pathlib import Path
import subprocess
import sys


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
                "tests/executors",
                "-p",
                "test_packages.py",
                "-v",
            ],
            cwd=root,
        ).returncode
    )


if __name__ == "__main__":
    main()
