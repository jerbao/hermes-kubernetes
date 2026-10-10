"""Run only distribution extraction/adoption checks; never call production or models."""

from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[2]
    for command in [
        [sys.executable, "hermes_kubernetes/checks/publication.py"],
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
            "test_distribution.py",
            "-v",
        ],
        ["kubectl", "kustomize", "deploy/kubernetes"],
        ["kubectl", "kustomize", "deploy/kubernetes/executors"],
    ]:
        result = subprocess.run(
            command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if result.returncode:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            raise SystemExit(result.returncode)
        print(
            result.stdout if "kubectl" not in command else "Kubernetes base rendered successfully",
            end="\n",
        )
        if "unittest" in command:
            print("\n".join(result.stderr.splitlines()[-4:]))
    print("Distribution adoption checks passed; model_calls=0")


if __name__ == "__main__":
    main()
