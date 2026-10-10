"""Validate the Git projection only, without downloading or testing upstream."""

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__" and "__file__" in globals():
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
from hermes_kubernetes.terminal.versioning import projection
from hermes_kubernetes.state.versioning import target


def main():
    files = projection(root)
    for name, item in files.items():
        # Validate schema/path destinations against an empty logical home.
        target(root / ".local/projection-validation", name)
        if name.endswith(
            (
                "config.json",
                "installations.json",
                "features.json",
                "mcp-lock.json",
                "python-lock.json",
            )
        ):
            import base64

            json.loads(base64.b64decode(item["data"]))
    print(
        json.dumps(
            {
                "status": "valid",
                "files": len(files),
                "profiles": sorted({n.split("/")[1] for n in files}),
            }
        )
    )


if __name__ == "__main__":
    main()
