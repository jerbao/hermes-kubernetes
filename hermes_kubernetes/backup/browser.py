"""Archive a browser profile privately with online backups of its SQLite files."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import argparse
from pathlib import Path
import sys

from hermes_kubernetes.backup.state import build_snapshot

REGENERATED = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "Crashpad",
}
TRANSIENT = {
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "DevToolsActivePort",
    "LOCK",
}


def retained(name):
    parts = Path(name).parts
    return bool(parts) and not any(
        part in REGENERATED or part in TRANSIENT or part.endswith(("-wal", "-shm", ".tmp", ".lock"))
        for part in parts
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(build_snapshot(args.profile, args.output, filter_func=retained))
